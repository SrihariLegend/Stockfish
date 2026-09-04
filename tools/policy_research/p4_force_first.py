#!/usr/bin/env python3
"""Phase 4 root-level counterfactual experiment tool (plan.md section 9).

Addresses Phase 4 review findings:
1. Tooling robustness (Finding 6): verifies engine banner and option presence
   at startup (fails fast if binary is not a POLICY_RESEARCH build); enforces
   Threads 1, Hash 16, MultiPV 1; token-based UCI parsing with assertion that
   target depth was actually reached; parses nodes, wall time (ms), score (cp/mate
   and bound), and PV.
2. Two distinct experimental estimands (Finding 2 & recommendation):
   - isolated (default, Experiment B): PolicyResearchForceFirstDepth == depth;
     depths 1..D-1 run under standard baseline conditions so TT and history state
     at the start of depth D are identical across all candidate interventions.
     Forcing the baseline's own best move at depth D is an exact no-op.
   - persistent (Experiment A): PolicyResearchForceFirstDepth == 0; overrides
     at all depths 1..D, capturing cumulative iterative-deepening trajectory churn.
3. Reference result & quality agreement (Finding 3, plan 9.4): runs a deeper
   reference search (default depth + 2) and checks both best-move agreement and
   score tolerance before classifying a candidate as quality-valid.
4. Candidate set framing (Finding 4): clarifies that the top-k candidate set is
   a search-informed empirical shortlist (upper bound for what a cheap policy can
   reach without search, lower bound on the all-legal oracle gap).
5. Test-root transparency (Finding 5): annotates test roots as burned/exploratory.

Usage:
  STOCKFISH_ENGINE=src/stockfish python3 tools/policy_research/p4_force_first.py \
      --depth 14 --reference-depth 16 --depth-mode isolated \
      --ids c1-d-001,c1-v-001,c1-t-001 --out /tmp/p4_isolated.json
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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
            "time_ms": None,
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
                if info and info.get("depth") == target_depth:
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
        score_agrees = False

        if r["score_type"] == "cp" and base["score_type"] == "cp":
            score_diff_base = r["score_val"] - base["score_val"]
        if r["score_type"] == "cp" and ref["score_type"] == "cp":
            score_diff_ref = r["score_val"] - ref["score_val"]
            score_agrees = abs(score_diff_ref) <= score_tolerance_cp
        elif r["score_type"] == ref["score_type"] and r["score_type"] == "mate":
            score_agrees = (r["score_val"] > 0) == (ref["score_val"] > 0)

        # Quality-valid candidate: agrees with reference best move and score tolerance
        quality_valid = best_agrees_ref and score_agrees

        rec = {
            "move": mv,
            "is_baseline_best": (mv == base["best"]),
            "nodes": r["nodes"],
            "time_ms": r["time_ms"],
            "score_type": r["score_type"],
            "score_val": r["score_val"],
            "score_bound": r["score_bound"],
            "best": r["best"],
            "pv": r["pv"],
            "node_delta_vs_base": r["nodes"] - base["nodes"],
            "node_ratio_vs_base": r["nodes"] / base["nodes"],
            "best_agrees_base": best_agrees_base,
            "best_agrees_ref": best_agrees_ref,
            "score_diff_base": score_diff_base,
            "score_diff_ref": score_diff_ref,
            "score_agrees": score_agrees,
            "quality_valid": quality_valid,
        }
        forced_records.append(rec)

    # Aggregates
    all_nodes = [c["nodes"] for c in forced_records]
    min_nodes_all = min(all_nodes)
    r_norm_all = (base["nodes"] - min_nodes_all) / base["nodes"]

    valid_records = [c for c in forced_records if c["quality_valid"]]
    if valid_records:
        min_nodes_valid = min(c["nodes"] for c in valid_records)
        cheapest_valid_mv = next(c["move"] for c in valid_records if c["nodes"] == min_nodes_valid)
        r_norm_valid = (base["nodes"] - min_nodes_valid) / base["nodes"]
        valid_times = [c["time_ms"] for c in valid_records if c["time_ms"] is not None]
        min_time_valid = min(valid_times) if valid_times else None
        r_norm_time = (
            (base["time_ms"] - min_time_valid) / base["time_ms"]
            if base["time_ms"] and min_time_valid is not None
            else None
        )
    else:
        min_nodes_valid = None
        cheapest_valid_mv = None
        r_norm_valid = None
        min_time_valid = None
        r_norm_time = None

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
            "time_ms": base["time_ms"],
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
            "min_nodes_all": min_nodes_all,
            "r_norm_all": r_norm_all,
            "valid_candidate_count": len(valid_records),
            "min_nodes_quality_valid": min_nodes_valid,
            "cheapest_quality_valid_move": cheapest_valid_mv,
            "r_norm_quality_valid": r_norm_valid,
            "r_norm_time": r_norm_time,
        },
    }


def print_root_report(res: dict):
    b = res["base"]
    ref = res["reference"]
    s = res["summary"]
    burned_note = " [EXPLORATORY / BURNED TEST ROOT]" if res["is_burned_test_root"] else ""
    print(f"\n================================================================================")
    print(f"Root: {res['root']} ({res['set']}){burned_note} | Depth Mode: {res['depth_mode'].upper()}")
    print(f"Baseline @ D{res['depth']}: {b['nodes']:,} nodes | {b['time_ms']} ms | score {b['score_type']} {b['score_val']} ({b['score_bound']}) | best: {b['best']}")
    print(f"Reference @ D{ref['depth']}: {ref['nodes']:,} nodes | {ref['time_ms']} ms | score {ref['score_type']} {ref['score_val']} | best: {ref['best']}")
    print(f"--------------------------------------------------------------------------------")
    print(f"{'Move':<8} {'Nodes':>10} {'Delta':>10} {'Ratio':>7} {'Time':>6} {'Score':>12} {'Best':>8} {'Agree':>8} {'Quality':>8}")
    print(f"--------------------------------------------------------------------------------")
    for c in res["candidates"]:
        star = " *" if c["is_baseline_best"] else "  "
        score_str = f"{c['score_type']} {c['score_val']}"
        agree_str = f"{'B' if c['best_agrees_base'] else '-'}{'R' if c['best_agrees_ref'] else '-'}{'S' if c['score_agrees'] else '-'}"
        qual_str = "PASS" if c["quality_valid"] else "FAIL"
        delta_str = f"{c['node_delta_vs_base']:+d}"
        print(f"{c['move'] + star:<8} {c['nodes']:>10,d} {delta_str:>10} {c['node_ratio_vs_base']:>7.2f} {c['time_ms']:>4}ms {score_str:>12} {c['best']:>8} {agree_str:>8} {qual_str:>8}")
    print(f"--------------------------------------------------------------------------------")
    print(f"Unconstrained min:  {s['min_nodes_all']:,} nodes  |  R_norm (all):   {s['r_norm_all']:+.3f}")
    if s['min_nodes_quality_valid'] is not None:
        print(f"Quality-valid min:  {s['min_nodes_quality_valid']:,} nodes ({s['cheapest_quality_valid_move']})  |  R_norm (valid): {s['r_norm_quality_valid']:+.3f}  |  R_norm (time): {s['r_norm_time']:+.3f}")
    else:
        print(f"Quality-valid min:  NONE PASSED QUALITY GATES")
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

    if args.out:
        out_data = {
            "schema": "policy-research-p4-counterfactual/1",
            "engine_banner": eng.banner,
            "depth_mode": args.depth_mode,
            "depth": args.depth,
            "reference_depth": args.reference_depth,
            "candidate_depth": args.candidate_depth,
            "k": args.k,
            "score_tolerance_cp": args.score_tolerance,
            "results": all_results,
        }
        Path(args.out).write_text(json.dumps(out_data, indent=1) + "\n")
        print(f"\nArtifact saved to {args.out}")


if __name__ == "__main__":
    main()
