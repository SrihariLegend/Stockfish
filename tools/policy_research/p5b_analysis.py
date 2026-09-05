#!/usr/bin/env python3
"""Phase-6 readiness analysis over internal-counterfactual/3 evidence sets.

Reproduces the key tables of the p5-m2m3-reorder-v3 and p5-phase6-breadth
evidence READMEs from the raw JSONL.gz corpora, plus the pooled 12-root
headline numbers when the breadth directory AND the two canonical files are
passed together (the READMEs' pooled figures always span that 12-root set):

  per-root (and pooled) ordinal outcome/cost tables for the census ordinals
  0..3 (forced whole-node replay vs baseline: fail-high flips), the
  measurement-A summary (natural-cutoff ordinal distribution; forced
  candidate cutoff_by_first rates; separate accounting of fail highs that
  bypass the ordinary child search/cutoff hook), the classification-
  preserving local-oracle savings over ordinals 0..3 (summed, per plan
  §11.1), and the baseline-vs-live node determinism audit (join on
  sample_id).

Cost aggregation follows plan §11.1 ("sum costs before dividing, not by
naively averaging per-node percentages"): the headline cost change is the
ratio of SUMMED forced nodes to SUMMED baseline nodes. The arithmetic mean
of per-row ratios is reported separately as a purely descriptive per-visit
statistic and is explicitly not to be read as the aggregate cost multiplier
(tiny subtrees dominate it).

Usage:
  python3 p5b_analysis.py <dir-or-file>... [--measurement-a]
"""
import gzip
import glob
import json
import os
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
    return {
        "n": n, "fh": fh, "ffl": ffl, "flfh": flfh, "sum_base": sum_base,
        "sum_forced": sum_forced, "censored": censored,
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


def measurement_a(dec):
    """Baseline move-loop cutoff shape and forced own-cut rates.

    Returns a dict with, over the census ordinals:
      nat_loop / nat_ord0: move-loop cutoffs in the baseline and how many
        happened on the natural ordinal-0 move;
      fh_no_loop: baseline fail highs that bypassed the ordinary cutoff hook
        (fail_high && !cutoff_seen: pre-loop exits such as TT/razor/null/
        ProbCut when the replay never reaches a move loop are dropped before
        row writing; inside rows these are in-loop early returns such as the
        singular-extension/multi-cut path before the first child search);
      first_unsearched: rows where the slot-1 emission was not searched;
      own_cut: per ordinal, forced candidate cut off at slot 1.
    """
    rows = per_row(dec)
    res = {"nat_loop": 0, "nat_ord0": 0, "fh_no_loop": 0, "fl": 0,
           "first_unsearched": 0, "own_cut": {o: 0 for o in (1, 2, 3)}}
    for r, cand, pr in rows:
        b = r["baseline"]
        if b["fail_high"]:
            if b["cutoff"]["cutoff_seen"]:
                res["nat_loop"] += 1
                res["nat_ord0"] += int(b["cutoff"]["cutoff_ordinal"] == 0)
            else:
                res["fh_no_loop"] += 1
        else:
            res["fl"] += 1
        if not b["first"]["searched"]:
            res["first_unsearched"] += 1
        for o in (1, 2, 3):
            p = pr.get(o)
            if p and p["completed"] and p["cutoff"]["cutoff_by_first"]:
                res["own_cut"][o] += 1
    res["n"] = len(rows)
    return res


def mean_row_ratio(dec, o):
    vals = []
    for r, cand, pr in per_row(dec):
        p = pr.get(o)
        if p and p["completed"] and r["baseline"]["nodes"] > 0:
            vals.append(p["nodes"] / r["baseline"]["nodes"])
    return statistics.mean(vals) if vals else float("nan")


def main():
    args = sys.argv[1:]
    do_ma = "--measurement-a" in args
    if do_ma:
        args.remove("--measurement-a")
    files = []
    for a in args:
        files += sorted(glob.glob(a + "/*.jsonl.gz")) if os.path.isdir(a) else [a]
    files = sorted(set(files))
    if not files:
        print("no files found")
        return
    pooled_n = pooled_sum_base = pooled_sum_f = 0
    pooled_or = pooled_ex = pooled_later = pooled_block = 0
    pooled_ord_sum_base = {o: 0 for o in (0, 1, 2, 3)}
    pooled_ord_sum_f = {o: 0 for o in (0, 1, 2, 3)}
    pooled_ord_n = {o: 0 for o in (0, 1, 2, 3)}
    ma_pool = None
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
            mrow = mean_row_ratio(dec, o)
            print(f"   ord {o}: n {t['n'][o]} FH {100*t['fh'][o]/t['n'][o]:.1f}%  "
                  f"FH->FL {100*t['ffl'][o]/t['n'][o]:.1f}%  FL->FH {100*t['flfh'][o]/t['n'][o]:.1f}%  "
                  f"aggregate ratio {aggr:.3f} (+{100*(aggr-1):.1f}%)  "
                  f"mean row ratio {mrow:.3f}  censored {t['censored'][o]}")
        print(f"   oracle (classification-preserving): {bt} -> {ot} "
              f"({100*(1-ot/bt):.1f}% saved; later pick {100*later/len(dec):.1f}% "
              f"of rows; censored-blocked probes {block})")
        print(f"   oracle (exact-value): {bt_e} -> {ot_e} ({100*(1-ot_e/bt_e):.1f}% saved)")
        if do_ma:
            ma = measurement_a(dec)
            oc = [100 * ma["own_cut"][o] / len(dec) for o in (1, 2, 3)]
            print(f"   meas-A: move-loop cutoffs {ma['nat_ord0']}/{ma['nat_loop']} "
                  f"on ordinal 0; fail highs w/o cutoff hook {ma['fh_no_loop']}; "
                  f"fail lows {ma['fl']}; slot-1 unsearched {ma['first_unsearched']}; "
                  f"own-cut ord1..3 {oc}")
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
    print(f"POOLED: {pooled_n} rows; aggregate blind-promotion cost at ordinals 1-3 "
          f"(sum forced / sum baseline, complete probes only; plan 11.1):")
    for o in (1, 2, 3):
        aggr = pooled_ord_sum_f[o] / pooled_ord_sum_base[o]
        print(f"   ord {o}: n {pooled_ord_n[o]} aggregate ratio {aggr:.3f} "
              f"(+{100*(aggr-1):.1f}%)")
    print(f"   oracle: baseline {pooled_sum_base} -> {pooled_or} "
          f"({100*(1-pooled_or/pooled_sum_base):.1f}% saved; exact "
          f"{100*(1-pooled_ex/pooled_sum_base):.1f}%); later picks "
          f"{100*pooled_later/pooled_n:.1f}% of rows; censored-blocked probes "
          f"{pooled_block}")


if __name__ == "__main__":
    main()
