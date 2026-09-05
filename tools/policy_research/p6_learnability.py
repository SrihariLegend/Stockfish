#!/usr/bin/env python3
"""Phase 6.2 learnability probe: root-held-out, abstaining, feature-only
policies evaluated on the exact measured top-1 costs (plan 11.2/11.4/11.5).

Data: internal-counterfactual/3 decision rows (top-four census). Every
candidate ordinal 0..3 of every row carries the measured whole-node cost
C_o (the forced replay cost; ordinal 0 = baseline) and the measured slot-1
own-cut label q_o (cutoff_by_first). All candidate features come from the
row (MovePicker stage/histories/SEE/etc. plus node context), i.e. they are
available BEFORE any candidate is searched: no future information enters a
predictor.

Procedure: leave-one-root-out over the 12 separately executed positions.
On the 11 training roots, fit ridge logistic q-hat (own cut) and ridge
log-cost c-hat models on per-candidate examples; tune each policy's
threshold/hyperparameter (theta, tau, lambda) by TOTAL measured cost on the
training roots only; apply the tuned policy to every row of the held-out
root and charge the row's MEASURED cost of the chosen ordinal (abstain =
ordinal 0 = baseline cost). Pooled aggregates are ratio-of-sums; per-root
savings are reported as the descriptive spread. This is the first
deployable-shape (feature-only, abstaining, out-of-root) measurement; it is
still optimistic because within-root correlation means the 12 roots are not
12 independent samples of a deployment distribution.

Policies:
  BASE         always ordinal 0
  Q(theta)     promote argmax q-hat over 1..3 when it exceeds theta
  CHEAP        promote argmin c-hat over 0..3
  CHEAP_SAFE   promote argmin c-hat over {0} + {o>=1 : q-hat_o >= tau}
  QE(lambda)   promote argmax over 0..3 of logit(q-hat_o) - lambda log c-hat_o

Ex-post references (reported for context, not learnable): ORACLE_CLS and
ORACLE_EXACT.

Outputs: console table and (with --json PATH) a machine-readable report.

Usage:
  python3 p6_learnability.py <dir-or-file>... [--json out.json]
"""
import gzip
import glob
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p6_explanatory import ORACLES, load, row_costs  # noqa: E402

RNG = np.random.RandomState(0)
LAMBDA_RIDGE = 1e-3


# --------------------------------------------------------------------------
# Row -> feature matrix (context + per-candidate features, pre-search only)
# --------------------------------------------------------------------------
STAGE_ORDER = ["tt", "good_capture", "good_quiet", "bad_capture", "bad_quiet"]
NUM_FEATURES = [
    "ply", "entry_depth", "depth", "root_depth", "improving", "tt_hit",
    "cut_node", "rule50", "n_candidates", "static_eval", "static_missing",
    "ordinal", "stage_score", "main_hist", "capture_hist", "pawn_hist",
    "cont_hist", "low_ply_hist", "see_bucket", "check", "capture", "tt_move",
] + [f"stage_{s}" for s in STAGE_ORDER]


