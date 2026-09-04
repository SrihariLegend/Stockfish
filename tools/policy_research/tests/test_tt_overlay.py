#!/usr/bin/env python3
"""Unit test for ResearchTTOverlay.

Verifies copy-on-write TT overlay mechanics via the engine's internal test harness:
1. Base miss -> read miss, private cluster creation, non-mutation of base
2. Base hit -> initial value equivalence, private cluster copy, isolated write
3. Private write isolation -> base TT remains pristine
4. Penalize isolation -> overlay penalizes depth without altering base TT
5. Clear overlay -> reverts overlay view to pristine base TT state
"""

import os
import subprocess
import unittest
from pathlib import Path

ENGINE_PATH = os.environ.get("STOCKFISH_ENGINE", "src/stockfish")


class TestResearchTTOverlay(unittest.TestCase):
    def test_overlay_cxx_unit_tests(self):
        engine_bin = Path(ENGINE_PATH)
        if not engine_bin.is_file():
            self.skipTest(f"Engine binary {ENGINE_PATH} not found")

        # Run policy_research_test_overlay command
        proc = subprocess.run(
            [str(engine_bin)],
            input="policy_research_test_overlay\nquit\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("OVERLAY_TEST_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
