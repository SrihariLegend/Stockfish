#!/usr/bin/env python3
"""Unit tests for Phase 5 Internal-Node Counterfactual Sandbox.

Verifies isolated search worker mechanics and state encapsulation:
1. Deep Position cloning with complete StateInfo::previous chain re-linking
2. History isolation: mutations to shadow SharedHistories do not leak to live engine
3. Stack frame cloning and private pointer rebinding (continuationHistory and continuationCorrectionHistory)
4. Isolated shadow search execution with ResearchTTOverlay (both PV and NonPV zero-window):
   - Shadow search produces valid evaluation
   - Shadow worker searched positive nodes while live worker nodes remain 0
   - Live position and base TT remain pristine and unmutated
   - Overlay captures isolated TT entries
   - Deterministic search produces identical scores and node counts
"""

import os
import re
import subprocess
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

    def test_internal_counterfactual_live_non_perturbation(self):
        """Verifies that live search results and node counts are 100% identical

        whether internal counterfactual probes are enabled or disabled.
        """
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        def run_search(mode):
            p = subprocess.Popen(
                [str(engine_bin)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            cmds = [
                "isready\n",
                "setoption name PolicyResearch value true\n",
                f"setoption name PolicyResearchMode value {mode}\n",
                "setoption name PolicyResearchTopK value 2\n",
                "setoption name PolicyResearchNodeBudget value 50\n",
                "setoption name PolicyResearchSampleRate value 1.0\n",
                "position startpos moves e2e4 e7e5\n",
                "go depth 5\n",
            ]
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
            p.wait(timeout=10)
            if p.stdin:
                p.stdin.close()
            if p.stdout:
                p.stdout.close()
            if p.stderr:
                p.stderr.close()
            bm = [l for l in lines if l.startswith("bestmove")][0]
            d5_lines = [l for l in lines if l.startswith("info depth 5")]
            self.assertTrue(d5_lines, f"No depth 5 line in output for mode {mode}")
            d5 = d5_lines[-1]
            counterfactuals = [l for l in lines if "internal_counterfactual" in l]
            return bm, d5, counterfactuals

        bm_off, d5_off, cf_off = run_search("off")
        bm_on, d5_on, cf_on = run_search("internal_counterfactual")

        self.assertEqual(bm_off, bm_on, f"Bestmove differs: {bm_off} vs {bm_on}")
        nodes_off = re.search(r"nodes (\d+)", d5_off).group(1)
        nodes_on = re.search(r"nodes (\d+)", d5_on).group(1)
        self.assertEqual(nodes_off, nodes_on, f"Live search nodes perturbed: {nodes_off} vs {nodes_on}")
        self.assertEqual(len(cf_off), 0, "Counterfactual telemetry emitted when mode is off")
        self.assertGreater(len(cf_on), 0, "No counterfactual telemetry emitted when mode is active")
        for line in cf_on:
            self.assertIn("internal_counterfactual", line)
            self.assertIn("candidates", line)

    def test_enumerate_candidates_uci_command(self):
        """Verifies policy_research_enumerate_candidates command outputs full legal denominator."""
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        proc = subprocess.run(
            [str(engine_bin)],
            input="position startpos\npolicy_research_enumerate_candidates\nquit\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("info string candidates_begin count 20", proc.stdout)
        self.assertIn("info string candidates_end total 20", proc.stdout)
        self.assertIn("candidate move e2e4", proc.stdout)
        self.assertIn("candidate move g1f3", proc.stdout)


if __name__ == "__main__":
    unittest.main()
