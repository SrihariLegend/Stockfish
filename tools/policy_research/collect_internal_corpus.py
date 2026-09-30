#!/usr/bin/env python3
"""Collect one schema-/3 internal-counterfactual log per frozen corpus root."""
import argparse
import concurrent.futures
import gzip
import json
import os
import subprocess
import tempfile


def send(proc, command):
    proc.stdin.write(command + "\n")
    proc.stdin.flush()


def collect_one(engine, out_dir, root, depth, node_budget, force):
    final = os.path.join(out_dir, root["id"] + ".jsonl.gz")
    if os.path.exists(final) and not force:
        return root["id"], "cached"
    fd, raw = tempfile.mkstemp(prefix=root["id"] + "-", suffix=".jsonl",
                               dir=out_dir)
    os.close(fd)
    os.unlink(raw)  # recorder requires a fresh path
    proc = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        send(proc, "uci")
        send(proc, "setoption name Threads value 1")
        send(proc, "setoption name Hash value 16")
        send(proc, "setoption name PolicyResearch value on")
        send(proc, "setoption name PolicyResearchMode value internal_counterfactual")
        send(proc, "setoption name PolicyResearchLogPath value " + raw)
        send(proc, f"setoption name PolicyResearchSeed value {root['engine_seed']}")
        send(proc, "setoption name PolicyResearchSampleRate value 1.0")
        send(proc, "setoption name PolicyResearchTopK value 4")
        send(proc, f"setoption name PolicyResearchNodeBudget value {node_budget}")
        send(proc, "setoption name PolicyResearchPermBattery value off")
        send(proc, "position fen " + root["fen"])
        send(proc, f"go depth {depth}")
        bestmove = None
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("bestmove"):
                bestmove = line.strip()
                break
        if bestmove is None:
            raise RuntimeError("engine ended without bestmove")
        send(proc, "quit")
        proc.wait(timeout=30)
        with open(raw) as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        decisions = [r for r in records if r.get("type") == "decision"]
        if not decisions or any(r.get("schema") != "internal-counterfactual/3"
                                for r in records):
            raise RuntimeError("missing decisions or wrong schema")
        censored = sum(not p["completed"] for r in decisions for p in r["probes"])
        if censored:
            raise RuntimeError(f"{censored} censored probes")
        if records[-1].get("type") != "root_end" or not records[-1].get("completed"):
            raise RuntimeError("missing completed root_end")
        with open(raw, "rb") as src, open(final + ".tmp", "wb") as dst:
            with gzip.GzipFile(filename="", mode="wb", fileobj=dst, mtime=0) as gz:
                gz.write(src.read())
        os.replace(final + ".tmp", final)
        os.unlink(raw)
        return root["id"], {"rows": len(decisions), "censored": 0,
                            "bestmove": bestmove.split()[1],
                            "baseline_nodes": sum(r["baseline"]["nodes"]
                                                  for r in decisions)}
    except Exception:
        proc.kill()
        proc.wait()
        if os.path.exists(raw):
            os.unlink(raw)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--node-budget", type=int, default=5000)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    corpus = json.load(open(args.corpus))
    os.makedirs(args.output_dir, exist_ok=True)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(collect_one, args.engine, args.output_dir, root,
                               args.depth, args.node_budget, args.force): root
                   for root in corpus["roots"]}
        for future in concurrent.futures.as_completed(futures):
            root_id, result = future.result()
            results[root_id] = result
            print(root_id, result, flush=True)
    manifest = {"schema": "internal-counterfactual-corpus-run/1",
                "corpus": os.path.relpath(args.corpus), "depth": args.depth,
                "threads": 1, "hash_mb": 16, "sample_rate": 1.0,
                "top_k": 4, "node_budget": args.node_budget,
                "roots": results}
    with open(os.path.join(args.output_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
