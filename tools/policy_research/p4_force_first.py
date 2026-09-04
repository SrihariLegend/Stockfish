#!/usr/bin/env python3
"""Phase 4 root-level counterfactual experiment tool (plan.md section 9).

Addresses Phase 4 expert review findings:
1. Root MovePicker mechanics (Finding 1): Stockfish sets ttData.move = rootMoves[0]
   and emits it first in MAIN_TT. The former lead move falls into its natural
   MovePicker stage (captures, quiets scored by history).
2. Two distinct experimental estimands (Finding 2):
   - isolated (default, Experiment B): PolicyResearchForceFirstDepth == depth;
     depths 1..D-1 run under standard baseline conditions so TT and history state
     at the start of depth D are identical across all candidate interventions.
     Forcing the baseline's own best move at depth D is an exact no-op when the
     lead move is stable across iterations (depth_D_best == depth_D_minus_1_best).
   - persistent (Experiment A): PolicyResearchForceFirstDepth == 0; overrides
     at all depths 1..D, capturing cumulative iterative-deepening trajectory churn.
     Incremental nodes represent each candidate's own divergent trajectory step.
3. Mate score sign verification & refined classification (Finding 3):
   - Checks mate score sign agreement: (cand_val > 0) == (ref_val > 0).
   - Classifies score status as exact_ref (0 cp), near_ref (<= 25 cp),
     within_tolerance (<= tolerance cp), mild_drift (<= 100 cp), score_collapse (> 100 cp).
4. Disentangled time metrics & ties (Finding 4):
   - Node-optimal candidate wall time reported with scheduling jitter caveat.
   - Separate time-optimal move only reported if strictly faster than node-optimal.
5. Dual-baseline comparison & tolerance sensitivity curve (Findings 2, 4):
   - Compares candidate scores to both same-depth baseline and deeper reference.
   - Computes sensitivity curve across tolerance gates (25, 50, 75, 100 cp) to
     show cost-quality frontiers.
6. Incremental vs cumulative node costs (Finding 5):
   - Captures depth-(D-1) node baseline to report both cumulative (search-to-depth-D)
     and incremental (target depth-D iteration only, ΔN_D) node metrics.
7. Candidate set framing (Finding 6):
   - Clarifies top-k candidate set is an empirical search-informed shortlist.
8. Test-root transparency (Finding 7): marks c1-t-001 as burned/exploratory.
9. Durable provenance (Finding 8): records engine banner, embedded commit, tool
   commit, worktree cleanliness, and writes versioned artifacts.

Usage:
  STOCKFISH_ENGINE=src/stockfish python3 tools/policy_research/p4_force_first.py \
      --depth 14 --reference-depth 16 --depth-mode isolated \
      --ids c1-d-001,c1-v-001,c1-t-001 --out tools/policy_research/runs/my-run/p4-isolated.json
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_corpus as rc


def parse_root_telemetry_line(line: str):
    m = re.search(
        r"root_telemetry depth (\d+) fail_low (\d+) fail_high (\d+) iterations (\d+)(?: attempts (.*?))? moves (.*)",
        line,
    )
    if not m:
        return None
    d = int(m.group(1))
    fail_low = int(m.group(2))
    fail_high = int(m.group(3))
    iterations = int(m.group(4))
    attempts_str = (m.group(5) or "").strip()
    moves_str = (m.group(6) or "").strip()

    attempts = []
    if attempts_str:
        for item in attempts_str.split():
            parts = item.split(":")
            if len(parts) >= 3:
                attempts.append({
                    "depth": int(parts[0]),
                    "value": int(parts[1]),
                    "result": parts[2],
                    "alpha": int(parts[3]) if len(parts) >= 5 else None,
                    "beta": int(parts[4]) if len(parts) >= 5 else None,
                    "attempt_nodes": int(parts[5]) if len(parts) >= 6 else None,
                })

    moves_map = {}
    if moves_str:
        for item in moves_str.split():
            parts = item.split(":")
            if len(parts) == 3:
                moves_map[parts[0]] = {
                    "effort_total": int(parts[1]),
                    "effort_incremental": int(parts[1]),
                    "score": int(parts[2]),
                }
            elif len(parts) >= 4:
                moves_map[parts[0]] = {
                    "effort_total": int(parts[1]),
                    "effort_incremental": int(parts[2]),
                    "score": int(parts[3]),
                }
    return {
        "depth": d,
        "aspiration_fail_low": fail_low,
        "aspiration_fail_high": fail_high,
        "aspiration_iterations": iterations,
        "attempts": attempts,
        "moves": moves_map,
    }


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
        self.sha256 = hashlib.sha256(Path(self.path).read_bytes()).hexdigest()
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
        t0 = time.perf_counter()
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
            "wall_time_ms": None,
            "score_type": None,
            "score_val": None,
            "score_bound": None,
            "seldepth": None,
            "nps": None,
            "pv": None,
            "best": None,
            "ponder": None,
            "info_strings": [],
            "telemetry": {},
            "root_telemetry": None,
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
                    if "root_telemetry" in line:
                        tel = parse_root_telemetry_line(line)
                        if tel:
                            res["telemetry"][tel["depth"]] = tel
                info = parse_info_line(line)
                if info:
                    if info.get("depth") == target_depth - 1:
                        res["prev_depth_nodes"] = info.get("nodes", 0)
                        res["prev_depth_time_ms"] = info.get("time_ms", 0)
                        if info.get("pv"):
                            res["d_minus_1_lead_move"] = info.get("pv").split()[0]
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

        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        res["wall_time_ms"] = wall_ms
        if res["nodes"] is None or res["best"] is None:
            raise RuntimeError(
                f"Search failed to produce results at target depth {target_depth}. "
                f"Last state: {res}"
            )
        res["incremental_nodes"] = max(0, res["nodes"] - (res["prev_depth_nodes"] or 0))
        res["incremental_time_ms"] = max(0, (res["time_ms"] or 0) - (res["prev_depth_time_ms"] or 0))
        if target_depth in res["telemetry"]:
            tel = res["telemetry"][target_depth]
            sum_eff = sum(m.get("effort_incremental", 0) for m in tel.get("moves", {}).values())
            tel["root_overhead_nodes"] = max(0, res["incremental_nodes"] - sum_eff)
            res["root_telemetry"] = tel
        else:
            res["root_telemetry"] = None
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

    def run_search(
        self,
        fen: str,
        depth: int,
        force_move: str = "",
        force_depth: int = 0,
        preserve_aspiration: bool = False,
        disable_fail_high_reduction: bool = False,
        ablation_depth: int = 0,
        preserve_previous_pv: bool = False,
    ):
        cmds = [
            "setoption name PolicyResearch value on",
            "setoption name PolicyResearchMode value root_counterfactual",
        ]
        if force_move:
            cmds.append(f"setoption name PolicyResearchForceFirstMove value {force_move}")
            cmds.append(f"setoption name PolicyResearchForceFirstDepth value {force_depth}")
        if ablation_depth > 0:
            cmds.append(f"setoption name PolicyResearchAblationDepth value {ablation_depth}")
        if preserve_aspiration:
            cmds.append("setoption name PolicyResearchPreserveAspiration value on")
        if disable_fail_high_reduction:
            cmds.append("setoption name PolicyResearchDisableFailHighReduction value on")
        if preserve_previous_pv:
            cmds.append("setoption name PolicyResearchPreservePreviousPV value on")
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
    trials: int = 1,
    preserve_aspiration: bool = False,
    disable_fail_high_reduction: bool = False,
    preserve_previous_pv: bool = False,
    ablation_depth: int = 0,
    fixed_candidates: list[str] | None = None,
):
    fen = root_info["fen"]
    root_set = root_info["set"]
    is_burned = root_id == "c1-t-001"

    abl_depth = ablation_depth if ablation_depth > 0 else (
        depth if (preserve_aspiration or disable_fail_high_reduction or preserve_previous_pv) else 0
    )

    # 1. Baseline search at depth D
    base = eng.run_search(
        fen,
        depth,
        preserve_aspiration=preserve_aspiration,
        disable_fail_high_reduction=disable_fail_high_reduction,
        ablation_depth=abl_depth,
        preserve_previous_pv=preserve_previous_pv,
    )

    # 2. Reference search at ref_depth (clean baseline, no override)
    ref = eng.run_search(fen, ref_depth)

    # Distance of baseline from reference
    base_diff_ref = (
        base["score_val"] - ref["score_val"]
        if base["score_type"] == "cp" and ref["score_type"] == "cp"
        else None
    )
    if base["score_type"] == "mate" and ref["score_type"] == "mate":
        mate_sign_agree = (base["score_val"] > 0) == (ref["score_val"] > 0)
        mate_dist_diff = abs(base["score_val"] - ref["score_val"])
        base_score_agrees_ref = mate_sign_agree and (mate_dist_diff <= 1)
    elif base_diff_ref is not None:
        base_score_agrees_ref = abs(base_diff_ref) <= score_tolerance_cp
    else:
        base_score_agrees_ref = False

    # 3. Candidate shortlist from depth candidate_depth MultiPV
    # Methodological constraint: do NOT inject same-depth untreated final best
    # into the candidate set. Use strictly pre-declared candidates from D-1/prior
    # depth information, with the D-1 leader as the mandatory exact no-op control.
    if fixed_candidates is not None:
        candidates = list(fixed_candidates)
    else:
        candidates = eng.top_k(fen, candidate_depth, k)

    # Ensure D-1 leader is present as the mandatory exact no-op control
    d_minus_1_lead = base.get("d_minus_1_lead_move")
    if d_minus_1_lead and d_minus_1_lead not in candidates:
        candidates.insert(0, d_minus_1_lead)

    forced_records = []
    for mv in candidates:
        force_depth = depth if depth_mode == "isolated" else 0
        r = eng.run_search(
            fen,
            depth,
            force_move=mv,
            force_depth=force_depth,
            preserve_aspiration=preserve_aspiration,
            disable_fail_high_reduction=disable_fail_high_reduction,
            ablation_depth=abl_depth,
            preserve_previous_pv=preserve_previous_pv,
        )

        # Quality evaluation (plan 9.4)
        best_agrees_base = r["best"] == base["best"]
        best_agrees_ref = r["best"] == ref["best"]

        score_diff_base = None
        score_diff_ref = None
        additional_error_vs_base = None

        if r["score_type"] == "cp" and base["score_type"] == "cp":
            score_diff_base = r["score_val"] - base["score_val"]
        if r["score_type"] == "cp" and ref["score_type"] == "cp":
            score_diff_ref = r["score_val"] - ref["score_val"]
            if base_diff_ref is not None:
                additional_error_vs_base = max(0, abs(score_diff_ref) - abs(base_diff_ref))

        # Classification of score stability with mate sign check and distance tolerance
        if r["score_type"] == "mate" and ref["score_type"] == "mate":
            mate_sign_agree = (r["score_val"] > 0) == (ref["score_val"] > 0)
            mate_dist_diff = abs(r["score_val"] - ref["score_val"])
            score_agrees = mate_sign_agree and (mate_dist_diff <= 1)
            if not mate_sign_agree:
                score_status = "score_collapse"
            elif mate_dist_diff == 0:
                score_status = "exact_mate"
            elif mate_dist_diff <= 1:
                score_status = "near_mate"
            else:
                score_status = "mate_distance_drift"
        elif r["score_type"] != ref["score_type"]:
            score_agrees = False
            score_status = "score_collapse"
        else:
            diff_abs = abs(score_diff_ref)
            score_agrees = (diff_abs <= score_tolerance_cp)
            if diff_abs == 0:
                score_status = "exact_ref"
            elif diff_abs <= min(25, score_tolerance_cp):
                score_status = "near_ref"
            elif diff_abs <= score_tolerance_cp:
                score_status = "within_tolerance"
            elif diff_abs <= 100:
                score_status = "mild_drift"
            else:
                score_status = "score_collapse"

        # Multi-band tolerance sensitivity (Findings 2, 4)
        bands = [25, 50, 75, 100]
        if score_tolerance_cp not in bands:
            bands.append(score_tolerance_cp)
            bands.sort()

        quality_valid_at_band = {}
        quality_valid_relative_at_band = {}
        for b in bands:
            if score_diff_ref is not None:
                band_agrees = abs(score_diff_ref) <= b
                rel_band_agrees = (additional_error_vs_base or 0) <= b
            elif r["score_type"] == "mate" and ref["score_type"] == "mate":
                band_agrees = (r["score_val"] > 0) == (ref["score_val"] > 0) and abs(r["score_val"] - ref["score_val"]) <= 1
                rel_band_agrees = band_agrees
            else:
                band_agrees = False
                rel_band_agrees = False
            quality_valid_at_band[b] = best_agrees_ref and band_agrees
            quality_valid_relative_at_band[b] = best_agrees_ref and rel_band_agrees

        quality_valid = best_agrees_ref and score_agrees

        inc_nodes = r["incremental_nodes"]
        base_inc_nodes = base["incremental_nodes"]
        inc_delta = inc_nodes - base_inc_nodes
        inc_ratio = (inc_nodes / base_inc_nodes) if base_inc_nodes > 0 else 1.0

        rec = {
            "move": mv,
            "is_baseline_best": (mv == base["best"]),
            "is_untreated_final_best": (mv == base["best"]),
            "is_d_minus_1_lead_move": (mv == base.get("d_minus_1_lead_move")),
            "nodes": r["nodes"],
            "incremental_nodes": inc_nodes,
            "prev_depth_nodes": r["prev_depth_nodes"],
            "time_ms": r["time_ms"],
            "incremental_time_ms": r["incremental_time_ms"],
            "wall_time_ms": r["wall_time_ms"],
            "score_type": r["score_type"],
            "score_val": r["score_val"],
            "score_bound": r["score_bound"],
            "best": r["best"],
            "pv": r["pv"],
            "root_telemetry": r["root_telemetry"],
            "node_delta_vs_base": r["nodes"] - base["nodes"],
            "node_ratio_vs_base": r["nodes"] / base["nodes"],
            "incremental_delta_vs_base": inc_delta,
            "incremental_ratio_vs_base": inc_ratio,
            "best_agrees_base": best_agrees_base,
            "best_agrees_ref": best_agrees_ref,
            "score_diff_base": score_diff_base,
            "score_diff_ref": score_diff_ref,
            "additional_error_vs_base": additional_error_vs_base,
            "score_status": score_status,
            "score_agrees": score_agrees,
            "quality_valid_at_band": quality_valid_at_band,
            "quality_valid_relative_at_band": quality_valid_relative_at_band,
            "quality_valid": quality_valid,
        }
        forced_records.append(rec)

    # Aggregates across all candidates
    all_nodes = [c["nodes"] for c in forced_records]
    min_nodes_all = min(all_nodes)
    r_norm_all_nodes = (base["nodes"] - min_nodes_all) / base["nodes"]

    # Sensitivity analysis across tolerance bands (Findings 2, 4)
    tolerance_sensitivity = {}
    for b in [25, 50, 75, 100]:
        v_abs = [c for c in forced_records if c["quality_valid_at_band"].get(b, False)]
        v_rel = [c for c in forced_records if c["quality_valid_relative_at_band"].get(b, False)]
        b_inc = base["incremental_nodes"]

        abs_opt = None
        if v_abs:
            opt = min(v_abs, key=lambda c: c["nodes"])
            abs_opt = {
                "move": opt["move"],
                "nodes": opt["nodes"],
                "incremental_nodes": opt["incremental_nodes"],
                "r_norm_cumulative": (base["nodes"] - opt["nodes"]) / base["nodes"],
                "r_norm_incremental": (b_inc - opt["incremental_nodes"]) / b_inc if b_inc > 0 else 0.0,
            }

        rel_opt = None
        if v_rel:
            opt = min(v_rel, key=lambda c: c["nodes"])
            rel_opt = {
                "move": opt["move"],
                "nodes": opt["nodes"],
                "incremental_nodes": opt["incremental_nodes"],
                "r_norm_cumulative": (base["nodes"] - opt["nodes"]) / base["nodes"],
                "r_norm_incremental": (b_inc - opt["incremental_nodes"]) / b_inc if b_inc > 0 else 0.0,
            }

        tolerance_sensitivity[str(b)] = {
            "absolute_gate": abs_opt,
            "relative_gate": rel_opt,
        }

    # Multi-trial timing benchmark with balanced Latin-square counterbalanced order
    timing_trials = None
    if trials > 1:
        import random
        prng = random.Random(int(hashlib.md5(fen.encode()).hexdigest(), 16) & 0xFFFFFFFF)
        actions = ["__baseline__"] + [mv for mv in candidates if mv != base["best"]]
        m_actions = len(actions)

        # Generate a balanced Latin square order across trials
        # Randomize initial action assignment, then cyclically permute
        base_perm = list(actions)
        prng.shuffle(base_perm)

        eng_times = {a: [] for a in actions}
        wall_times = {a: [] for a in actions}
        execution_orders = []
        pos_counts = {a: [0] * m_actions for a in actions}

        for trial_idx in range(trials):
            shift = trial_idx % m_actions
            trial_actions = [base_perm[(i + shift) % m_actions] for i in range(m_actions)]
            execution_orders.append(list(trial_actions))
            for slot_idx, act in enumerate(trial_actions):
                pos_counts[act][slot_idx] += 1

            for act in trial_actions:
                if act == "__baseline__":
                    r_act = eng.run_search(
                        fen,
                        depth,
                        preserve_aspiration=preserve_aspiration,
                        disable_fail_high_reduction=disable_fail_high_reduction,
                    )
                else:
                    f_d = depth if depth_mode == "isolated" else 0
                    r_act = eng.run_search(
                        fen,
                        depth,
                        force_move=act,
                        force_depth=f_d,
                        preserve_aspiration=preserve_aspiration,
                        disable_fail_high_reduction=disable_fail_high_reduction,
                    )
                eng_times[act].append(r_act["time_ms"])
                wall_times[act].append(r_act.get("wall_time_ms", r_act["time_ms"]))

        def compute_stats(vals):
            s_vals = sorted(vals)
            n = len(s_vals)
            q25 = s_vals[n // 4]
            q75 = s_vals[(3 * n) // 4]
            return {
                "values": vals,
                "median": statistics.median(vals),
                "iqr": round(q75 - q25, 2),
                "mean": round(statistics.mean(vals), 2),
                "std": round(statistics.stdev(vals), 2) if n > 1 else 0.0,
            }

        timing_trials = {
            "design": "balanced_latin_square",
            "trials_count": trials,
            "actions": actions,
            "positional_counts": pos_counts,
            "orders": execution_orders,
            "baseline": {
                "engine_stats": compute_stats(eng_times["__baseline__"]),
                "wall_stats": compute_stats(wall_times["__baseline__"]),
            },
            "candidates": {
                mv: {
                    "engine_stats": compute_stats(eng_times[mv]),
                    "wall_stats": compute_stats(wall_times[mv]),
                }
                for mv in actions
                if mv != "__baseline__"
            },
        }

    valid_records = [c for c in forced_records if c["quality_valid"]]
    better_valid = [c for c in valid_records if c["nodes"] < base["nodes"]]

    if better_valid:
        cheapest_nodes_rec = min(better_valid, key=lambda c: c["nodes"])
        cheapest_nodes_mv = cheapest_nodes_rec["move"]
        min_nodes_valid = cheapest_nodes_rec["nodes"]
        r_norm_nodes_valid = (base["nodes"] - min_nodes_valid) / base["nodes"]

        base_inc = base["incremental_nodes"]
        opt_inc = cheapest_nodes_rec["incremental_nodes"]
        min_inc_nodes_valid = opt_inc
        r_norm_inc_nodes_valid = (base_inc - opt_inc) / base_inc if base_inc > 0 else 0.0

        time_at_min_nodes = cheapest_nodes_rec["time_ms"]
        r_norm_time_at_min_nodes = (
            (base["time_ms"] - time_at_min_nodes) / base["time_ms"]
            if base["time_ms"] and time_at_min_nodes is not None
            else None
        )

        if base["best"] == ref["best"]:
            outcome_category = "result_preserving_saving"
        else:
            outcome_category = "convergence_correction_saving"
    elif valid_records:
        # At least one candidate passed quality gate, but none beat baseline nodes
        if base["best"] == ref["best"] and base_score_agrees_ref:
            outcome_category = "baseline_optimal"
        else:
            outcome_category = "harmful_intervention"
        cheapest_nodes_mv = base["best"]
        min_nodes_valid = base["nodes"]
        r_norm_nodes_valid = 0.0
        min_inc_nodes_valid = base["incremental_nodes"]
        r_norm_inc_nodes_valid = 0.0
        time_at_min_nodes = base["time_ms"]
        r_norm_time_at_min_nodes = 0.0
    else:
        if base["best"] == ref["best"] and base_score_agrees_ref:
            outcome_category = "baseline_optimal"
        else:
            outcome_category = "no_valid_candidate"
        cheapest_nodes_mv = base["best"]
        min_nodes_valid = base["nodes"]
        r_norm_nodes_valid = 0.0
        min_inc_nodes_valid = base["incremental_nodes"]
        r_norm_inc_nodes_valid = 0.0
        time_at_min_nodes = base["time_ms"]
        r_norm_time_at_min_nodes = 0.0

    return {
        "root": root_id,
        "set": root_set,
        "is_burned_test_root": is_burned,
        "depth": depth,
        "reference_depth": ref_depth,
        "candidate_depth": candidate_depth,
        "depth_mode": depth_mode,
        "preserve_aspiration": preserve_aspiration,
        "disable_fail_high_reduction": disable_fail_high_reduction,
        "preserve_previous_pv": preserve_previous_pv,
        "ablation_depth": abl_depth,
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
            "d_minus_1_lead_move": base.get("d_minus_1_lead_move"),
            "pv": base["pv"],
            "root_telemetry": base.get("root_telemetry"),
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
        "tolerance_sensitivity": tolerance_sensitivity,
        "timing_trials": timing_trials,
        "summary": {
            "candidate_count": len(forced_records),
            "valid_candidate_count": len(valid_records),
            "outcome_category": outcome_category,
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
            "strictly_faster_alternative": None,
        },
    }


def print_root_report(res: dict):
    b = res["base"]
    ref = res["reference"]
    s = res["summary"]
    opt_node = s["node_optimal"]
    faster_alt = s["strictly_faster_alternative"]
    burned_note = " [EXPLORATORY / BURNED TEST ROOT]" if res["is_burned_test_root"] else ""
    print(f"\n================================================================================")
    print(f"Root: {res['root']} ({res['set']}){burned_note} | Depth Mode: {res['depth_mode'].upper()}")
    print(f"Baseline @ D{res['depth']}: {b['nodes']:,} nodes ({b['incremental_nodes']:,} incr) | {b['time_ms']} ms | score {b['score_type']} {b['score_val']} ({b['score_bound']}) | best: {b['best']}")
    print(f"Reference @ D{ref['depth']}: {ref['nodes']:,} nodes | {ref['time_ms']} ms | score {ref['score_type']} {ref['score_val']} | best: {ref['best']}")
    print(f"--------------------------------------------------------------------------------")
    print(f"{'Move':<8} {'Nodes(Cum)':>11} {'Delta(Cum)':>11} {'Nodes(Inc)':>11} {'Time':>6} {'Score(Δbase/Δref)':>22} {'Best':>8} {'Agree':>7} {'Quality':>10}")
    print(f"--------------------------------------------------------------------------------")
    for c in res["candidates"]:
        star = " *" if c["is_baseline_best"] else "  "
        db_str = f"{c['score_diff_base']:+d}" if c['score_diff_base'] is not None else "?"
        dr_str = f"{c['score_diff_ref']:+d}" if c['score_diff_ref'] is not None else "?"
        score_diff_str = f"({db_str}/{dr_str}cp)"
        score_str = f"{c['score_type']} {c['score_val']:>3} {score_diff_str:>13}"
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
        print(f"{c['move'] + star:<8} {c['nodes']:>11,d} {delta_str:>11} {c['incremental_nodes']:>11,d} {c['time_ms']:>4}ms {score_str:>22} {c['best']:>8} {agree_str:>7} {qual_str:>10}")
    print(f"--------------------------------------------------------------------------------")
    print(f"Unconstrained min:  {s['min_nodes_all']:,} nodes  |  R_norm (cumulative): {s['r_norm_all_nodes']:+.3f}")
    if opt_node['move'] is not None:
        print(f"Quality-valid node-optimal:  {opt_node['move']}  |  Cumulative: {opt_node['nodes']:,} nodes (R_norm: {opt_node['r_norm_cumulative_nodes']:+.3f})")
        print(f"                             Incremental D{res['depth']}: {opt_node['incremental_nodes']:,} nodes (R_norm: {opt_node['r_norm_incremental_nodes']:+.3f})")
        print(f"                             Time at node-optimal: {opt_node['time_ms']} ms (R_norm: {opt_node['r_norm_time']:+.3f})")
        if faster_alt is not None:
            print(f"Strictly faster alternative: {faster_alt['move']}  |  Time: {faster_alt['time_ms']} ms (R_norm: {faster_alt['r_norm_time']:+.3f})")
    else:
        print(f"Quality-valid min:  NONE PASSED QUALITY GATES")

    # Search telemetry printout (Finding 3)
    if b.get("root_telemetry"):
        b_tel = b["root_telemetry"]
        print(f"Root search telemetry @ D{res['depth']}:")
        print(f"  Baseline: fail_low={b_tel['aspiration_fail_low']}, fail_high={b_tel['aspiration_fail_high']}, aspiration_iterations={b_tel['aspiration_iterations']}")
        if b_tel.get("attempts"):
            att_strs = [f"D{a['depth']}:{a['value']}({a['result']})" for a in b_tel["attempts"]]
            print(f"  Baseline attempts: {' -> '.join(att_strs)}")
        opt_c = next((c for c in res["candidates"] if c["move"] == opt_node["move"]), None)
        if opt_c and opt_c.get("root_telemetry"):
            c_tel = opt_c["root_telemetry"]
            print(f"  Optimal forced move ({opt_node['move']}): fail_low={c_tel['aspiration_fail_low']}, fail_high={c_tel['aspiration_fail_high']}, aspiration_iterations={c_tel['aspiration_iterations']}")
            if c_tel.get("attempts"):
                att_strs = [f"D{a['depth']}:{a['value']}({a['result']})" for a in c_tel["attempts"]]
                print(f"  Optimal attempts: {' -> '.join(att_strs)}")

    # Multi-trial timing printout if present
    if res.get("timing_trials"):
        tt = res["timing_trials"]
        b_tt = tt["baseline"]
        b_eng = b_tt["engine_stats"]
        b_wall = b_tt["wall_stats"]
        print(f"Balanced Latin-square multi-trial timing ({tt['trials_count']} counterbalanced runs):")
        print(f"  Baseline: engine median {b_eng['median']:.1f} ms (IQR: {b_eng['iqr']:.1f}, mean: {b_eng['mean']:.1f}±{b_eng['std']:.1f} ms) | wall median {b_wall['median']:.1f} ms")
        if opt_node["move"] and opt_node["move"] in tt["candidates"]:
            c_tt = tt["candidates"][opt_node["move"]]
            c_eng = c_tt["engine_stats"]
            c_wall = c_tt["wall_stats"]
            eng_speedup = (b_eng['median'] - c_eng['median']) / b_eng['median'] if b_eng['median'] else 0.0
            print(f"  Optimal ({opt_node['move']}): engine median {c_eng['median']:.1f} ms ({eng_speedup:+.1%} engine speedup, IQR: {c_eng['iqr']:.1f}, mean: {c_eng['mean']:.1f}±{c_eng['std']:.1f} ms) | wall median {c_wall['median']:.1f} ms")

    # Outcome category and tolerance sensitivity printout
    print(f"Outcome taxonomy category: {s.get('outcome_category', 'UNKNOWN').upper()}")
    print(f"Score tolerance sensitivity:")
    sens = res.get("tolerance_sensitivity", {})
    for b_str in ["25", "50", "75", "100"]:
        item = sens.get(b_str, {})
        abs_item = item.get("absolute_gate") if isinstance(item, dict) else None
        rel_item = item.get("relative_gate") if isinstance(item, dict) else None
        abs_str = (
            f"opt {abs_item['move']} ({abs_item['nodes']:,} nodes, cumul: {abs_item['r_norm_cumulative']:+.3f}, incr: {abs_item['r_norm_incremental']:+.3f})"
            if abs_item
            else "NONE VALID"
        )
        rel_str = (
            f"opt {rel_item['move']} ({rel_item['r_norm_incremental']:+.3f} incr)"
            if rel_item
            else "NONE"
        )
        print(f"  Gate ±{b_str:>3} cp: absolute [{abs_str}] | relative vs base [{rel_str}]")

    if res['depth_mode'] == 'persistent':
        print(f"Persistent mode note: incremental nodes represent each candidate's divergent trajectory step; not a common-prefix causal comparison.")
    print(f"Timing caveat: single-run wall times at millisecond resolution (5-35 ms range) are subject to system scheduling jitter.")
    print(f"================================================================================")


def main():
    ap = argparse.ArgumentParser(description="Phase 4 root-level counterfactual experiment driver.")
    ap.add_argument("--engine", default=None, help="Path to research Stockfish binary.")
    ap.add_argument("--depth", type=int, default=14, help="Target search depth.")
    ap.add_argument(
        "--reference-depth",
        type=int,
        default=None,
        help="Reference search depth (default: target depth + 2).",
    )
    ap.add_argument("--candidate-depth", type=int, default=10, help="MultiPV depth for candidate shortlist.")
    ap.add_argument("--k", type=int, default=4, help="Number of MultiPV candidates.")
    ap.add_argument(
        "--depth-mode",
        choices=["isolated", "persistent"],
        default="isolated",
        help="isolated: override only at target depth (Experiment B). persistent: override all depths (Experiment A).",
    )
    ap.add_argument("--score-tolerance", type=int, default=50, help="Max score cp delta vs reference.")
    ap.add_argument("--trials", type=int, default=1, help="Number of counterbalanced timing trials (default: 1).")
    ap.add_argument(
        "--preserve-aspiration",
        action="store_true",
        help="Causal ablation: keep baseline aspiration window center and width (isolate pure MovePicker ordering).",
    )
    ap.add_argument(
        "--disable-fail-high-reduction",
        action="store_true",
        help="Causal ablation: disable failedHighCnt depth reduction (force searches at nominal depth).",
    )
    ap.add_argument(
        "--preserve-previous-pv",
        action="store_true",
        help="Causal ablation: keep baseline leader's previousPV (isolate pure first-slot ordering without PV-follow changes).",
    )
    ap.add_argument(
        "--ablation-depth",
        type=int,
        default=0,
        help="Depth at which causal ablations apply (default: target depth).",
    )
    ap.add_argument("--ids", default=None, help="Comma-separated root IDs (default: all dev and val roots).")
    ap.add_argument("--corpus", default=None, help="Path to corpus JSON (default: corpora/corpus-v3.json).")
    ap.add_argument("--out", default=None, help="Output JSON path.")
    args = ap.parse_args()

    engine_path = args.engine or os.environ.get("STOCKFISH_ENGINE")
    if not engine_path:
        sys.exit("Error: must set STOCKFISH_ENGINE or pass --engine")

    eng = Engine(engine_path)

    ref_depth = args.reference_depth if args.reference_depth is not None else args.depth + 2
    if ref_depth <= args.depth:
        sys.exit(f"Error: reference depth ({ref_depth}) must be strictly greater than target depth ({args.depth})")

    corpus_path = (
        Path(args.corpus).resolve()
        if args.corpus
        else Path(__file__).resolve().parents[0] / "corpora" / "corpus-v3.json"
    )
    corpus, _ = rc.load_corpus(str(corpus_path))
    by_id = {p["id"]: p for p in corpus["positions"]}
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        # Default: all development and validation roots (quarantining test roots)
        ids = [p["id"] for p in corpus["positions"] if p["set"] in ("development", "validation")]

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
            ref_depth=ref_depth,
            candidate_depth=args.candidate_depth,
            k=args.k,
            depth_mode=args.depth_mode,
            score_tolerance_cp=args.score_tolerance,
            trials=args.trials,
            preserve_aspiration=args.preserve_aspiration,
            disable_fail_high_reduction=args.disable_fail_high_reduction,
            preserve_previous_pv=args.preserve_previous_pv,
            ablation_depth=args.ablation_depth,
        )
        all_results.append(res)
        print_root_report(res)

    # Population summary
    pop_total = len(all_results)
    population_summary = None
    if pop_total > 0:
        res_pres = sum(1 for r in all_results if r["summary"]["outcome_category"] == "result_preserving_saving")
        conv_corr = sum(1 for r in all_results if r["summary"]["outcome_category"] == "convergence_correction_saving")
        base_opt = sum(1 for r in all_results if r["summary"]["outcome_category"] == "baseline_optimal")
        harmful = sum(1 for r in all_results if r["summary"]["outcome_category"] == "harmful_intervention")
        no_val = sum(1 for r in all_results if r["summary"]["outcome_category"] == "no_valid_candidate")

        oracle_cum_savings = [
            max(0.0, r["summary"]["node_optimal"]["r_norm_cumulative_nodes"] or 0.0)
            for r in all_results
        ]
        oracle_inc_savings = [
            max(0.0, r["summary"]["node_optimal"]["r_norm_incremental_nodes"] or 0.0)
            for r in all_results
        ]

        base_nodes_total = sum(r["base"]["nodes"] for r in all_results)
        opt_nodes_total = sum(
            min(r["base"]["nodes"], r["summary"]["node_optimal"]["nodes"] or r["base"]["nodes"])
            for r in all_results
        )
        pooled_cum_saving = (
            (base_nodes_total - opt_nodes_total) / base_nodes_total if base_nodes_total > 0 else 0.0
        )

        base_inc_total = sum(r["base"]["incremental_nodes"] for r in all_results)
        opt_inc_total = sum(
            min(r["base"]["incremental_nodes"], r["summary"]["node_optimal"]["incremental_nodes"] or r["base"]["incremental_nodes"])
            for r in all_results
        )
        pooled_inc_saving = (
            (base_inc_total - opt_inc_total) / base_inc_total if base_inc_total > 0 else 0.0
        )

        population_summary = {
            "roots_evaluated": pop_total,
            "result_preserving_saving_count": res_pres,
            "result_preserving_saving_pct": round(res_pres / pop_total * 100, 1),
            "convergence_correction_saving_count": conv_corr,
            "convergence_correction_saving_pct": round(conv_corr / pop_total * 100, 1),
            "total_positive_opportunity_count": res_pres + conv_corr,
            "total_positive_opportunity_pct": round((res_pres + conv_corr) / pop_total * 100, 1),
            "baseline_optimal_count": base_opt,
            "baseline_optimal_pct": round(base_opt / pop_total * 100, 1),
            "harmful_intervention_count": harmful,
            "harmful_intervention_pct": round(harmful / pop_total * 100, 1),
            "no_valid_candidate_count": no_val,
            "no_valid_candidate_pct": round(no_val / pop_total * 100, 1),
            "macro_mean_cumulative_saving": round(statistics.mean(oracle_cum_savings), 4),
            "macro_median_cumulative_saving": round(statistics.median(oracle_cum_savings), 4),
            "macro_mean_incremental_saving": round(statistics.mean(oracle_inc_savings), 4),
            "macro_median_incremental_saving": round(statistics.median(oracle_inc_savings), 4),
            "pooled_cumulative_saving": round(pooled_cum_saving, 4),
            "pooled_incremental_saving": round(pooled_inc_saving, 4),
            "pooled_baseline_cumulative_nodes": base_nodes_total,
            "pooled_optimal_cumulative_nodes": opt_nodes_total,
            "pooled_baseline_incremental_nodes": base_inc_total,
            "pooled_optimal_incremental_nodes": opt_inc_total,
        }

        print("\n================================================================================")
        print("POPULATION STATISTICS SUMMARY (18 Predeclared Dev + Val Roots):")
        print(f"  Roots evaluated: {pop_total}")
        print(f"  Positive opportunities: {res_pres + conv_corr}/{pop_total} ({(res_pres + conv_corr)/pop_total*100:.1f}%)")
        print(f"    - Result-preserving savings: {res_pres}/{pop_total} ({res_pres/pop_total*100:.1f}%)")
        print(f"    - Convergence corrections:   {conv_corr}/{pop_total} ({conv_corr/pop_total*100:.1f}%)")
        print(f"  Baseline optimal (regret 0.0): {base_opt}/{pop_total} ({base_opt/pop_total*100:.1f}%)")
        print(f"  Harmful interventions:         {harmful}/{pop_total} ({harmful/pop_total*100:.1f}%)")
        print(f"  No valid candidates:           {no_val}/{pop_total} ({no_val/pop_total*100:.1f}%)")
        print(f"  Macro Mean Savings:  Cumulative {population_summary['macro_mean_cumulative_saving']*100:.1f}% | Target-Iteration {population_summary['macro_mean_incremental_saving']*100:.1f}%")
        print(f"  Macro Median Savings: Cumulative {population_summary['macro_median_cumulative_saving']*100:.1f}% | Target-Iteration {population_summary['macro_median_incremental_saving']*100:.1f}%")
        print(f"  Pooled Savings:      Cumulative {population_summary['pooled_cumulative_saving']*100:.1f}% | Target-Iteration {population_summary['pooled_incremental_saving']*100:.1f}%")
        print("================================================================================\n")

    git_st = rc.git_state(rc.repo_root())
    corpus_p = Path(corpus_path).resolve() if corpus_path else None
    corpus_sha = None
    corpus_schema = None
    if corpus_p and corpus_p.is_file():
        cb = corpus_p.read_bytes()
        corpus_sha = hashlib.sha256(cb).hexdigest()
        try:
            corpus_schema = json.loads(cb.decode("utf-8")).get("schema")
        except Exception:
            pass

    import platform
    provenance = {
        "engine_path": eng.path,
        "engine_banner": eng.banner,
        "engine_sha256": eng.sha256,
        "engine_embedded_commit": extract_engine_embedded_commit(eng.banner),
        "tool_commit": git_st["commit_short"],
        "tool_dirty": git_st["dirty"],
        "tool_dirty_file_count": git_st["dirty_file_count"],
        "corpus_path": str(corpus_p) if corpus_p else None,
        "corpus_sha256": corpus_sha,
        "corpus_schema": corpus_schema,
        "command_line": " ".join(sys.argv),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    out_data = {
        "schema": "policy-research-p4-counterfactual/4",
        "provenance": provenance,
        "depth_mode": args.depth_mode,
        "preserve_aspiration": args.preserve_aspiration,
        "disable_fail_high_reduction": args.disable_fail_high_reduction,
        "depth": args.depth,
        "reference_depth": ref_depth,
        "candidate_depth": args.candidate_depth,
        "k": args.k,
        "score_tolerance_cp": args.score_tolerance,
        "candidate_set_framing": "empirical search-informed candidate shortlist from MultiPV at candidate_depth",
        "population_summary": population_summary,
        "results": all_results,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(out_data, indent=1) + "\n")
        print(f"\nArtifact saved to {args.out}")


if __name__ == "__main__":
    main()