def row_examples(r):
    """Yield (feature_vector, ordinal, q_label, C_label, C0) per candidate
    ordinal 0..3 with a completed measurement."""
    C, FH, pp, b = row_costs(r)
    ctx = {
        "ply": r["ply"], "entry_depth": r["entry_depth"], "depth": r["depth"],
        "root_depth": r["root_depth"], "improving": int(r["improving"]),
        "tt_hit": int(r["tt_hit"]), "cut_node": int(r["cut_node"]),
        "rule50": r["rule50"], "n_candidates": r["n_candidates"],
        "static_eval": r["static_eval"] if r["static_eval"] is not None else 0.0,
        "static_missing": int(r["static_eval"] is None),
    }
    for o in range(4):
        if o not in C:
            continue
        if o > 0:
            p = pp[o]
            own = int(p["cutoff"]["cutoff_by_first"])
        else:
            own = int(b["cutoff"]["cutoff_by_first"])
        cf = next(c for c in r["candidates"] if c["ordinal"] == o)
        feats = dict(ctx)
        feats.update({
            "ordinal": o, "stage_score": cf["stage_score"] or 0,
            "main_hist": cf["main_hist"] or 0,
            "capture_hist": cf["capture_hist"] or 0,
            "pawn_hist": cf["pawn_hist"] or 0,
            "cont_hist": cf["cont_hist"] or 0,
            "low_ply_hist": cf["low_ply_hist"] or 0,
            "see_bucket": cf["see_bucket"] or 0,
            "check": int(cf["check"]), "capture": int(cf["capture"]),
            "tt_move": int(cf["tt_move"]),
        })
        for s in STAGE_ORDER:
            feats[f"stage_{s}"] = int(cf["stage"] == s)
        yield feats, o, own, C[o], C[0]


def feature_matrix(examples, train_scale=None):
    """Stack feature dicts into a design matrix; winsorize at the training
    1st/99th percentiles, then standardize with training mean/std (test
    folds transform with train statistics). Heavy-tailed history features
    make raw z-scoring outlier-dominated on held-out roots; the quantile
    clip keeps the linear models stable."""
    X = np.array([[e[k] for k in NUM_FEATURES] for e, *_ in examples],
                 dtype=np.float64)
    if train_scale is None:
        p1 = np.percentile(X, 1, axis=0)
        p99 = np.percentile(X, 99, axis=0)
        p99[p99 == p1] = p1[p99 == p1] + 1.0
        Xc = np.clip(X, p1, p99)
        mu = Xc.mean(axis=0)
        sd = Xc.std(axis=0)
        sd[sd == 0] = 1.0
    else:
        p1, p99, mu, sd = train_scale
        Xc = np.clip(X, p1, p99)
    X = (Xc - mu) / sd
    # append a ones column
    X = np.hstack([X, np.ones((X.shape[0], 1))])
    return X, (p1, p99, mu, sd)


def ridge_fit(X, y, lam=LAMBDA_RIDGE):
    """Closed-form ridge solution; y numeric. Returns (coef, intercept)."""
    XtX = X.T @ X
    XtX.flat[:: XtX.shape[0] + 1] += lam
    Xty = X.T @ y
    w = np.linalg.solve(XtX, Xty)
    return w[:-1], w[-1]


