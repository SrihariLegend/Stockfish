#!/usr/bin/env python3
"""Phase 6.2 explanatory policy study over internal-counterfactual/3 corpora.

Single-candidate top-1 policies evaluated EXACTLY on the measured whole-node
replay costs: for every decision row and every policy, the policy's cost on
that row is the measured cost of the forced probe of the chosen candidate
(ordinal 0 = the baseline replay). No independence assumptions are needed
for single-candidate policies (the forced replay IS the node run with that
candidate first, prefix-preserving). Multi-move ordering policies require
the shared-permutation data of plan 11.3 and are out of scope here.

Policies (all ex-post / in-sample; they are UPPER BOUNDS for any learnable
rule, since they use the measured outcomes):
  BASE          natural order (ordinal 0)
  CHEAP_UNC     cheapest measured whole-node cost, any classification
  ORACLE_CLS    cheapest measured cost whose fail-high classification
                matches the baseline (0 flips by construction)
  ORACLE_EXACT  cheapest measured cost whose value equals the baseline value
  Q1_EARLIEST   promote the earliest candidate that itself cuts off
                (cutoff_by_first == true); baseline otherwise
  Q1_CHEAPEST   among candidates that themselves cut off, the cheapest;
                baseline otherwise

Outputs, per root and pooled: aggregate node sums (baseline, policy cost),
percent saved (sums, plan 11.1), later-pick share of rows, classification
flip rows (FH->FL and FL->FH), and root-clustered spread. Optional
--json PATH writes the per-root table as JSON.

Usage:
  python3 p6_explanatory.py <dir-or-glob> [--json out.json]
"""
import gzip
import glob
import json
import os
import sys

POLICIES = ["BASE", "CHEAP_UNC", "ORACLE_CLS", "ORACLE_EXACT", "Q1_EARLIEST",
            "Q1_CHEAPEST"]


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def row_costs(r):
    """C[o] / FH[o] for o in 0..3 with measured probes; plus probe dict."""
    b = r["baseline"]
    cand = {c["move"]: c["ordinal"] for c in r["candidates"]}
    pp = {cand.get(x["move"]): x for x in r["probes"]}
    C = {0: b["nodes"]}
    FH = {0: b["fail_high"]}
    for o in (1, 2, 3):
        x = pp.get(o)
        if x and x["completed"]:
            C[o] = x["nodes"]
            FH[o] = x["fail_high"]
    return C, FH, pp, b


def choose(r):
    C, FH, pp, b = row_costs(r)
    out = {}
    out["BASE"] = (C[0], 0)
    out["CHEAP_UNC"] = min(((C[o], o) for o in C), key=lambda t: t[0])
    out["ORACLE_CLS"] = min(((C[o], o) for o in C if o == 0 or FH[o] == FH[0]),
                            key=lambda t: t[0])
    out["ORACLE_EXACT"] = min(
        ((C[o], o) for o in C
         if o == 0 or (pp[o] and pp[o]["completed"] and pp[o]["value"] == b["value"])),
        key=lambda t: t[0])
    own = [o for o in (1, 2, 3)
           if o in pp and pp[o]["completed"] and pp[o]["cutoff"]["cutoff_by_first"]]
    out["Q1_EARLIEST"] = (C[own[0]], own[0]) if own else (C[0], 0)
    out["Q1_CHEAPEST"] = min(((C[o], o) for o in own), default=(C[0], 0),
                             key=lambda t: t[0])
    return out


def root_table(dec):
    agg = {k: {"B": 0, "C": 0, "later": 0, "fhfl": 0, "flfh": 0} for k in POLICIES}
    n = len(dec)
    dec = list(dec)
    for r in dec:
        C, FH, pp, b = row_costs(r)
        out = choose(r)
        for k, (cost, o) in out.items():
            a = agg[k]
            a["B"] += C[0]
            a["C"] += cost
            a["later"] += int(o > 0)
            if o and FH[o] != FH[0]:
                if FH[0]:
                    a["fhfl"] += 1
                else:
                    a["flfh"] += 1
    return {"rows": n,
            **{k: {"baseline_nodes": v["B"], "policy_nodes": v["C"],
                   "save_pct": 100 * (1 - v["C"] / v["B"]) if v["B"] else 0.0,
                   "later_share_pct": 100 * v["later"] / n,
                   "fh_to_fl_rows": v["fhfl"], "fl_to_fh_rows": v["flfh"]}
               for k, v in agg.items()}}
    return n, agg


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    json_path = None
    positionals = []
    i = 0
    while i < len(sys.argv[1:]):
        a = sys.argv[1:][i]
        if a == "--json" and i + 1 < len(sys.argv[1:]):
            json_path = sys.argv[1:][i + 1]
            i += 2
            continue
        positionals.append(a)
        i += 1
    files = []
    for a in positionals:
        files += sorted(glob.glob(a + "/*.jsonl.gz")) if os.path.isdir(a) else [a]
    files = sorted(set(files))
    if not files:
        print("no files found"); return
    per_root = {}
    for f in files:
        dec = [r for r in load(f) if r["type"] == "decision"]
        if dec:
            per_root[f] = root_table(dec)
    if json_path:
        with open(json_path, "w") as fh:
            json.dump(per_root, fh, indent=1, sort_keys=True)
    pooled = {k: {"B": 0, "C": 0, "later": 0, "fhfl": 0, "flfh": 0} for k in POLICIES}
    nrows = 0
    for f in files:
        dec = [r for r in load(f) if r["type"] == "decision"]
        nrows += len(dec)
        for r in dec:
            C, FH, pp, b = row_costs(r)
            out = choose(r)
            for k, (cost, o) in out.items():
                a = pooled[k]
                a["B"] += C[0]
                a["C"] += cost
                a["later"] += int(o > 0)
                if o and FH[o] != FH[0]:
                    if FH[0]:
                        a["fhfl"] += 1
                    else:
                        a["flfh"] += 1
    print(f"{'policy':14s} {'save%':>7s} {'later%':>7s} {'FH->FL':>6s} {'FL->FH':>6s}")
    for k in POLICIES:
        a = pooled[k]
        print(f"{k:14s} {100*(1-a['C']/a['B']):7.2f} {100*a['later']/nrows:7.2f} "
              f"{a['fhfl']:6d} {a['flfh']:6d}")
    if json_path:
        print("wrote", json_path)


if __name__ == "__main__":
    main()
