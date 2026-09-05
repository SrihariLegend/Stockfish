#!/usr/bin/env python3
"""Phase-6 shared-prefix interaction analysis (schema internal-counterfactual/5).

The /5 engine distinguishes two treatments:
  * variable-length controls [k, 0..k-1], then natural suffix; these must
    equal the /3 force-next probe for ordinal k bit-for-bit;
  * committed full top-K schedules (identity, reverse, rotation, adjacent
    swaps, ex-post scalar-cost order). Natural baseline is reported
    separately because it may dynamically skip a later quiet.

Every permutation must have order_valid=true and its observed slot ordinals
must be a prefix of its requested order. A cutoff before K targets is valid;
a non-prefix execution is not and is excluded/fails validation.

Cost claims use sums before division. Raw, fail-high-classification-preserving
and exact-value references are kept separate.
"""
import argparse
import gzip
import glob
import json
import os
import statistics


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def discover(paths):
    files = []
    for path in paths:
        files.extend(sorted(glob.glob(os.path.join(path, "*.jsonl.gz")))) \
            if os.path.isdir(path) else files.append(path)
    return sorted(set(files))


def measured_top4(row):
    ordinal = {c["move"]: c["ordinal"] for c in row["candidates"]}
    costs = {0: row["baseline"]["nodes"]}
    fail_high = {0: row["baseline"]["fail_high"]}
    values = {0: row["baseline"]["value"]}
    probes = {}
    for probe in row["probes"]:
        o = ordinal.get(probe["move"])
        if o is not None:
            probes[o] = probe
            if probe["completed"]:
                costs[o] = probe["nodes"]
                fail_high[o] = probe["fail_high"]
                values[o] = probe["value"]
    return costs, fail_high, values, probes


def expected_orders(row):
    K = min(4, row["n_candidates"])
    ident = list(range(K))
    costs, _, _, _ = measured_top4(row)
    out = {"identity": ident,
           "reverse": list(reversed(ident)),
           "rotation": ident[1:] + [0]}
    for k in range(1, K):
        out[f"control{k}"] = [k] + list(range(k))
    for i in range(1, min(3, K - 1)):
        order = ident.copy()
        order[i], order[i + 1] = order[i + 1], order[i]
        out[f"swap{i}{i + 1}"] = order
    out["cheapest_first"] = sorted(ident, key=lambda o: (costs[o], o))
    return out


def permutation_map(row):
    return {tuple(p["order"]): p for p in row["permutations"]}


def observed_prefix_valid(perm):
    order = perm["order"]
    got = [slot["ordinal"] for slot in perm.get("slots", [])[:len(order)]]
    return got == order[:len(got)] and perm.get("order_valid", False)


def validate_row(row):
    errors = []
    if row.get("schema") != "internal-counterfactual/5":
        errors.append(f"wrong schema {row.get('schema')}")
        return errors
    orders = [tuple(p["order"]) for p in row["permutations"]]
    if len(orders) != len(set(orders)):
        errors.append("duplicate orders")
    pmap = permutation_map(row)
    expected = expected_orders(row)
    _, _, _, probes = measured_top4(row)
    for name, order in expected.items():
        if tuple(order) not in pmap:
            # A data-dependent cheapest order may duplicate another battery
            # member; it still exists under the same tuple.
            errors.append(f"missing {name} {order}")
            continue
        perm = pmap[tuple(order)]
        if not perm["completed"]:
            errors.append(f"censored {name}")
        if not observed_prefix_valid(perm):
            errors.append(f"invalid prefix {name}")
        if name.startswith("control"):
            k = int(name[7:])
            probe = probes.get(k)
            if probe is None:
                errors.append(f"control{k} missing probe")
            elif any((perm["nodes"] != probe["nodes"],
                      perm["value"] != probe["value"],
                      perm["fail_high"] != probe["fail_high"])):
                errors.append(f"control{k} != probe")
    return errors


def save_pct(cost, baseline):
    return 100.0 * (1.0 - cost / baseline) if baseline else 0.0


