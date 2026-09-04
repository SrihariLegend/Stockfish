#!/usr/bin/env python3
"""Committed runner for Phase 4 multi-depth ladder stability experiments.

Evaluates development and validation roots across search depths (e.g. D12, D14, D16)
using a fixed candidate set generated at a single candidate depth (e.g. D10 MultiPV-4).
This controls candidate-set selection confounds and isolates how ordering savings
evolve as search depth deepens.

Stores complete candidate-level evaluations for every root and depth.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
import sys

# Ensure local imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p4_force_first as p4
import run_corpus as rc


def run_depth_ladder(
    eng: p4.Engine,
    corpus_positions: list[dict],
    depths: list[int],
    ref_offset: int = 2,
    fixed_candidate_depth: int = 10,
    k: int = 4,
    score_tolerance_cp: int = 50,
) -> dict:
    dev_val_roots = [p for p in corpus_positions if p.get("set") in ("development", "validation")]

    ladder_results: dict[str, dict] = {}

    for root in dev_val_roots:
        rid = root["id"]
        fen = root["fen"]
        rset = root.get("set", "")

        # 1. Pre-generate fixed candidate shortlist at fixed_candidate_depth
        cand_list = eng.top_k(fen, fixed_candidate_depth, k)

        ladder_results[rid] = {
            "set": rset,
            "fixed_candidate_depth": fixed_candidate_depth,
            "predeclared_candidates": cand_list,
            "depths": {},
        }

        print(f"Evaluating {rid} ({rset}) across depths {depths} with candidates {cand_list}...")

        for d in depths:
            rd = d + ref_offset
            # Ensure untreated base best at depth d is also included
            base_temp = eng.run_search(fen, d)
            depth_candidates = list(cand_list)
            if base_temp["best"] not in depth_candidates:
                depth_candidates.insert(0, base_temp["best"])

            r = p4.evaluate_root(
                eng=eng,
                root_id=rid,
                root_info=root,
                depth=d,
                ref_depth=rd,
                candidate_depth=fixed_candidate_depth,
                k=k,
                depth_mode="isolated",
                score_tolerance_cp=score_tolerance_cp,
                fixed_candidates=depth_candidates,
            )

            s = r["summary"]
            opt = s["node_optimal"]

            ladder_results[rid]["depths"][str(d)] = {
                "depth": d,
                "ref_depth": rd,
                "outcome_category": s["outcome_category"],
                "base": r["base"],
                "reference": r["reference"],
                "optimal_move": opt["move"],
                "optimal_nodes": opt["nodes"],
                "optimal_incremental_nodes": opt["incremental_nodes"],
                "cumulative_saving": opt["r_norm_cumulative_nodes"],
                "incremental_saving": opt["r_norm_incremental_nodes"],
                "candidates": r["candidates"],
            }

    return ladder_results


def main():
    ap = argparse.ArgumentParser(description="Run fixed-candidate multi-depth ladder stability experiment.")
    ap.add_argument("--engine", required=True, help="Path to research Stockfish binary.")
    ap.add_argument("--corpus", default=None, help="Path to corpus JSON.")
    ap.add_argument("--depths", default="12,14,16", help="Comma-separated target depths (default: 12,14,16).")
    ap.add_argument("--ref-offset", type=int, default=2, help="Reference depth offset (default: +2).")
    ap.add_argument("--candidate-depth", type=int, default=10, help="Fixed depth for candidate generation.")
    ap.add_argument("--k", type=int, default=4, help="Number of candidates.")
    ap.add_argument("--score-tolerance", type=int, default=50, help="Score tolerance in cp.")
    ap.add_argument("--out", required=True, help="Output JSON path.")
    args = ap.parse_args()

    depths = [int(x.strip()) for x in args.depths.split(",") if x.strip()]

    corpus_path = (
        Path(args.corpus).resolve()
        if args.corpus
        else Path(__file__).resolve().parent / "corpora" / "corpus-v3.json"
    )
    corpus_bytes = corpus_path.read_bytes()
    corpus_sha = hashlib.sha256(corpus_bytes).hexdigest()
    corpus_data = json.loads(corpus_bytes.decode("utf-8"))

    eng = p4.Engine(args.engine)
    git_st = rc.git_state(rc.repo_root())

    provenance = {
        "generator_script": "tools/policy_research/p4_depth_ladder.py",
        "command_line": " ".join(sys.argv),
        "engine_path": eng.path,
        "engine_banner": eng.banner,
        "engine_sha256": eng.sha256,
        "engine_embedded_commit": p4.extract_engine_embedded_commit(eng.banner),
        "tool_commit": git_st["commit_short"],
        "tool_dirty": git_st["dirty"],
        "tool_dirty_file_count": git_st["dirty_file_count"],
        "corpus_path": str(corpus_path),
        "corpus_sha256": corpus_sha,
        "corpus_schema": corpus_data.get("schema"),
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

    results = run_depth_ladder(
        eng=eng,
        corpus_positions=corpus_data["positions"],
        depths=depths,
        ref_offset=args.ref_offset,
        fixed_candidate_depth=args.candidate_depth,
        k=args.k,
        score_tolerance_cp=args.score_tolerance,
    )

    output = {
        "schema": "policy-research-depth-ladder/2",
        "provenance": provenance,
        "depths": depths,
        "fixed_candidate_depth": args.candidate_depth,
        "score_tolerance_cp": args.score_tolerance,
        "roots_count": len(results),
        "results": results,
    }

    out_p = Path(args.out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(output, indent=2))
    print(f"Fixed-candidate depth ladder saved to {out_p}")


if __name__ == "__main__":
    main()
