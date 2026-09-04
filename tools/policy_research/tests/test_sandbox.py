#!/usr/bin/env python3
"""Unit tests for Phase 5 Internal-Node Counterfactual Sandbox.

Verifies isolated search worker mechanics and state encapsulation:
1. Deep Position cloning with complete StateInfo::previous chain re-linking
2. History isolation: mutations to shadow SharedHistories do not leak to live engine
3. Stack frame cloning and private pointer rebinding (continuationHistory and continuationCorrectionHistory)
4. Isolated shadow search execution with ResearchTTOverlay (both PV and NonPV zero-window)
5. Whole-node replay + force-next arms (C++ suite, Parts 8-11)
6. Versioned JSONL internal-counterfactual dataset collection (run_start/root_start/
   decision/root_end rows) with bit-identical live search behavior
7. Dataset-armed vs unarmed diagnostics for fixed-depth-only and Threads==1 gates
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ENGINE_PATH = os.environ.get("STOCKFISH_ENGINE", "src/stockfish")


class TestResearchSandbox(unittest.TestCase):
    def test_sandbox_cxx_unit_tests(self):
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        # Run policy_research_test_sandbox command
        proc = subprocess.run(
            [str(engine_bin)],
            input="policy_research_test_sandbox\nquit\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"Engine exited with code {proc.returncode}")
        self.assertIn("SANDBOX_TEST_OK", proc.stdout, f"Sandbox tests failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("SANDBOX_TEST_FAIL", proc.stdout)

    def run_search(self, cmds):
        """Send cmds (list of lines), read until bestmove, then quit."""
        engine_bin = Path(ENGINE_PATH)
        p = subprocess.Popen(
            [str(engine_bin)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for c in cmds:
                p.stdin.write(c)
            p.stdin.flush()
            lines = []
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                lines.append(line.strip())
                if line.startswith("bestmove"):
                    break
            p.stdin.write("quit\n")
            p.stdin.flush()
            p.wait(timeout=60)
            return lines
        finally:
            for s in (p.stdin, p.stdout, p.stderr):
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass

    def test_internal_counterfactual_live_non_perturbation(self):
        """Live search results/node counts must be bit-identical whether the
        internal counterfactual dataset is armed or not; armed runs write
        schema-valid JSONL rows (run_start/root_start/decision/root_end)."""
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        tmp = tempfile.mkdtemp(prefix="policy_research_sandbox_")
        dataset_path = os.path.join(tmp, "dataset.jsonl")

        def run_search(mode, dataset):
            cmds = [
                "isready\n",
                "setoption name PolicyResearch value on\n",
                f"setoption name PolicyResearchMode value {mode}\n",
                "setoption name PolicyResearchTopK value 2\n",
                "setoption name PolicyResearchNodeBudget value 50\n",
                "setoption name PolicyResearchSampleRate value 1.0\n",
                f"setoption name PolicyResearchLogPath value {dataset}\n",
                "position startpos moves e2e4 e7e5\n",
                "go depth 5\n",
            ]
            return self.run_search(cmds)

        lines_off = run_search("off", dataset_path)
        # Mode 'off' must never write a dataset file (checked before any armed run).
        self.assertFalse(os.path.exists(dataset_path), "Dataset file written while mode=off")
        lines_on = run_search("internal_counterfactual", dataset_path)

        bm_off = [l for l in lines_off if l.startswith("bestmove")][0]
        bm_on = [l for l in lines_on if l.startswith("bestmove")][0]
        self.assertEqual(bm_off, bm_on, f"Bestmove differs: {bm_off} vs {bm_on}")

        def d5_of(lines):
            d5 = [l for l in lines if l.startswith("info depth 5")]
            self.assertTrue(d5, "No depth 5 line in output")
            return d5[-1]

        d5_off, d5_on = d5_of(lines_off), d5_of(lines_on)
        nodes_off = re.search(r"nodes (\d+)", d5_off).group(1)
        nodes_on = re.search(r"nodes (\d+)", d5_on).group(1)
        self.assertEqual(nodes_off, nodes_on,
                         f"Live search nodes perturbed: {nodes_off} vs {nodes_on}")

        # Armed run wrote a versioned JSONL dataset with the full row lifecycle.
        self.assertTrue(os.path.exists(dataset_path), "No dataset file written in internal mode")
        with open(dataset_path) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        types = [r["type"] for r in rows]
        self.assertEqual(types[0], "run_start")
        self.assertEqual(types[1], "root_start")
        self.assertEqual(types[-1], "root_end")
        decisions = [r for r in rows if r["type"] == "decision"]
        self.assertGreater(len(decisions), 0, "No decision rows collected")
        for r in rows:
            self.assertEqual(r["schema"], "internal-counterfactual/2")
        d = decisions[0]
        for field in ("root_key", "pos_key", "fen", "ply", "depth", "entry_depth",
                      "root_depth", "alpha", "beta", "static_eval", "improving", "tt_hit",
                      "cut_node", "rule50", "fullmove", "sample_seed", "sample_rate",
                      "selection", "n_candidates", "node_budget", "baseline", "candidates",
                      "probes"):
            self.assertIn(field, d, f"decision row missing {field}")
        self.assertEqual(d["root_key"], d["pos_key"] or d["root_key"])
        for field in ("nodes", "decision_point_nodes", "completed", "stop", "budget_hit",
                      "value", "fail_high"):
            self.assertIn(field, d["baseline"], f"baseline missing {field}")
        b = d["baseline"]
        self.assertGreaterEqual(b["nodes"], 0)
        self.assertGreaterEqual(b["decision_point_nodes"], 0)
        self.assertLessEqual(b["decision_point_nodes"], b["nodes"])
        for cand in d["candidates"]:
            for field in ("move", "ordinal", "stage", "stage_score", "main_hist",
                          "capture_hist", "pawn_hist", "cont_hist", "low_ply_hist",
                          "see_bucket", "check", "capture", "tt_move", "prob", "selected"):
                self.assertIn(field, cand, f"candidate missing {field}")
            self.assertIn(cand["see_bucket"], (-1, 0, 1))
            if cand["capture"]:
                self.assertIsNone(cand["pawn_hist"])
                self.assertIsNone(cand["cont_hist"])
            else:
                self.assertIsNotNone(cand["pawn_hist"])
                self.assertIsNotNone(cand["cont_hist"])
        for probe in d["probes"]:
            for field in ("move", "prob", "nodes", "completed", "stop", "budget_hit",
                          "value", "forced_slot1", "fail_high"):
                self.assertIn(field, probe, f"probe missing {field}")
            if probe["completed"]:
                self.assertIsNotNone(probe["value"])
            else:
                self.assertIsNone(probe["value"], "censored probe must carry value null")
        for probe in d["probes"]:
            self.assertTrue(probe["forced_slot1"],
                            "Row kept although no forced replay consumed the force-next arm")
        self.assertEqual(d["baseline"]["nodes"], d["probes"][0]["nodes"] or 0)
        root_end = rows[-1]
        self.assertFalse(root_end["io_failed"])
        self.assertEqual(root_end["rows"], len(decisions))
        self.assertFalse(root_end["overflow"])
        self.assertEqual(root_end["target_depth"], rows[1]["target_depth"])
        self.assertEqual(root_end["target_nodes"], rows[1]["target_nodes"])
        # node_exit audit rows join the recorded decisions on the full identity
        # key; lifecycle rows bypass the decision-row cap.
        exits = [r for r in rows if r["type"] == "node_exit"]
        key = lambda r: (r["root_key"], r["pos_key"], r["ply"], r["entry_depth"],
                         r["sample_seed"])
        exit_keys = {key(r) for r in exits}
        self.assertEqual(len(exits), len(decisions))
        for d2 in decisions:
            self.assertIn(key(d2), exit_keys, "decision row without matching node_exit")
        self.assertIn("target_depth", rows[1])
        self.assertIn("target_nodes", rows[1])
        self.assertIn("achieved_depth", root_end)
        self.assertIn("searched_nodes", root_end)

    def test_dataset_armed_unarmed_diagnostics(self):
        """Fixed-depth-only and Threads==1 collection gates produce the skip
        diagnostics and leave no dataset behind for incompatible searches."""
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")
        tmp = tempfile.mkdtemp(prefix="policy_research_diag_")
        dataset_path = os.path.join(tmp, "dataset.jsonl")

        base = [
            "setoption name PolicyResearch value on\n",
            "setoption name PolicyResearchMode value internal_counterfactual\n",
            f"setoption name PolicyResearchLogPath value {dataset_path}\n",
            "position startpos moves e2e4 c7c5\n",
        ]

        # 1. Missing log path.
        lines = self.run_search(
            [l for l in base if "LogPath" not in l] + ["go depth 5\n"])
        self.assertTrue(any("PolicyResearchLogPath" in l and "skipping" in l for l in lines),
                        f"no missing-path diagnostic:\n{lines}")
        self.assertFalse(os.path.exists(dataset_path))

        # 2. movetime (not fixed-depth) is rejected.
        lines = self.run_search(base + ["go movetime 1000\n"])
        self.assertTrue(any("fixed-depth or fixed-node" in l and "skipping" in l for l in lines),
                        f"no fixed-only diagnostic:\n{lines}")
        self.assertFalse(os.path.exists(dataset_path))

        # 3. Threads != 1 is rejected even for fixed depth.
        lines = self.run_search(
            base + ["setoption name Threads value 2\n", "go depth 5\n"])
        self.assertTrue(any("Threads=1" in l and "skipping" in l for l in lines),
                        f"no Threads diagnostic:\n{lines}")
        self.assertFalse(os.path.exists(dataset_path))

    def test_enumerate_candidates_uci_command(self):
        """Verifies policy_research_enumerate_candidates command outputs the
        full legal denominator with MovePicker-exact stages and ordinals."""
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        proc = subprocess.run(
            [str(engine_bin)],
            input="position startpos\npolicy_research_enumerate_candidates 3\nquit\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("info string candidates_begin count 20", proc.stdout)
        self.assertIn("info string candidates_end total 20", proc.stdout)
        cand_lines = [l for l in proc.stdout.splitlines() if " candidate move " in l]
        self.assertEqual(len(cand_lines), 20)
        for i, l in enumerate(cand_lines):
            self.assertIn(f" ordinal {i} ", l, f"ordinal not contiguous at {i}: {l}")
        first = cand_lines[0]
        self.assertIn("is_tt_move 0", first)  # no ttMove in this diagnostic drive
        stages = set(re.search(r"stage (\w+)", l).group(1) for l in cand_lines)
        self.assertTrue(stages <= {"tt", "good_capture", "bad_capture", "good_quiet",
                                   "bad_quiet"}, f"unexpected stage names: {stages}")
        # Deep-ish drive from a capture-rich Kiwipete position exercises all
        # capture stages; only structurally valid output is required here.
        proc = subprocess.run(
            [str(engine_bin)],
            input=("position fen r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/"
                   "PPPBBPPP/R3K2R w KQkq - 0 1\n"
                   "policy_research_enumerate_candidates 2\nquit\n"),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("info string candidates_begin count 48", proc.stdout)
        self.assertIn("info string candidates_end total 48", proc.stdout)


if __name__ == "__main__":
    unittest.main()
