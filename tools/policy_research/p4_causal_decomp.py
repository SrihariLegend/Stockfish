#!/usr/bin/env python3
"""Committed runner for Phase 4 causal decomposition of root move-order interventions.

Evaluates experimental conditions isolating search mechanisms:
1. baseline: Untreated baseline search
2. joint_intervention: Forced candidate move into rootMoves[0]
3. preserve_aspiration: Forced move with baseline aspiration center & width
4. preserve_previous_pv: Forced move with baseline lead move's previousPV (decoupling PV-follow)
5. disable_fail_high_reduction: Forced move without failedHighCnt depth reduction (nominal depth)
6. pure_order_nominal_depth: Forced move with baseline aspiration AND disabled fail-high reduction

Ensures 100% common prefix equivalence (depths 1..D-1 identical across all conditions)
via depth-gated options (PolicyResearchAblationDepth).
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


def run_causal_decomposition(
    eng: p4.Engine,
    corpus_positions: list[dict],
    target_depth: int = 14,
    ref_depth: int = 16,
    roots_to_test: list[tuple[str, str]] | None = None,
    score_tolerance_cp: int = 50,
) -> dict:
    by_id = {p["id"]: p for p in corpus_positions}

    if roots_to_test is None:
        roots_to_test = [
            ("c3-v-001", "b1c3"),
            ("c3-d-004", "g1h1"),
            ("c3-d-002", "e1g1"),
            ("c3-d-007", "a2a3"),
        ]

    decomp_results: dict[str, dict] = {}

    for rid, opt_mv in roots_to_test:
        if rid not in by_id:
            continue
        fen = by_id[rid]["fen"]

        # Run reference search
        ref = eng.run_search(fen, ref_depth)
        ref_best = ref["best"]
        ref_score = ref["score_val"]

        # Run 7 causal conditions with depth-gated ablation
        r_base = eng.run_search(fen, target_depth)
        r_joint = eng.run_search(
            fen, target_depth, force_move=opt_mv, force_depth=target_depth
        )
        r_pres_asp = eng.run_search(
            fen,
            target_depth,
            force_move=opt_mv,
            force_depth=target_depth,
            preserve_aspiration=True,
            ablation_depth=target_depth,
        )
        r_pres_pv = eng.run_search(
            fen,
            target_depth,
            force_move=opt_mv,
            force_depth=target_depth,
            preserve_previous_pv=True,
            ablation_depth=target_depth,
        )
        r_dis_fhr = eng.run_search(
            fen,
            target_depth,
            force_move=opt_mv,
            force_depth=target_depth,
            disable_fail_high_reduction=True,
            ablation_depth=target_depth,
        )
        r_pres_asp_fhr = eng.run_search(
            fen,
            target_depth,
            force_move=opt_mv,
            force_depth=target_depth,
            preserve_aspiration=True,
            disable_fail_high_reduction=True,
            ablation_depth=target_depth,
        )
        r_full_control = eng.run_search(
            fen,
            target_depth,
            force_move=opt_mv,
            force_depth=target_depth,
            preserve_aspiration=True,
            preserve_previous_pv=True,
            disable_fail_high_reduction=True,
            ablation_depth=target_depth,
        )

        conditions = {
            "1_baseline": r_base,
            "2_joint_intervention": r_joint,
            "3_preserve_aspiration": r_pres_asp,
            "4_preserve_previous_pv": r_pres_pv,
            "5_disable_fail_high_reduction": r_dis_fhr,
            "6_preserve_asp_and_fhr": r_pres_asp_fhr,
            "7_full_control_nominal_depth": r_full_control,
        }

        b_nodes = r_base["nodes"]
        b_inc = r_base["incremental_nodes"]

        decomp_results[rid] = {
            "optimal_move": opt_mv,
            "reference": {
                "depth": ref_depth,
                "best": ref_best,
                "score_val": ref_score,
                "score_type": ref["score_type"],
            },
            "conditions": {},
        }

        for cname, res in conditions.items():
            cd_cum = (b_nodes - res["nodes"]) / b_nodes * 100
            cd_inc = (b_inc - res["incremental_nodes"]) / b_inc * 100 if b_inc > 0 else 0.0
            tel = res.get("root_telemetry") or {}
            prev = res["nodes"] - res["incremental_nodes"]

            # Quality validity check vs reference
            best_agrees_ref = res["best"] == ref_best
            score_diff_ref = (
                abs(res["score_val"] - ref_score)
                if res["score_type"] == "cp" and ref["score_type"] == "cp"
                else None
            )
            quality_valid = best_agrees_ref and (
                score_diff_ref is not None and score_diff_ref <= score_tolerance_cp
            )

            decomp_results[rid]["conditions"][cname] = {
                "nodes": res["nodes"],
                "prev_depth_nodes": prev,
                "incremental_nodes": res["incremental_nodes"],
                "cum_reduction_pct": round(cd_cum, 2),
                "incr_reduction_pct": round(cd_inc, 2),
                "best": res["best"],
                "score_val": res["score_val"],
                "score_type": res["score_type"],
                "quality_valid_ref": quality_valid,
                "fail_low": tel.get("aspiration_fail_low", 0),
                "fail_high": tel.get("aspiration_fail_high", 0),
                "iterations": tel.get("aspiration_iterations", 0),
                "root_overhead_nodes": tel.get("root_overhead_nodes", 0),
                "attempts": tel.get("attempts", []),
            }

    return decomp_results


def main():
    ap = argparse.ArgumentParser(description="Run causal decomposition of root search interventions.")
    ap.add_argument("--engine", required=True, help="Path to research Stockfish binary.")
    ap.add_argument("--corpus", default=None, help="Path to corpus JSON.")
    ap.add_argument("--depth", type=int, default=14, help="Target search depth.")
    ap.add_argument("--ref-depth", type=int, default=16, help="Reference search depth.")
    ap.add_argument("--score-tolerance", type=int, default=50, help="Score tolerance in cp.")
    ap.add_argument("--out", required=True, help="Output JSON path.")
    args = ap.parse_args()

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
        "generator_script": "tools/policy_research/p4_causal_decomp.py",
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

    decomp = run_causal_decomposition(
        eng=eng,
        corpus_positions=corpus_data["positions"],
        target_depth=args.depth,
        ref_depth=args.ref_depth,
        score_tolerance_cp=args.score_tolerance,
    )

    output = {
        "schema": "policy-research-causal-decomposition/3",
        "provenance": provenance,
        "depth": args.depth,
        "ref_depth": args.ref_depth,
        "score_tolerance_cp": args.score_tolerance,
        "roots": decomp,
    }

    out_p = Path(args.out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(output, indent=2))
    print(f"Causal decomposition saved to {out_p}")


if __name__ == "__main__":
    main()
