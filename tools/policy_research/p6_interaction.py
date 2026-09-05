#!/usr/bin/env python3
"""Phase 6.3 interaction-gap analysis over internal-counterfactual/4 corpora.

Validates the shared-permutation battery on every row and reports the
interaction-gap tables (plan 11.3):

Validation (per row, when the corresponding battery member exists):
  - probe ordinal k == permutation control [k, 0..k-1, k+1..K-1]
    (nodes, value, fail_high; bit-for-bit expected);
  - identity permutation [0..K-1] == baseline replay (flagged separately:
    a documented ~0.2% of rows have capture-enumeration order != the node's
    own picker emission order; those rows are counted, not failed);
  - no duplicate orders per row; completed/censoring bookkeeping.

Analysis (sums before division, plan 11.1):
  - per battery permutation: aggregate ratio sum(perm.nodes)/sum(baseline)
    over completed runs, per root and pooled;
  - the interaction gap: per-row best tested permutation vs baseline vs the
    scalar ex-post cheapest-first permutation (the 11.3 reference order);
  - stratification by baseline size and entry depth; root-clustered spread;
  - censored/not-fully-served run counts (fully_served == false is
    legitimate when a cutoff ended the node before all K targets served).

Usage:
  python3 p6_interaction.py <dir-or-file>...
"""
import gzip
import glob
import json
import os
import sys

BATTERY = ["ident", "ctrl1", "ctrl2", "ctrl3", "rev", "rot", "s12", "s23",
           "cheap"]


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def classify(order):
    """Name the battery member an order corresponds to (None for extras)."""
    K = len(order)
    ident = list(range(K))
    if order == ident:
        return "ident"
    for k in (1, 2, 3):
        if k < K and order == [k] + [i for i in range(K) if i != k]:
            return f"ctrl{k}"
    if order == list(reversed(ident)):
        return "rev"
    if K >= 2 and order == list(range(1, K)) + [0]:
        return "rot"
    if K >= 4 and order == [0, 2, 1] + list(range(3, K)):
        return "s12"
    if K >= 4 and order == [0, 1, 3] + list(range(2, K)):
        return "s23"
    # cheapest-first is data dependent; match only by exclusion
    return "cheap"


def row_checks(r):
    """Return (problems, flags) for one decision row."""
    problems = []
    flags = {}
    cand = {c["move"]: c["ordinal"] for c in r["candidates"]}
    probes = {}
    for pr in r["probes"]:
        o = cand.get(pr["move"])
        if o is not None:
            probes[o] = pr
    orders = [tuple(x["order"]) for x in r["permutations"]]
    if len(orders) != len(set(orders)):
        problems.append("duplicate orders")
    n_ctrl = 0
    for perm in r["permutations"]:
        order = perm["order"]
        K = len(order)
        name = classify(order)
        if name == "ident":
            b = r["baseline"]
            if not (b["nodes"] == perm["nodes"] and b["value"] == perm["value"]
                    and b["fail_high"] == perm["fail_high"]):
                flags["ident_mismatch"] = True
        elif name.startswith("ctrl"):
            k = int(name[4])
            pr = probes.get(k)
            n_ctrl += 1
            if pr is None:
                problems.append(f"ctrl{k} without probe")
            elif not (pr["nodes"] == perm["nodes"] and pr["value"] == perm["value"]
                      and pr["fail_high"] == perm["fail_high"]):
                problems.append(f"ctrl{k} != probe")
    return problems, flags