def analyze_rows(rows, min_baseline_nodes=0):
    rows = [r for r in rows if r["baseline"]["nodes"] >= min_baseline_nodes]
    totals = {name: {"B": 0, "C": 0, "n": 0, "fhfl": 0, "flfh": 0}
              for name in ("identity", "reverse", "rotation", "swap12",
                           "swap23", "cheapest_first")}
    ref = {name: 0 for name in ("B", "scalar_unc", "scalar_cls", "scalar_exact",
                                "cheap", "cheap_cls_fallback", "cheap_exact_fallback",
                                "best", "best_cls", "best_exact")}
    validation_errors = []
    identity_differences = 0
    early_cutoff_prefixes = 0
    control_checks = 0
    for row in rows:
        errs = validate_row(row)
        if errs:
            validation_errors.append((row["sample_id"], errs))
            continue
        expected = expected_orders(row)
        pmap = permutation_map(row)
        control_checks += sum(name.startswith("control") for name in expected)
        costs, fh, values, _ = measured_top4(row)
        K = min(4, row["n_candidates"])
        base = row["baseline"]
        B = base["nodes"]
        ref["B"] += B
        ref["scalar_unc"] += min(costs[o] for o in range(K))
        ref["scalar_cls"] += min(costs[o] for o in range(K)
                                 if fh[o] == fh[0])
        ref["scalar_exact"] += min(costs[o] for o in range(K)
                                   if values[o] == values[0])

        full = {}
        for name in ("identity", "reverse", "rotation", "swap12", "swap23",
                     "cheapest_first"):
            if name not in expected:
                continue
            perm = pmap[tuple(expected[name])]
            full[name] = perm
            agg = totals[name]
            agg["B"] += B
            agg["C"] += perm["nodes"]
            agg["n"] += 1
            if perm["fail_high"] != base["fail_high"]:
                if base["fail_high"]:
                    agg["fhfl"] += 1
                else:
                    agg["flfh"] += 1
        early_cutoff_prefixes += sum(
            not p["fully_served"] for p in {id(p): p for p in full.values()}.values())
        identity = full["identity"]
        identity_differences += int(any((identity["nodes"] != B,
                                         identity["value"] != base["value"],
                                         identity["fail_high"] != base["fail_high"])))
        cheap = full["cheapest_first"]
        ref["cheap"] += cheap["nodes"]
        ref["cheap_cls_fallback"] += (cheap["nodes"]
                                       if cheap["fail_high"] == base["fail_high"] else B)
        ref["cheap_exact_fallback"] += (cheap["nodes"]
                                         if cheap["value"] == base["value"] else B)
        candidates = list({id(p): p for p in full.values()}.values())
        ref["best"] += min([B] + [p["nodes"] for p in candidates])
        ref["best_cls"] += min([B] + [p["nodes"] for p in candidates
                                      if p["fail_high"] == base["fail_high"]])
        ref["best_exact"] += min([B] + [p["nodes"] for p in candidates
                                        if p["value"] == base["value"]])
    return {"rows": len(rows), "valid_rows": len(rows) - len(validation_errors),
            "validation_errors": validation_errors,
            "identity_differences": identity_differences,
            "early_cutoff_prefixes": early_cutoff_prefixes,
            "control_checks": control_checks,
            "orders": totals, "references": ref}


def combine(reports):
    out = {"rows": 0, "valid_rows": 0, "validation_errors": [],
           "identity_differences": 0, "early_cutoff_prefixes": 0,
           "control_checks": 0,
           "orders": {}, "references": {}}
    for root, report in reports.items():
        for key in ("rows", "valid_rows", "identity_differences",
                    "early_cutoff_prefixes", "control_checks"):
            out[key] += report[key]
        out["validation_errors"].extend((root, *e) for e in report["validation_errors"])
        for name, agg in report["orders"].items():
            dst = out["orders"].setdefault(name,
                                            {"B": 0, "C": 0, "n": 0,
                                             "fhfl": 0, "flfh": 0})
            for key in dst:
                dst[key] += agg[key]
        for key, value in report["references"].items():
            out["references"][key] = out["references"].get(key, 0) + value
    return out


def print_report(name, report):
    print(f"== {name}")
    print(f"rows {report['rows']} valid {report['valid_rows']} "
          f"errors {len(report['validation_errors'])} committed_identity_diff "
          f"{report['identity_differences']} controls "
          f"{report['control_checks']}/{report['control_checks']} "
          f"early_cutoff_prefixes {report['early_cutoff_prefixes']}")
    for order, agg in report["orders"].items():
        if agg["n"]:
            print(f"  {order:18s} n={agg['n']:5d} ratio={agg['C']/agg['B']:.4f} "
                  f"save={save_pct(agg['C'], agg['B']):7.2f}% "
                  f"FH->FL={agg['fhfl']:3d} FL->FH={agg['flfh']:3d}")
    ref = report["references"]
    B = ref.get("B", 0)
    if B:
        print("  references:")
        for key in ("scalar_unc", "scalar_cls", "scalar_exact", "cheap",
                    "cheap_cls_fallback", "cheap_exact_fallback", "best",
                    "best_cls", "best_exact"):
            print(f"    {key:22s} {save_pct(ref[key], B):7.2f}% ({ref[key]})")
        print(f"    interaction gap raw      {100*(ref['cheap']-ref['best'])/B:7.2f}pp")
        print(f"    interaction gap class    "
              f"{100*(ref['cheap_cls_fallback']-ref['best_cls'])/B:7.2f}pp")
        print(f"    interaction gap exact    "
              f"{100*(ref['cheap_exact_fallback']-ref['best_exact'])/B:7.2f}pp")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json")
    args = parser.parse_args()
    files = discover(args.paths)
    reports = {}
    for path in files:
        rows = [r for r in load(path) if r.get("type") == "decision"]
        if rows:
            reports[path] = analyze_rows(rows)
            print_report(path, reports[path])
    pooled = combine(reports)
    print_report("POOLED", pooled)
    # Required plan-11.4 strata and root-clustered descriptive spread.
    strata = {}
    for threshold in (10, 25):
        sr = {}
        for path in files:
            rows = [r for r in load(path) if r.get("type") == "decision"]
            if rows:
                sr[path] = analyze_rows(rows, threshold)
        strata[str(threshold)] = combine(sr)
        print_report(f"POOLED baseline>={threshold}", strata[str(threshold)])
    root_best = [save_pct(r["references"]["best"], r["references"]["B"])
                 for r in reports.values() if r["references"]["B"]]
    if len(root_best) > 1:
        print(f"root best-order save mean {statistics.mean(root_best):.2f}% "
              f"sd {statistics.stdev(root_best):.2f}% "
              f"range {min(root_best):.2f}..{max(root_best):.2f}%")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"roots": reports, "pooled": pooled, "strata": strata},
                      fh, indent=1, sort_keys=True)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
