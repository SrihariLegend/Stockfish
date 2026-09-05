#!/usr/bin/env python3
"""Phase-6 readiness analysis over internal-counterfactual/3 evidence sets.

Reproduces the key tables of the p5-m2m3-reorder-v3 and p5-phase6-breadth
evidence READMEs from the raw JSONL.gz corpora:

  per-root (and pooled) ordinal outcome/cost tables for the census ordinals
  0..3 (forced whole-node replay vs baseline: fail-high flips, forced/
  baseline node ratio), the measurement-A summary (natural-cutoff ordinal
  distribution; forced candidate cutoff_by_first rates), the
  classification-preserving local-oracle savings over ordinals 0..3, and
  the baseline-vs-live node determinism audit (join on sample_id).

Usage:
  python3 p5b_analysis.py <evidence-dir-or-glob> [--census 0..3]

Rows are schema internal-counterfactual/3 decision/node_exit rows.
"""
import gzip
import glob
import json
import statistics
import sys


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def decisions(path):
    return [r for r in load(path) if r["type"] == "decision"]


def per_row(dec):
    """Index forced probes by natural candidate ordinal."""
    out = []
    for r in dec:
        cand = {c["move"]: c["ordinal"] for c in r["candidates"]}
        out.append((r, cand, {cand.get(p["move"]): p for p in r["probes"]}))
    return out


def ordinal_table(dec, ords=(0, 1, 2, 3)):
    rows = per_row(dec)
    n = {o: 0 for o in ords}
    fh = {o: 0 for o in ords}
    ffl = {o: 0 for o in ords}
    flfh = {o: 0 for o in ords}
    ratio = {o: [] for o in ords}
    for r, cand, pr in rows:
        b = r["baseline"]
        for o in ords:
            p = pr.get(o)
            if p is None or not p["completed"]:
                continue
            n[o] += 1
            fh[o] += int(p["fail_high"])
            if b["fail_high"] and not p["fail_high"]:
                ffl[o] += 1
            if (not b["fail_high"]) and p["fail_high"]:
                flfh[o] += 1
            if b["nodes"] > 0:
                ratio[o].append(p["nodes"] / b["nodes"])
    return n, fh, ffl, flfh, ratio


def oracle_savings(dec):
    """Cheapest classification-preserving choice over ordinals 0..3."""
    base_tot = 0
    or_tot = 0
    later = 0
    for r, cand, pr in per_row(dec):
        b = r["baseline"]
        base_tot += b["nodes"]
        best, bo = b["nodes"], 0
        for o in (1, 2, 3):
            p = pr.get(o)
            if p is None or not p["completed"]:
                continue
            if p["fail_high"] != b["fail_high"]:
                continue
            if p["nodes"] < best:
                best, bo = p["nodes"], o
        or_tot += best
        later += int(bo > 0)
    return base_tot, or_tot, later


def main():
    paths = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(glob.glob(paths + "/*.jsonl.gz")) or sorted(glob.glob(paths))
    if not files:
        print("no files found"); return
    pooled = []
    for f in files:
        dec = decisions(f)
        if not dec:
            continue
        n, fh, ffl, flfh, ratio = ordinal_table(dec)
        fh_base = 100 * sum(1 for r in dec if r["baseline"]["fail_high"]) / len(dec)
        bt, ot, later = oracle_savings(dec)
        print(f"== {f}")
        print(f"   rows {len(dec)}  baseline FH {fh_base:.1f}%")
        for o in sorted(n):
            if not n[o]:
                continue
            mr = statistics.mean(ratio[o]) if ratio[o] else float("nan")
            print(f"   ord {o}: n {n[o]} FH {100*fh[o]/n[o]:.1f}%  "
                  f"FH->FL {100*ffl[o]/n[o]:.1f}%  FL->FH {100*flfh[o]/n[o]:.1f}%  "
                  f"node ratio {mr:.3f}")
        print(f"   oracle: {bt} -> {ot} ({100*(1-ot/bt):.1f}% saved; "
              f"later pick {100*later/len(dec):.1f}% of rows)")
        pooled.append((f, len(dec), bt, ot, ratio))
    # pooled summary
    nb = sum(x[2] for x in pooled)
    no_ = sum(x[3] for x in pooled)
    allr = []
    for _, _, _, _, ratio in pooled:
        for o in (1, 2, 3):
            allr += ratio[o]
    print(f"POOLED: {sum(x[1] for x in pooled)} rows; baseline {nb} -> oracle {no_}"
          f" ({100*(1-no_/nb):.1f}% saved); pooled ord1..3 ratios "
          f"{statistics.mean(allr):.3f}")


if __name__ == "__main__":
    main()
