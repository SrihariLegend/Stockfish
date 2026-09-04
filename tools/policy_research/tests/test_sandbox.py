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


if __name__ == "__main__":
    unittest.main()