def ridge_logistic(X, y, lam=LAMBDA_RIDGE, iters=40):
    """IRLS ridge logistic regression (for the q-hat model; plain
    ridge-on-0/1 nearly interpolates and saturates sigmoid probabilities,
    which breaks threshold semantics). Returns (coef, intercept)."""
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        z = np.clip(X @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        s = np.clip(p * (1 - p), 1e-9, None)
        zstar = z + (y - p) / s
        W = np.sqrt(s)
        Xw = X * W[:, None]
        H = Xw.T @ Xw
        H.flat[:: H.shape[0] + 1] += lam
        w_new = np.linalg.solve(H, Xw.T @ (W * zstar))
        if np.allclose(w_new, w, atol=1e-8, rtol=1e-6):
            w = w_new
            break
        w = w_new
    return w[:-1], w[-1]
def logistic(X, w, b):
    z = X @ np.hstack([w, b])
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


# --------------------------------------------------------------------------
# Per-root policy application
# --------------------------------------------------------------------------
def apply_policy(rows, score_fn):
    """score_fn(o_features, o) -> scalar score over ordinals 0..3; the
    policy promotes the argmax ordinal (0 = abstain) when it differs from 0;
    charges the measured cost. Returns (total_baseline, total_cost,
    later_rows, fhfl_rows, flfh_rows)."""
    tb = tc = later = fhfl = flfh = 0
    for r in rows:
        exs = list(row_examples(r))
        if not exs:
            continue
        C0 = exs[0][4]
        tb += C0
        best_o, best_s = 0, score_fn(exs[0][0], 0, C0)
        for e in exs:
            feats, o, _, C, _ = e
            s = score_fn(feats, o, C0)
            if s > best_s:
                best_s, best_o = s, o
        C, FH, pp, b = row_costs(r)
        tc += C[best_o]
        later += int(best_o > 0)
        if best_o and FH[best_o] != FH[0]:
            if FH[0]:
                fhfl += 1
            else:
                flfh += 1
    return tb, tc, later, fhfl, flfh


def total_cost(rows, score_fn):
    return apply_policy(rows, score_fn)[1]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    json_path = None
    positionals = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--json" and i + 1 < len(argv):
            json_path = argv[i + 1]
            i += 2
            continue
        positionals.append(argv[i])
        i += 1
    files = []
    for a in positionals:
        files += sorted(glob.glob(a + "/*.jsonl.gz")) if os.path.isdir(a) else [a]
    files = sorted(set(files))
    if not files:
        print("no files found")
        return
    per_root = {}
    for f in files:
        dec = [r for r in load(f) if r["type"] == "decision"]
        if dec:
            per_root[f] = dec
    root_names = list(per_root)

    def q_hat(feats, coef_q, intercept_q):
        X = np.array([[feats[k] for k in NUM_FEATURES] + [1.0]])
        return float(logistic(X, coef_q, intercept_q)[0])

    def q_score(coef_q, intercept_q, theta):
        def f(feats, o, C0):
            if o == 0:
                return 0.0
            return q_hat(feats, coef_q, intercept_q) - theta
        return f

    def c_hat(feats, coef_c, intercept_c):
        X = np.array([[feats[k] for k in NUM_FEATURES] + [1.0]])
        return float(np.exp(np.clip(X @ np.hstack([coef_c, intercept_c]), -20, 20))[0])

    def cheap_score(coef_c, intercept_c):
        def f(feats, o, C0):
            return -c_hat(feats, coef_c, intercept_c)
        return f

    def cheap_safe_score(coef_q, iq, coef_c, ic, tau):
        def f(feats, o, C0):
            q = q_hat(feats, coef_q, iq)
            if o > 0 and q < tau:
                return -1e18
            return -c_hat(feats, coef_c, ic)
        return f

    def qe_score(coef_q, iq, coef_c, ic, lam):
        def f(feats, o, C0):
            q = q_hat(feats, coef_q, iq)
            c = c_hat(feats, coef_c, ic)
            return math.log(max(q, 1e-4)) - lam * math.log(max(c, 1e-3))
        return f

    policies = {"Q": {}, "CHEAP": {}, "CHEAP_SAFE": {}, "QE": {}}
    oracle_cls = {name: 0 for name in root_names}
    report = {"roots": {}, "policies": {}}
    pooled = {k: {"B": 0, "C": 0, "later": 0, "fhfl": 0, "flfh": 0}
              for k in ["BASE", "Q", "CHEAP", "CHEAP_SAFE", "QE"]}
    insample = {k: {"B": 0, "C": 0} for k in ["Q", "CHEAP_SAFE", "QE"]}
    chosen_params = {}
    diag = {"q_auc": [], "c_spearman": []}

    for held_out in root_names:
        train = [r for n in root_names for r in per_root[n] if n != held_out]
        test = per_root[held_out]
        # training examples
        tr_ex = []
        for r in train:
            tr_ex += list(row_examples(r))
        Xtr, scale = feature_matrix(tr_ex)
        yq = np.array([e[2] for e in tr_ex])
        yc = np.log(np.maximum(np.array([e[3] for e in tr_ex]), 1.0))
        wq, bq = ridge_logistic(Xtr, yq)
        wc, bc = ridge_fit(Xtr, yc)
        # scale test folds the same way
        te_ex = [e for r in test for e in row_examples(r)]
        Xte, _ = feature_matrix(te_ex, train_scale=scale)
        # oracle references on the test root (ex-post)
        ob = ot = 0
        for r in test:
            exs = list(row_examples(r))
            if not exs:
                continue
            C, FH, pp, b = row_costs(r)
            ob += C[0]
            ot += min(C[o] for o in range(4) if o == 0 or FH[o] == FH[0])
        oracle_cls[held_out] = 100 * (1 - ot / ob) if ob else 0.0

        # threshold tuning on TRAINING roots only
        def train_cost(score_fn):
            return total_cost(train, score_fn)

        best = {}
        # Q theta grid
        grid = list(np.arange(0.05, 1.0, 0.05)) + [1.5]
        costs = [train_cost(q_score(wq, bq, float(th))) for th in grid]
        th = float(grid[int(np.argmin(costs))])
        best["Q"] = q_score(wq, bq, th)
        # CHEAP_SAFE tau grid (promote cheapest among sufficiently-own-cut)
        costs = [train_cost(cheap_safe_score(wq, bq, wc, bc, float(ta)))
                 for ta in grid]
        ta = float(grid[int(np.argmin(costs))])
        best["CHEAP_SAFE"] = cheap_safe_score(wq, bq, wc, bc, ta)
        # QE lambda grid
        costs = [train_cost(qe_score(wq, bq, wc, bc, float(l)))
                 for l in (0.25, 0.5, 1.0, 2.0)]
        lam = float((0.25, 0.5, 1.0, 2.0)[int(np.argmin(costs))])
        best["QE"] = qe_score(wq, bq, wc, bc, lam)
        best["CHEAP"] = cheap_score(wc, bc)
        chosen_params[held_out] = {"theta": th, "tau": ta, "lambda": lam}
        for nm, sf in (("Q", best["Q"]), ("CHEAP_SAFE", best["CHEAP_SAFE"]),
                       ("QE", best["QE"])):
            tb, tc, *_ = apply_policy(train, sf)
            insample[nm]["B"] += tb
            insample[nm]["C"] += tc

        # held-out predictability diagnostics for the test root
        if len(te_ex) > 0:
            zq = Xte @ np.hstack([wq, bq])
            yq_te = np.array([e[2] for e in te_ex])
            auc = 0.0
            pos = yq_te == 1
            if pos.sum() > 0 and (~pos).sum() > 0:
                r = np.argsort(np.argsort(zq))
                auc = (r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (
                    pos.sum() * (~pos).sum())
            diag["q_auc"].append(float(auc))
            zc = Xte @ np.hstack([wc, bc])
            yc_te = np.log(np.maximum(np.array([e[3] for e in te_ex]), 1.0))
            if len(zc) > 1 and np.std(zc) > 0:
                rr = np.corrcoef(np.argsort(np.argsort(zc)),
                                 np.argsort(np.argsort(yc_te)))[0, 1]
            else:
                rr = float("nan")
            diag["c_spearman"].append(float(rr))

        row_report = {}
        for name, score_fn in best.items():
            tb, tc, later, fhfl, flfh = apply_policy(test, score_fn)
            policies[name][held_out] = (tb, tc, later, fhfl, flfh)
            row_report[name] = {"baseline_nodes": tb, "policy_nodes": tc,
                                "later_rows": later, "fhfl": fhfl, "flfh": flfh}
            for k, v in [("B", tb), ("C", tc), ("later", later),
                         ("fhfl", fhfl), ("flfh", flfh)]:
                pooled[name][k] += v
        # BASE
        tb, tc, later, fhfl, flfh = apply_policy(test, lambda f, o, c: 0.0)
        row_report["BASE"] = {"baseline_nodes": tb, "policy_nodes": tc,
                              "later_rows": later, "fhfl": fhfl, "flfh": flfh}
        for k, v in [("B", tb), ("C", tc), ("later", later),
                     ("fhfl", fhfl), ("flfh", flfh)]:
            pooled["BASE"][k] += v
        report["roots"][held_out] = {"oracle_cls_save_pct": oracle_cls[held_out],
                                     **row_report}

    print(f"LOO over {len(root_names)} roots")
    nrows = sum(len(per_root[n]) for n in root_names)
    tot_b = tot_or = 0
    for n in root_names:
        for r in per_root[n]:
            C, FH, pp, b = row_costs(r)
            tot_b += C[0]
            tot_or += min(C[o] for o in range(4) if o == 0 or FH[o] == FH[0])
    print(f"ORACLE_CLS pooled save {100*(1-tot_or/tot_b):.2f}%  "
          f"ORACLE_EXACT refs in per-root json")
    print("in-sample (train-root) pooled save of the tuned rules "
          "[overfit reference]:")
    for nm in ("Q", "CHEAP_SAFE", "QE"):
        a = insample[nm]
        print(f"   {nm:12s} in-sample save {100*(1-a['C']/a['B']):6.2f}%")
    import statistics as _st
    print(f"held-out q AUC mean {_st.mean(diag['q_auc']):.3f} "
          f"(per-fold: {[f'{v:.2f}' for v in diag['q_auc']]})")
    print(f"held-out log-cost Spearman mean {_st.mean(diag['c_spearman']):.3f} "
          f"(per-fold: {[f'{v:.2f}' for v in diag['c_spearman']]})")
    print("per-fold chosen params (theta/tau/lambda):")
    for n in root_names:
        p = chosen_params[n]
        print(f"   {n.split('/')[-1]:22s} theta {p['theta']:.2f} tau "
              f"{p['tau']:.2f} lambda {p['lambda']:.2f}")
    for name in ["BASE", "Q", "CHEAP", "CHEAP_SAFE", "QE"]:
        a = pooled[name]
        print(f"{name:12s} save% {100*(1-a['C']/a['B']):6.2f}  later% "
              f"{100*a['later']/nrows:5.1f}  FH->FL {a['fhfl']:5d}  "
              f"FL->FH {a['flfh']:5d}  pooled {a['B']} -> {a['C']}")
    for name in ["Q", "CHEAP", "CHEAP_SAFE", "QE"]:
        tc = pooled[name]["C"]
        cap = (tot_b - tc) / (tot_b - tot_or) if tot_b > tot_or else float("nan")
        print(f"capture vs ORACLE_CLS {name:12s} {100*cap:6.1f}%")
    if json_path:
        out = {"roots": {}, "pooled": {}}
        for n in root_names:
            out["roots"][n] = {"oracle_cls_save_pct": oracle_cls[n],
                               **{name: {"baseline_nodes": policies[name][n][0],
                                         "policy_nodes": policies[name][n][1],
                                         "later_rows": policies[name][n][2],
                                         "fhfl": policies[name][n][3],
                                         "flfh": policies[name][n][4]}
                                  for name in policies}}
        for name in ["BASE", "Q", "CHEAP", "CHEAP_SAFE", "QE"]:
            a = pooled[name]
            cap = (a["B"] - a["C"]) / (a["B"] - tot_or) if a["B"] > tot_or else float("nan")
            out["pooled"][name] = {"baseline_nodes": a["B"], "policy_nodes": a["C"],
                                   "save_pct": 100 * (1 - a["C"] / a["B"]),
                                   "later_share_pct": 100 * a["later"] / nrows,
                                   "fhfl": a["fhfl"], "flfh": a["flfh"],
                                   "capture_oracle_cls_pct": 100 * cap}
        out["pooled"]["ORACLE_CLS"] = {"baseline_nodes": tot_b,
                                       "policy_nodes": tot_or,
                                       "save_pct": 100 * (1 - tot_or / tot_b)}
        with open(json_path, "w") as fh:
            json.dump(out, fh, indent=1, sort_keys=True)
        print("wrote", json_path)


if __name__ == "__main__":
    main()
