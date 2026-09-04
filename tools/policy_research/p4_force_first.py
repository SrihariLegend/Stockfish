#!/usr/bin/env python3
"""Phase 4 root-level counterfactual experiment tool (plan.md section 9).

Addresses Phase 4 expert review findings:
1. MovePicker root mechanics (Finding 1): Stockfish sets ttData.move = rootMoves[0]
   and emits it first in MAIN_TT. Subsequent moves follow MovePicker stages.
2. Two distinct experimental estimands (Finding 2):
   - isolated (default, Experiment B): PolicyResearchForceFirstDepth == depth;
     depths 1..D-1 run under standard baseline conditions so TT and history state
     at the start of depth D are identical across all candidate interventions.
     Forcing the baseline's own best move at depth D is an exact no-op.
   - persistent (Experiment A): PolicyResearchForceFirstDepth == 0; overrides
     at all depths 1..D, capturing cumulative iterative-deepening trajectory churn.
3. Disentangled time metrics & jitter caveat (Finding 3): reports wall time of
   the node-optimal candidate separately from the overall time-optimal candidate;
   warns that single-run millisecond times are subject to scheduling jitter.
4. Continuous score deltas & multi-band tolerance (Finding 4): records exact
   score difference vs baseline and reference; classifies deviations into
   exact, within tolerance, mild drift (near boundary), and score collapse.
5. Incremental vs cumulative node costs (Finding 5): records depth-(D-1) node
   baseline to report both cumulative (search-to-depth-D) and incremental (depth-D
   decision only) node savings.
6. Candidate set framing (Finding 6): clarifies that the top-k candidate set is
   a search-informed empirical shortlist (oracle-gap proxy, not deployable policy).
7. Test-root transparency (Finding 7): marks c1-t-001 as burned/exploratory.
8. Durable provenance (Finding 8): records engine banner, embedded commit, tool
   commit, and worktree status; writes versioned artifacts.

Usage:
  STOCKFISH_ENGINE=/tmp/stockfish-research-clean python3 tools/policy_research/p4_force_first.py \
      --depth 14 --reference-depth 16 --depth-mode isolated \
      --ids c1-d-001,c1-v-001,c1-t-001 --out /tmp/p4_isolated.json
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_corpus as rc


def parse_info_line(line: str):
    """Tokenize and parse a standard Stockfish UCI info line."""
    parts = line.split()
    if not parts or parts[0] != "info":
        return None
    d = {}
    i = 1
    while i < len(parts):
        key = parts[i]
        if key == "depth" and i + 1 < len(parts):
            d["depth"] = int(parts[i + 1])
            i += 2
        elif key == "seldepth" and i + 1 < len(parts):
            d["seldepth"] = int(parts[i + 1])
            i += 2
        elif key == "multipv" and i + 1 < len(parts):
            d["multipv"] = int(parts[i + 1])
            i += 2
        elif key == "score" and i + 2 < len(parts):
            d["score_type"] = parts[i + 1]  # "cp" or "mate"
            d["score_val"] = int(parts[i + 2])
            i += 3
            if i < len(parts) and parts[i] in ("lowerbound", "upperbound"):
                d["score_bound"] = parts[i]
                i += 1
            else:
                d["score_bound"] = "exact"
        elif key == "nodes" and i + 1 < len(parts):
            d["nodes"] = int(parts[i + 1])
            i += 2
        elif key == "nps" and i + 1 < len(parts):
            d["nps"] = int(parts[i + 1])
            i += 2
        elif key == "time" and i + 1 < len(parts):
            d["time_ms"] = int(parts[i + 1])
            i += 2
        elif key == "hashfull" and i + 1 < len(parts):
            d["hashfull"] = int(parts[i + 1])
            i += 2
        elif key == "tbhits" and i + 1 < len(parts):
            d["tbhits"] = int(parts[i + 1])
            i += 2
        elif key == "pv":
            d["pv"] = " ".join(parts[i + 1 :])
            break
        else:
            i += 1
    return d


def extract_engine_embedded_commit(banner: str | None) -> str | None:
    if not banner:
        return None
    m = re.search(r"dev-\d{8}-([0-9a-f]{7,12})", banner)
    return m.group(1) if m else None


class Engine:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())
        self.banner = ""
        self.has_force_move = False
        self.has_force_depth = False
        self._probe()

    def _probe(self):
        p = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        out = ""
        try:
            p.stdin.write("uci\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                out += line
                if line.startswith("id name"):
                    self.banner = line.strip()[8:].strip()
                if line.strip() == "uciok":
                    break
        finally:
            try:
                if p.stdin:
                    p.stdin.write("quit\n")
                    p.stdin.flush()
                    p.stdin.close()
            except OSError:
                pass
            try:
                if p.stdout:
                    p.stdout.close()
            except OSError:
                pass
            p.wait()

        self.has_force_move = "option name PolicyResearchForceFirstMove" in out
        self.has_force_depth = "option name PolicyResearchForceFirstDepth" in out
        if not (self.has_force_move and self.has_force_depth):
            raise RuntimeError(
                f"Engine at '{self.path}' ({self.banner}) does not expose required "
                f"research options (ForceFirstMove: {self.has_force_move}, "
                f"ForceFirstDepth: {self.has_force_depth}). "
                f"Did you build with 'make research-build'?"
            )

    def _talk(self, commands, target_depth: int):
        p = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        res = {
            "depth": None,
            "nodes": None,
            "prev_depth_nodes": 0,
            "time_ms": None,
            "prev_depth_time_ms": 0,
            "score_type": None,
            "score_val": None,
            "score_bound": None,
            "seldepth": None,
            "nps": None,
            "pv": None,
            "best": None,
            "ponder": None,
            "info_strings": [],
        }
        try:
            p.stdin.write("uci\n")
            p.stdin.write("setoption name Threads value 1\n")
            p.stdin.write("setoption name Hash value 16\n")
            p.stdin.write("setoption name MultiPV value 1\n")
            for c in commands:
                p.stdin.write(c + "\n")
            p.stdin.flush()

            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("info string research"):
                    res["info_strings"].append(line)
                info = parse_info_line(line)
                if info:
                    if info.get("depth") == target_depth - 1:
                        res["prev_depth_nodes"] = info.get("nodes", 0)
                        res["prev_depth_time_ms"] = info.get("time_ms", 0)
                    elif info.get("depth") == target_depth:
                        res.update(info)
                if line.startswith("bestmove"):
                    parts = line.split()
                    res["best"] = parts[1]
                    if len(parts) >= 4 and parts[2] == "ponder":
                        res["ponder"] = parts[3]
                    break
        finally:
            try:
                if p.stdin:
                    p.stdin.write("quit\n")
                    p.stdin.flush()
                    p.stdin.close()
            except OSError:
                pass
            try:
                if p.stdout:
                    p.stdout.close()
            except OSError:
                pass
            p.wait()

        if res["nodes"] is None or res["best"] is None:
            raise RuntimeError(
                f"Search failed to produce results at target depth {target_depth}. "
                f"Last state: {res}"
            )
        res["incremental_nodes"] = max(0, res["nodes"] - (res["prev_depth_nodes"] or 0))
        res["incremental_time_ms"] = max(0, (res["time_ms"] or 0) - (res["prev_depth_time_ms"] or 0))
        return res

    def top_k(self, fen: str, depth: int, k: int):
        p = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        seen = {}
        try:
            p.stdin.write("uci\n")
            p.stdin.write("setoption name Threads value 1\n")
            p.stdin.write("setoption name Hash value 16\n")
            p.stdin.write("setoption name MultiPV value %d\n" % k)
            p.stdin.write("isready\n")
            p.stdin.write("position fen %s\n" % fen)
            p.stdin.write("go depth %d\n" % depth)
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                info = parse_info_line(line)
                if info and "multipv" in info and "pv" in info:
                    pv_first = info["pv"].split()[0]
                    seen[info["multipv"]] = pv_first
                if line.startswith("bestmove"):
                    break
        finally:
            try:
                if p.stdin:
                    p.stdin.write("quit\n")
                    p.stdin.flush()
                    p.stdin.close()
            except OSError:
                pass
            try:
                if p.stdout:
                    p.stdout.close()
            except OSError:
                pass
            p.wait()
        return [seen[i] for i in sorted(seen) if i in seen]

    def run_search(self, fen: str, depth: int, force_move: str = "", force_depth: int = 0):
        cmds = [
            "setoption name PolicyResearch value on",
            "setoption name PolicyResearchMode value root_counterfactual",
        ]
        if force_move:
            cmds.append(f"setoption name PolicyResearchForceFirstMove value {force_move}")
            cmds.append(f"setoption name PolicyResearchForceFirstDepth value {force_depth}")
        cmds.extend(["isready", f"position fen {fen}", f"go depth {depth}"])
        return self._talk(cmds, depth)


def evaluate_root(
    eng: Engine,
    root_id: str,
    root_info: dict,
    depth: int,
    ref_depth: int,
    candidate_depth: int,
    k: int,
    depth_mode: str,
    score_tolerance_cp: int,
):
    fen = root_info["fen"]
    root_set = root_info["set"]
    is_burned = root_id == "c1-t-001"

    # 1. Baseline search at depth D
    base = eng.run_search(fen, depth)

    # 2. Reference search at ref_depth (clean baseline, no override)
    ref = eng.run_search(fen, ref_depth)

    # 3. Candidate shortlist from depth candidate_depth MultiPV
    candidates = eng.top_k(fen, candidate_depth, k)
    if base["best"] not in candidates:
        candidates.insert(0, base["best"])

    forced_records = []
    for mv in candidates:
        force_depth = depth if depth_mode == "isolated" else 0
        r = eng.run_search(fen, depth, force_move=mv, force_depth=force_depth)

        # Quality evaluation (plan 9.4)
        best_agrees_base = r["best"] == base["best"]
        best_agrees_ref = r["best"] == ref["best"]

        score_diff_base = None
        score_diff_ref = None

        if r["score_type"] == "cp" and base["score_type"] == "cp":
            score_diff_base = r["score_val"] - base["score_val"]
        if r["score_type"] == "cp" and ref["score_type"] == "cp":
            score_diff_ref = r["score_val"] - ref["score_val"]

        # Classification of score stability
        if score_diff_ref is None:
            score_status = "mate_eval"
            score_agrees = (r["score_type"] == ref["score_type"])
        else:
            diff_abs = abs(score_diff_ref)
            if diff_abs <= 25:
                score_status = "exact_ref"
                score_agrees = True
            elif diff_abs <= score_tolerance_cp:
                score_status = "within_tolerance"
                score_agrees = True
            elif diff_abs <= 100:
                score_status = "mild_drift"
                score_agrees = False
            else:
                score_status = "score_collapse"
                score_agrees = False

        # Quality-valid candidate: agrees with reference best move and score tolerance
        quality_valid = best_agrees_ref and score_agrees

        inc_nodes = r["incremental_nodes"]
        base_inc_nodes = base["incremental_nodes"]
        inc_delta = inc_nodes - base_inc_nodes
        inc_ratio = (inc_nodes / base_inc_nodes) if base_inc_nodes > 0 else 1.0

        rec = {
            "move": mv,
            "is_baseline_best": (mv == base["best"]),
            "nodes": r["nodes"],
            "incremental_nodes": inc_nodes,
            "prev_depth_nodes": r["prev_depth_nodes"],
            "time_ms": r["time_ms"],
            "incremental_time_ms": r["incremental_time_ms"],
            "score_type": r["score_type"],
            "score_val": r["score_val"],
            "score_bound": r["score_bound"],
            "best": r["best"],
            "pv": r["pv"],
            "node_delta_vs_base": r["nodes"] - base["nodes"],
            "node_ratio_vs_base": r["nodes"] / base["nodes"],
            "incremental_delta_vs_base": inc_delta,
            "incremental_ratio_vs_base": inc_ratio,
            "best_agrees_base": best_agrees_base,
            "best_agrees_ref": best_agrees_ref,
            "score_diff_base": score_diff_base,
            "score_diff_ref": score_diff_ref,
            "score_status": score_status,
            "score_agrees": score_agrees,
            "quality_valid": quality_valid,
        }
        forced_records.append(rec)

    # Aggregates across all candidates
    all_nodes = [c["nodes"] for c in forced_records]
    min_nodes_all = min(all_nodes)
    r_norm_all_nodes = (base["nodes"] - min_nodes_all) / base["nodes"]

    valid_records = [c for c in forced_records if c["quality_valid"]]
    if valid_records:
        # Node-optimal quality-valid candidate
        cheapest_nodes_rec = min(valid_records, key=lambda c: c["nodes"])
        cheapest_nodes_mv = cheapest_nodes_rec["move"]
        min_nodes_valid = cheapest_nodes_rec["nodes"]
        r_norm_nodes_valid = (base["nodes"] - min_nodes_valid) / base["nodes"]

        # Incremental nodes at node-optimal candidate
        base_inc = base["incremental_nodes"]
        opt_inc = cheapest_nodes_rec["incremental_nodes"]
        min_inc_nodes_valid = opt_inc
        r_norm_inc_nodes_valid = (
            (base_inc - opt_inc) / base_inc if base_inc > 0 else 0.0
        )

        # Wall time at node-optimal candidate
        time_at_min_nodes = cheapest_nodes_rec["time_ms"]
        r_norm_time_at_min_nodes = (
            (base["time_ms"] - time_at_min_nodes) / base["time_ms"]
            if base["time_ms"] and time_at_min_nodes is not None
            else None
        )

        # Time-optimal candidate (reported separately to avoid conflation)
        cheapest_time_rec = min(valid_records, key=lambda c: c["time_ms"])
        cheapest_time_mv = cheapest_time_rec["move"]
        min_time_valid = cheapest_time_rec["time_ms"]
        r_norm_time_min = (
            (base["time_ms"] - min_time_valid) / base["time_ms"]
            if base["time_ms"] and min_time_valid is not None
            else None
        )
    else:
        cheapest_nodes_mv = None
        min_nodes_valid = None
        r_norm_nodes_valid = None
        min_inc_nodes_valid = None
        r_norm_inc_nodes_valid = None
        time_at_min_nodes = None
        r_norm_time_at_min_nodes = None
        cheapest_time_mv = None
        min_time_valid = None
        r_norm_time_min = None

    return {
        "root": root_id,
        "set": root_set,
        "is_burned_test_root": is_burned,
        "depth": depth,
        "reference_depth": ref_depth,
        "candidate_depth": candidate_depth,
        "depth_mode": depth_mode,
        "base": {
            "nodes": base["nodes"],
            "incremental_nodes": base["incremental_nodes"],
            "prev_depth_nodes": base["prev_depth_nodes"],
            "time_ms": base["time_ms"],
            "incremental_time_ms": base["incremental_time_ms"],
            "score_type": base["score_type"],
            "score_val": base["score_val"],
            "score_bound": base["score_bound"],
            "best": base["best"],
            "pv": base["pv"],
        },
        "reference": {
            "depth": ref_depth,
            "nodes": ref["nodes"],
            "time_ms": ref["time_ms"],
            "score_type": ref["score_type"],
            "score_val": ref["score_val"],
            "best": ref["best"],
            "pv": ref["pv"],
        },
        "candidates": forced_records,
        "summary": {
            "candidate_count": len(forced_records),
            "valid_candidate_count": len(valid_records),
            "min_nodes_all": min_nodes_all,
            "r_norm_all_nodes": r_norm_all_nodes,
            "node_optimal": {
                "move": cheapest_nodes_mv,
                "nodes": min_nodes_valid,
                "r_norm_cumulative_nodes": r_norm_nodes_valid,
                "incremental_nodes": min_inc_nodes_valid,
                "r_norm_incremental_nodes": r_norm_inc_nodes_valid,
                "time_ms": time_at_min_nodes,
                "r_norm_time": r_norm_time_at_min_nodes,
            },
            "time_optimal": {
                "move": cheapest_time_mv,
                "time_ms": min_time_valid,
                "r_norm_time": r_norm_time_min,
            },
        },
    }


def print_root_report(res: dict):
    b = res["base"]
    ref = res["reference"]
    s = res["summary"]
    opt_node = s["node_optimal"]
    opt_time = s["time_optimal"]
    burned_note = " [EXPLORATORY / BURNED TEST ROOT]" if res["is_burned_test_root"] else ""
    print(f"\n================================================================================")
    print(f"Root: {res['root']} ({res['set']}){burned_note} | Depth Mode: {res['depth_mode'].upper()}")
    print(f"Baseline @ D{res['depth']}: {b['nodes']:,} nodes ({b['incremental_nodes']:,} incr) | {b['time_ms']} ms | score {b['score_type']} {b['score_val']} ({b['score_bound']}) | best: {b['best']}")
    print(f"Reference @ D{ref['depth']}: {ref['nodes']:,} nodes | {ref['time_ms']} ms | score {ref['score_type']} {ref['score_val']} | best: {ref['best']}")
    print(f"--------------------------------------------------------------------------------")
    print(f"{'Move':<8} {'Nodes(Cum)':>11} {'Delta(Cum)':>11} {'Nodes(Inc)':>11} {'Time':>6} {'Score(Δref)':>17} {'Best':>8} {'Agree':>7} {'Quality':>10}")
    print(f"--------------------------------------------------------------------------------")
    for c in res["candidates"]:
        star = " *" if c["is_baseline_best"] else "  "
        score_diff_str = f"{c['score_diff_ref']:+d}cp" if c['score_diff_ref'] is not None else "n/a"
        score_str = f"{c['score_type']} {c['score_val']:>3} ({score_diff_str:>6})"
        agree_str = f"{'B' if c['best_agrees_base'] else '-'}{'R' if c['best_agrees_ref'] else '-'}{'S' if c['score_agrees'] else '-'}"
        if c["quality_valid"]:
            qual_str = "PASS"
        elif c["score_status"] == "mild_drift":
            qual_str = "DRIFT"
        elif c["score_status"] == "score_collapse":
            qual_str = "COLLAPSE"
        else:
            qual_str = "FAIL_BEST"
        delta_str = f"{c['node_delta_vs_base']:+d}"
        print(f"{c['move'] + star:<8} {c['nodes']:>11,d} {delta_str:>11} {c['incremental_nodes']:>11,d} {c['time_ms']:>4}ms {score_str:>17} {c['best']:>8} {agree_str:>7} {qual_str:>10}")
    print(f"--------------------------------------------------------------------------------")
    print(f"Unconstrained min:  {s['min_nodes_all']:,} nodes  |  R_norm (cumulative): {s['r_norm_all_nodes']:+.3f}")
    if opt_node['move'] is not None:
        print(f"Quality-valid node-optimal:  {opt_node['move']}  |  Cumulative: {opt_node['nodes']:,} nodes (R_norm: {opt_node['r_norm_cumulative_nodes']:+.3f})")
        print(f"                             Incremental D{res['depth']}: {opt_node['incremental_nodes']:,} nodes (R_norm: {opt_node['r_norm_incremental_nodes']:+.3f})")
        print(f"                             Time at node-optimal: {opt_node['time_ms']} ms (R_norm: {opt_node['r_norm_time']:+.3f})")
        if opt_time['move'] != opt_node['move']:
            print(f"Separate time-optimal move:  {opt_time['move']}  |  Time: {opt_time['time_ms']} ms (R_norm: {opt_time['r_norm_time']:+.3f})")
    else:
        print(f"Quality-valid min:  NONE PASSED QUALITY GATES")
    print(f"Timing caveat: single-run wall times at millisecond resolution (5-35 ms range) are subject to system scheduling jitter.")
    print(f"================================================================================")


def main():
    ap = argparse.ArgumentParser(description="Phase 4 root-level counterfactual experiment driver.")
    ap.add_argument("--engine", default=None, help="Path to research Stockfish binary.")
    ap.add_argument("--depth", type=int, default=14, help="Target search depth.")
    ap.add_argument("--reference-depth", type=int, default=16, help="Reference search depth (plan 9.4).")
    ap.add_argument("--candidate-depth", type=int, default=10, help="MultiPV depth for candidate shortlist.")
    ap.add_argument("--k", type=int, default=4, help="Number of MultiPV candidates.")
    ap.add_argument(
        "--depth-mode",
        choices=["isolated", "persistent"],
        default="isolated",
        help="isolated: override only at target depth (Experiment B). persistent: override all depths (Experiment A).",
    )
    ap.add_argument("--score-tolerance", type=int, default=50, help="Max score cp delta vs reference.")
    ap.add_argument("--ids", default="c1-d-001,c1-v-001,c1-t-001", help="Comma-separated root IDs.")
    ap.add_argument("--out", default=None, help="Output JSON path.")
    args = ap.parse_args()

    engine_path = args.engine or os.environ.get("STOCKFISH_ENGINE")
    if not engine_path:
        sys.exit("Error: must set STOCKFISH_ENGINE or pass --engine")

    eng = Engine(engine_path)

    corpus_path = Path(__file__).resolve().parents[0] / "corpora" / "corpus-v1.json"
    corpus, _ = rc.load_corpus(str(corpus_path))
    by_id = {p["id"]: p for p in corpus["positions"]}
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]

    all_results = []
    for rid in ids:
        if rid not in by_id:
            print(f"Skipping unknown root ID {rid}")
            continue
        res = evaluate_root(
            eng=eng,
            root_id=rid,
            root_info=by_id[rid],
            depth=args.depth,
            ref_depth=args.reference_depth,
            candidate_depth=args.candidate_depth,
            k=args.k,
            depth_mode=args.depth_mode,
            score_tolerance_cp=args.score_tolerance,
        )
        all_results.append(res)
        print_root_report(res)

    git_st = rc.git_state(rc.repo_root())
    provenance = {
        "engine_path": eng.path,
        "engine_banner": eng.banner,
        "engine_embedded_commit": extract_engine_embedded_commit(eng.banner),
        "tool_commit": git_st["commit_short"],
        "tool_dirty": git_st["dirty"],
        "tool_dirty_file_count": git_st["dirty_file_count"],
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    out_data = {
        "schema": "policy-research-p4-counterfactual/2",
        "provenance": provenance,
        "depth_mode": args.depth_mode,
        "depth": args.depth,
        "reference_depth": args.reference_depth,
        "candidate_depth": args.candidate_depth,
        "k": args.k,
        "score_tolerance_cp": args.score_tolerance,
        "candidate_set_framing": "empirical search-informed candidate shortlist from MultiPV at candidate_depth",
        "results": all_results,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(out_data, indent=1) + "\n")
        print(f"\nArtifact saved to {args.out}")


if __name__ == "__main__":
    main()