def analyze(files):
    per_root = {}
    for f in files:
        rows = load(f)
        dec = [r for r in rows if r["type"] == "decision"]
        if not dec:
            continue
        checks = {"rows": len(dec), "problems": 0, "ident_mismatch": 0,
                  "censored_perms": 0, "not_fully_served": 0}
        # aggregate node sums per battery member
        agg = {}
        base_total = 0
        per_perm = {}
        for r in dec:
            probs, flags = row_checks(r)
            checks["problems"] += len(probs)
            checks["ident_mismatch"] += int(flags.get("ident_mismatch", False))
            b = r["baseline"]
            base_total += b["nodes"]
            for perm in r["permutations"]:
                name = classify(perm["order"])
                if not perm["completed"]:
                    checks["censored_perms"] += 1
                    continue
                if not perm["fully_served"]:
                    checks["not_fully_served"] += 1
                a = agg.setdefault(name, {"B": 0, "C": 0, "n": 0})
                a["B"] += b["nodes"]
                a["C"] += perm["nodes"]
                a["n"] += 1
        # per-row references: best tested shared permutation (completed),
        # cheapest-first shared order (sorted by the row's scalar probe
        # costs), classification-preserving scalar oracle (ex-post).
        best_tot = cheap_tot = oracle_tot = 0
        rows_n = 0
        for r in dec:
            b = r["baseline"]
            best_tot += b["nodes"]
            cheap_tot += b["nodes"]
            rows_n += 1
            C = {0: b["nodes"]}
            FH = {0: b["fail_high"]}
            for pr in r["probes"]:
                o = next((c["ordinal"] for c in r["candidates"]
                          if c["move"] == pr["move"]), None)
                if o is not None and pr["completed"]:
                    C[o] = pr["nodes"]
                    FH[o] = pr["fail_high"]
            oracle_tot += min(C[o] for o in range(4) if o == 0 or FH[o] == FH[0])
            best_row = b["nodes"]
            cheap_row = None
            for perm in r["permutations"]:
                if not perm["completed"]:
                    continue
                best_row = min(best_row, perm["nodes"])
                order = perm["order"]
                sortc = sorted(range(len(order)), key=lambda o: (C[o], o))
                if list(order) == sortc:
                    cheap_row = perm["nodes"]
            best_tot += best_row - b["nodes"]
            if cheap_row is not None:
                cheap_tot += cheap_row - b["nodes"]
        per_root[f] = {"checks": checks, "agg": agg, "base_total": base_total,
                       "rows": len(dec),
                       "best_perm": best_tot, "cheap_first": cheap_tot,
                       "oracle_cls": oracle_tot}
    return per_root


def main():
    files = []
    for a in sys.argv[1:]:
        files += sorted(glob.glob(a + "/*.jsonl.gz")) if os.path.isdir(a) else [a]
    files = sorted(set(files))
    if not files:
        print("no files found")
        return
    per_root = analyze(files)
    pooled = {}
    base_total = 0
    rows_total = 0
    for f, info in per_root.items():
        print(f"== {f}")
        c = info["checks"]
        print(f"   rows {c['rows']} problems {c['problems']} "
              f"ident_mismatch {c['ident_mismatch']} censored_perms "
              f"{c['censored_perms']} not_fully_served {c['not_fully_served']}")
        print(f"   {'perm':8s} {'n':>6s} {'ratio':>8s} {'saved%':>7s}")
        for name in BATTERY:
            if name in info["agg"]:
                a = info["agg"][name]
                ratio = a["C"] / a["B"]
                print(f"   {name:8s} {a['n']:6d} {ratio:8.3f} "
                      f"{100*(1-ratio):7.2f}")
        base_total += info["base_total"]
        rows_total += info["rows"]
        for name, a in info["agg"].items():
            p = pooled.setdefault(name, {"B": 0, "C": 0, "n": 0})
            p["B"] += a["B"]
            p["C"] += a["C"]
            p["n"] += a["n"]
    for key in ("best_perm", "cheap_first", "oracle_cls"):
        tot = sum(per_root[f][key] for f in per_root)
        print(f"   pooled {key:12s} saved {100*(1-tot/base_total):.2f}% "
              f"({tot})")
    print(f"POOLED rows {rows_total} baseline {base_total}")
    for name in BATTERY:
        if name in pooled:
            a = pooled[name]
            ratio = a["C"] / a["B"]
            print(f"   {name:8s} n {a['n']:6d} ratio {ratio:.3f} "
                  f"saved {100*(1-ratio):.2f}%")


if __name__ == "__main__":
    main()
