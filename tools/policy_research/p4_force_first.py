#!/usr/bin/env python3
"""Phase 4 root-level counterfactual pilot: force-first root ordering.

Runs a POLICY_RESEARCH build of the engine (plan.md section 9) in mode
RootCounterfactual with PolicyResearchForceFirstMove set to each candidate in
turn, one fresh process per intervention (plan 9.2 common state), and reports
the fixed-depth root node cost C(r | m first) against the baseline cost
C_base(r) plus best-move agreement with the baseline.

The candidate set for this pilot is the engine's own top-k MultiPV moves at a
shallower depth: that is the *plausible* reorder space, and min over it is the
plan-9.5 oracle-gap upper bound on ordering savings (not a wall-time or
reference-quality study - those are later P4.2+ increments).

Usage:
  STOCKFISH_ENGINE=src/stockfish python3 tools/policy_research/p4_force_first.py \
      --depth 14 --k 4 --ids c1-d-001,c1-v-001,c1-t-001 \
      --out /tmp/p4kickoff.json

Only fixed depth is compared; every run is a fresh process with Hash 16,
Threads 1, MultiPV 1 (candidate listing uses a separate fresh process).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_corpus as rc


class Engine:
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())

    def _talk(self, commands, wait_bestmove):
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
        out = {"nodes": None, "best": None, "infos": []}
        try:
            p.stdin.write("uci\n")
            for c in commands:
                p.stdin.write(c + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("info string research"):
                    out["infos"].append(line)
                if wait_bestmove:
                    m = re.match(r"info depth (\d+) .*? nodes (\d+)", line)
                    if m:
                        out["nodes"] = int(m.group(2))  # last reported depth
                    if line.startswith("bestmove"):
                        out["best"] = line.split()[1]
                        break
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                p.stdout.close()
            except OSError:
                pass
            p.wait()
        return out

    def top_k(self, fen, depth, k):
        cmds = ["setoption name MultiPV value %d" % k,
                "isready", "position fen %s" % fen,
                "go depth %d" % depth]
        seen = {}
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            p.stdin.write("uci\n")
            for c in cmds:
                p.stdin.write(c + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                m = re.search(r"multipv (\d+) .*? pv (\S+)", line)
                if m:
                    seen[int(m.group(1))] = m.group(2)
                if line.startswith("bestmove"):
                    break
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                p.stdout.close()
            except OSError:
                pass
            p.wait()
        return [seen[i] for i in sorted(seen)]

    def baseline(self, fen, depth):
        cmds = ["setoption name Hash value 16",
                "setoption name PolicyResearch value on",
                "setoption name PolicyResearchMode value root_counterfactual",
                "isready", "position fen %s" % fen, "go depth %d" % depth]
        return self._talk(cmds, True)

    def force(self, fen, depth, move):
        cmds = ["setoption name Hash value 16",
                "setoption name PolicyResearch value on",
                "setoption name PolicyResearchMode value root_counterfactual",
                "setoption name PolicyResearchForceFirstMove value %s" % move,
                "isready", "position fen %s" % fen, "go depth %d" % depth]
        return self._talk(cmds, True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=None)
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--candidate-depth", type=int, default=10)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--ids", default="c1-d-001,c1-v-001,c1-t-001")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    engine = args.engine or os.environ.get("STOCKFISH_ENGINE")
    if not engine:
        sys.exit("set STOCKFISH_ENGINE or pass --engine")
    eng = Engine(engine)

    corpus_path = Path(__file__).resolve().parents[0] / "corpora" / "corpus-v1.json"
    corpus, _ = rc.load_corpus(str(corpus_path))
    by_id = {p["id"]: p for p in corpus["positions"]}
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]

    results = []
    for rid in ids:
        fen = by_id[rid]["fen"]
        base = eng.baseline(fen, args.depth)
        cands = eng.top_k(fen, args.candidate_depth, args.k)
        forced = []
        for mv in cands:
            r = eng.force(fen, args.depth, mv)
            forced.append({"move": mv, "nodes": r["nodes"], "best": r["best"]})
        cmin = min((c["nodes"] for c in forced if c["nodes"] is not None),
                   default=None)
        rec = {
            "root": rid, "set": by_id[rid]["set"], "fen": fen,
            "depth": args.depth, "candidate_depth": args.candidate_depth,
            "base_nodes": base["nodes"], "base_best": base["best"],
            "candidates": forced,
            "oracle_nodes_min_over_candidates": cmin,
            "r_norm": None if (base["nodes"] and cmin is not None)
                     else (base["nodes"] - cmin) / base["nodes"]
                     if base["nodes"] else None,
            "best_agreement_with_base": [c["move"] for c in forced
                                         if c["best"] == base["best"]],
        }
        if base["nodes"] and cmin is not None:
            rec["r_norm"] = (base["nodes"] - cmin) / base["nodes"]
        results.append(rec)
        print("root %-10s base_nodes=%8d best=%s" % (rid, base["nodes"],
                                                     base["best"]))
        for c in forced:
            print("   force %-6s nodes=%8d best=%s" % (c["move"], c["nodes"],
                                                       c["best"]))
        print("   min-over-candidates=%s r_norm=%.3f"
              % (cmin, rec["r_norm"] or 0.0))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()
