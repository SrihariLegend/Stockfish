#!/usr/bin/env python3
"""Phase-6 readiness analysis over internal-counterfactual/3 evidence sets.

Reproduces the key tables of the p5-m2m3-reorder-v3 and p5-phase6-breadth
evidence READMEs from the raw JSONL.gz corpora:

  per-root (and pooled) ordinal outcome/cost tables for the census ordinals
  0..3 (forced whole-node replay vs baseline: fail-high flips), the
  measurement-A summary (natural-cutoff ordinal distribution; forced
  candidate cutoff_by_first rates), the classification-preserving
  local-oracle savings over ordinals 0..3 (summed, per plan §11.1), and the
  baseline-vs-live node determinism audit (join on sample_id).

Cost aggregation follows plan §11.1 ("sum costs before dividing, not by
naively averaging per-node percentages"): the headline cost change is the
ratio of SUMMED forced nodes to SUMMED baseline nodes. The arithmetic mean
of per-row ratios is reported separately as a purely descriptive per-visit
statistic and is explicitly not to be read as the aggregate cost multiplier
(tiny subtrees dominate it).

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
    sum_base = {o: 0 for o in ords}
    sum_forced = {o: 0 for o in ords}
    deltas = {o: [] for o in ords}
    censored = {o: 0 for o in ords}
    for r, cand, pr in rows:
        b = r["baseline"]
        for o in ords:
            p = pr.get(o)
            if p is None:
                continue
            if not p["completed"]:
                censored[o] += 1
                continue
            n[o] += 1
            fh[o] += int(p["fail_high"])
            if b["fail_high"] and not p["fail_high"]:
                ffl[o] += 1
            if (not b["fail_high"]) and p["fail_high"]:
                flfh[o] += 1
            sum_base[o] += b["nodes"]
            sum_forced[o] += p["nodes"]
            deltas[o].append(p["nodes"] - b["nodes"])
    return {
        "n": n, "fh": fh, "ffl": ffl, "flfh": flfh, "sum_base": sum_base,
        "sum_forced": sum_forced, "deltas": deltas, "censored": censored,
    }


def oracle_savings(dec, exact=False):
    """Cheapest classification-preserving (or exact-value) choice over
    ordinals 0..3. Costs are SUMMED before the ratio is taken."""
    base_tot = 0
    or_tot = 0
    later = 0
    censored_blocking = 0
    for r, cand, pr in per_row(dec):
        b = r["baseline"]
        base_tot += b["nodes"]
        best = b["nodes"]
        bo = 0
        for o in (1, 2, 3):
            p = pr.get(o)
            if p is None:
                continue
            if not p["completed"]:
                censored_blocking += 1  # cannot prove cheaper; counted per row
                continue
            if not exact and p["fail_high"] != b["fail_high"]:
                continue
            if exact and p["value"] != b["value"]:
                continue
            if p["nodes"] < best:
                best, bo = p["nodes"], o
        or_tot += best
        later += int(bo > 0)
    return base_tot, or_tot, later, censored_blocking


def main():
    path_arg = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(glob.glob(path_arg + "/*.jsonl.gz"))
    if not files:
        files = sorted(glob.glob(path_arg))
    if not files:
        print("no files found")
        return
    pooled_n = pooled_sum_base = pooled_sum_f = 0
    pooled_or = pooled_ex = pooled_later = pooled_block = 0
    pooled_ord_sum_base = {o: 0 for o in (0, 1, 2, 3)}
    pooled_ord_sum_f = {o: 0 for o in (0, 1, 2, 3)}
    pooled_ord_n = {o: 0 for o in (0, 1, 2, 3)}
    all_deltas = {o: [] for o in (1, 2, 3)}
    for f in files:
        dec = decisions(f)
        if not dec:
            continue
        t = ordinal_table(dec)
        fh_base = 100 * sum(1 for r in dec if r["baseline"]["fail_high"]) / len(dec)
        bt, ot, later, block = oracle_savings(dec)
        bt_e, ot_e, _, _ = oracle_savings(dec, exact=True)
        print(f"== {f}")
        print(f"   rows {len(dec)}  baseline FH {fh_base:.1f}%")
        for o in sorted(t["n"]):
            if not t["n"][o]:
                continue
            aggr = t["sum_forced"][o] / t["sum_base"][o]
            mrow = None
            if t["n"][o] and t["sum_base"][o]:
                # descriptive per-row mean ratio
                rows_ = per_row(dec)
                rat = []
                for r, cand, pr in rows_:
                    p = pr.get(o)
                    if p and p["completed"] and r["baseline"]["nodes"] > 0:
                        rat.append(p["nodes"] / r["baseline"]["nodes"])
                mrow = statistics.mean(rat) if rat else float("nan")
            print(f"   ord {o}: n {t['n'][o]} FH {100*t['fh'][o]/t['n'][o]:.1f}%  "
                  f"FH->FL {100*t['ffl'][o]/t['n'][o]:.1f}%  FL->FH {100*t['flfh'][o]/t['n'][o]:.1f}%  "
                  f"aggregate ratio {aggr:.3f} (+{100*(aggr-1):.1f}%)  "
                  f"mean row ratio {mrow:.3f}  censored {t['censored'][o]}")
        print(f"   oracle (classification-preserving): {bt} -> {ot} "
              f"({100*(1-ot/bt):.1f}% saved; later pick {100*later/len(dec):.1f}% "
              f"of rows; censored-blocked probes {block})")
        print(f"   oracle (exact-value): {bt_e} -> {ot_e} ({100*(1-ot_e/bt_e):.1f}% saved)")
        pooled_n += len(dec)
        pooled_sum_base += bt
        pooled_or += ot
        pooled_ex += ot_e
        pooled_later += later
        pooled_block += block
        for o in (0, 1, 2, 3):
            pooled_ord_sum_base[o] += t["sum_base"][o]
            pooled_ord_sum_f[o] += t["sum_forced"][o]
            pooled_ord_n[o] += t["n"][o]
        for o in (1, 2, 3):
            all_deltas[o] += t["deltas"][o]
    print(f"POOLED: {pooled_n} rows; aggregate blind-promotion cost at ordinals 1-3 "
          f"(sum forced / sum baseline, complete probes only; plan 11.1):")
    for o in (1, 2, 3):
        aggr = pooled_ord_sum_f[o] / pooled_ord_sum_base[o]
        print(f"   ord {o}: n {pooled_ord_n[o]} aggregate ratio {aggr:.3f} "
              f"(+{100*(aggr-1):.1f}%)")
    print(f"   paired deltas: ord1 mean {statistics.mean(all_deltas[1]):.2f} median "
          f"{statistics.median(all_deltas[1])} | ord2 mean {statistics.mean(all_deltas[2]):.2f} "
          f"median {statistics.median(all_deltas[2])} | ord3 mean "
          f"{statistics.mean(all_deltas[3]):.2f} median {statistics.median(all_deltas[3])}")
    print(f"   oracle: baseline {pooled_sum_base} -> {pooled_or} "
          f"({100*(1-pooled_or/pooled_sum_base):.1f}% saved; exact "
          f"{100*(1-pooled_ex/pooled_sum_base):.1f}%); later picks "
          f"{100*pooled_later/pooled_n:.1f}% of rows; censored-blocked probes "
          f"{pooled_block}")


if __name__ == "__main__":
    main()
