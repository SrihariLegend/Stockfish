#!/usr/bin/env python3
"""Paired fixed-depth wall-time benchmark for the research live QE policy."""
import argparse
import json
import random
import re
import statistics
import subprocess
import time


INFO_RE = re.compile(r"\bdepth (\d+).*?\bscore (cp|mate) (-?\d+).*?\bnodes (\d+)")
STATS_RE = re.compile(
    r"calls (\d+) candidates (\d+) promotions (\d+)(?: prepare_failures (\d+))?")


def send(proc, command):
    proc.stdin.write(command + "\n")
    proc.stdin.flush()


def wait_for(proc, prefix):
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"engine ended waiting for {prefix}")
        if line.startswith(prefix):
            return line.strip()


def one_run(engine, fen, depth, mode):
    proc = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        send(proc, "uci"); wait_for(proc, "uciok")
        for command in ("setoption name Threads value 1",
                        "setoption name Hash value 16",
                        "setoption name PolicyResearch value on",
                        "setoption name PolicyResearchMode value internal_counterfactual",
                        f"setoption name PolicyResearchLiveQE value {'off' if mode == 'off' else 'on'}",
                        f"setoption name PolicyResearchLiveQEMinDepth value {0 if mode == 'off' else int(mode)}"):
            send(proc, command)
        send(proc, "isready"); wait_for(proc, "readyok")
        # Warm the network/code pages outside the timed interval.
        send(proc, "position startpos")
        send(proc, "go depth 6")
        wait_for(proc, "bestmove")
        send(proc, "ucinewgame")
        send(proc, "setoption name Clear Hash")
        send(proc, "isready"); wait_for(proc, "readyok")
        send(proc, "position fen " + fen)
        send(proc, "policy_research_live_qe_stats reset")
        wait_for(proc, "LIVE_QE_STATS")
        started = time.perf_counter_ns()
        send(proc, f"go depth {depth}")
        last = None
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("engine ended before bestmove")
            match = INFO_RE.search(line)
            if match and int(match.group(1)) == depth:
                last = (match.group(2), int(match.group(3)), int(match.group(4)), line.strip())
            if line.startswith("bestmove"):
                bestmove = line.split()[1]
                break
        elapsed_ns = time.perf_counter_ns() - started
        if last is None:
            raise RuntimeError("no exact-depth info line")
        send(proc, "policy_research_live_qe_stats")
        stats_line = wait_for(proc, "LIVE_QE_STATS")
        stats_match = STATS_RE.search(stats_line)
        send(proc, "quit"); proc.wait(timeout=30)
        return {"wall_ns": elapsed_ns, "score_type": last[0], "score": last[1],
                "nodes": last[2], "bestmove": bestmove,
                "calls": int(stats_match.group(1)),
                "candidates": int(stats_match.group(2)),
                "promotions": int(stats_match.group(3)),
                "prepare_failures": int(stats_match.group(4) or 0)}
    except Exception:
        proc.kill(); proc.wait()
        raise


def summarize(records, modes):
    out = {}
    baseline_nodes = sum(r["nodes"] for r in records if r["mode"] == "off")
    baseline_time = sum(r["wall_ns"] for r in records if r["mode"] == "off")
    for mode in modes:
        selected = [r for r in records if r["mode"] == mode]
        nodes = sum(r["nodes"] for r in selected)
        wall = sum(r["wall_ns"] for r in selected)
        ratios = []
        for row in selected:
            base = next(r for r in records if r["mode"] == "off"
                        and r["root"] == row["root"] and r["trial"] == row["trial"])
            ratios.append(row["wall_ns"] / base["wall_ns"])
        out[mode] = {"runs": len(selected), "nodes": nodes, "wall_ns": wall,
                     "node_ratio": nodes / baseline_nodes,
                     "wall_ratio": wall / baseline_time,
                     "median_paired_wall_ratio": statistics.median(ratios),
                     "calls": sum(r["calls"] for r in selected),
                     "promotions": sum(r["promotions"] for r in selected),
                     "prepare_failures": sum(r.get("prepare_failures", 0) for r in selected),
                     "root_result_changes": sum(
                         (r["bestmove"], r["score_type"], r["score"])
                         != (next(b for b in records if b["mode"] == "off"
                                  and b["root"] == r["root"]
                                  and b["trial"] == r["trial"])["bestmove"],
                             next(b for b in records if b["mode"] == "off"
                                  and b["root"] == r["root"]
                                  and b["trial"] == r["trial"])["score_type"],
                             next(b for b in records if b["mode"] == "off"
                                  and b["root"] == r["root"]
                                  and b["trial"] == r["trial"])["score"])
                         for r in selected) if mode != "off" else 0}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--modes", default="off,0,4,6",
                        help="off or minimum decision depth")
    parser.add_argument("--seed", type=int, default=20260918)
    args = parser.parse_args()
    corpus = json.load(open(args.corpus))
    roots = corpus.get("positions", corpus.get("roots"))
    modes = args.modes.split(",")
    schedule = [(root, trial, mode) for trial in range(args.trials)
                for root in roots for mode in modes]
    random.Random(args.seed).shuffle(schedule)
    records = []
    for index, (root, trial, mode) in enumerate(schedule, 1):
        result = one_run(args.engine, root["fen"], args.depth, mode)
        result.update({"root": root["id"], "trial": trial, "mode": mode})
        records.append(result)
        print(f"{index}/{len(schedule)} {root['id']} trial={trial} mode={mode} "
              f"nodes={result['nodes']} wall_ms={result['wall_ns']/1e6:.2f} "
              f"calls={result['calls']} promotions={result['promotions']} "
              f"prepare_failures={result['prepare_failures']}", flush=True)
    summary = summarize(records, modes)
    with open(args.output, "w") as fh:
        json.dump({"schema": "live-qe-benchmark/1", "engine": args.engine,
                   "corpus": args.corpus, "depth": args.depth,
                   "trials": args.trials, "schedule_seed": args.seed,
                   "modes": modes, "records": records, "summary": summary},
                  fh, indent=2, sort_keys=True)
        fh.write("\n")
    for mode in modes:
        row = summary[mode]
        print(f"SUMMARY mode={mode} node_ratio={row['node_ratio']:.4f} "
              f"wall_ratio={row['wall_ratio']:.4f} "
              f"paired_median={row['median_paired_wall_ratio']:.4f} "
              f"calls={row['calls']} promotions={row['promotions']} "
              f"prepare_failures={row['prepare_failures']} "
              f"root_changes={row['root_result_changes']}")


if __name__ == "__main__":
    main()
