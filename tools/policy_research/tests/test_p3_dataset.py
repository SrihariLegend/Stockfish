#!/usr/bin/env python3
"""Unit tests for the Phase 3 dataset collector/reporter (no engine required).

Run with:  python3 -m unittest discover -s tools/policy_research/tests -v
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import p3_dataset as p3

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _decision(serial=7, fen=START_FEN, alpha=0, beta=1, st=50, **kw):
    d = {
        "root_key": 1, "node_serial": serial, "key": 100 + serial,
        "ply": 12, "depth": 9, "root_iter_depth": 11, "alpha": alpha,
        "beta": beta, "static_eval": st, "improving": True, "tt_hit": False,
        "tt_move_present": False, "rule50": 30, "side_to_move": 0, "fen": fen,
    }
    d.update(kw)
    return d


def _attempt(serial=0, node_serial=7, outcome=2, move_uci="e2e4", **kw):
    a = {
        "root_key": 1, "node_serial": node_serial, "attempt_serial": serial,
        "move_raw": 0, "move_uci": move_uci, "gives_check": False,
        "is_tt_move": False, "quiet_ordinal": 3, "total_attempted": 6,
        "child_search_count": 1, "first_child_depth": 6, "research_depth": -1,
        "alpha_before": 0, "beta_before": 1, "value_returned": -5,
        "nodes_consumed": 3, "outcome": outcome,
    }
    a.update(kw)
    return a


class TestPieceLookup(unittest.TestCase):
    def test_corners_and_empty(self):
        self.assertEqual(p3._piece_at(START_FEN, "a1"), "R")
        self.assertEqual(p3._piece_at(START_FEN, "h8"), "r")
        self.assertEqual(p3._piece_at(START_FEN, "e2"), "P")
        self.assertEqual(p3._piece_at(START_FEN, "e4"), "")
        self.assertEqual(p3._piece_at(START_FEN, "i9"), "")
        self.assertEqual(p3._piece_at(START_FEN, ""), "")

    def test_castling_move_from_king_square(self):
        # black castled-ish middle-game placement: e8 has a king
        fen = "rnbqkbnr/pppp1ppp/4p3/8/8/4P3/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        self.assertEqual(p3._piece_at(fen, "e8"), "k")


class TestDerivedRow(unittest.TestCase):
    def test_margins_and_window(self):
        row = p3._derived(_decision(st=50), _attempt())
        self.assertEqual(row["window"], 1)
        self.assertEqual(row["margin_beta"], 1 - 50)
        self.assertEqual(row["margin_alpha"], 50 - 0)
        self.assertEqual(row["outcome_name"], "OBSERVED_FAIL_LOW")

    def test_fail_high_naming_and_move_type(self):
        d = _decision(st=-40)
        a = _attempt(outcome=1, value_returned=1, move_uci="e1g1",
                     move_raw=(6 << 6) | 4 | (3 << 12))  # CASTLING type
        row = p3._derived(d, a)
        self.assertEqual(row["outcome_name"], "OBSERVED_FAIL_HIGH")
        self.assertEqual(row["move_type"], "CASTLING")
        self.assertEqual(row["from_sq"], "e1")
        self.assertEqual(row["to_sq"], "g1")
        self.assertEqual(row["piece"], "K")

    def test_aborted_naming(self):
        row = p3._derived(_decision(), _attempt(outcome=3, value_returned=0))
        self.assertEqual(row["outcome_name"], "ABORTED_STOP")


class TestBucketsAndStatus(unittest.TestCase):
    def test_bucket_labels_half_open(self):
        s = pd.Series([0, 2, 3, 9])
        out = p3._bucket(s, [0, 3, np.inf])
        self.assertEqual(list(out), ["[0,3)", "[0,3)", ">=3", ">=3"])

    def test_status_table_shares_sum_to_one(self):
        df = pd.DataFrame({
            "outcome": [1, 2, 2, 3, 1],
            "outcome_name": ["OBSERVED_FAIL_HIGH", "OBSERVED_FAIL_LOW",
                             "OBSERVED_FAIL_LOW", "ABORTED_STOP",
                             "OBSERVED_FAIL_HIGH"],
            "nodes_consumed": [5, 1, 1, 0, 9],
        })
        tab = p3._status_table(df)
        self.assertAlmostEqual(float(tab["share"].sum()), 1.0)
        row = tab.set_index("outcome_name").loc["OBSERVED_FAIL_LOW"]
        self.assertEqual(int(row["rows"]), 2)
        self.assertEqual(int(row["nodes_total"]), 2)


class TestPav(unittest.TestCase):
    def test_unit_weights(self):
        out = p3._pav_monotone(np.array([1.0, 0.0, 2.0]),
                               np.array([1.0, 1.0, 1.0]))
        np.testing.assert_allclose(out, [0.5, 0.5, 2.0])

    def test_weighted_sums(self):
        # observations {1,1,0,0,0,2,2,2,2} -> block means 2/5 then 2 -> ok
        out = p3._pav_monotone(np.array([1.0, 0.0, 2.0]),
                               np.array([2.0, 3.0, 4.0]))
        np.testing.assert_allclose(out, [0.2, 0.2, 0.5])

    def test_already_monotone_is_identity(self):
        out = p3._pav_monotone(np.array([1.0, 2.0, 3.0]),
                               np.array([1.0, 1.0, 1.0]))
        np.testing.assert_allclose(out, [1.0, 2.0, 3.0])


class TestCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(11)
        n = 60000
        z = rng.normal(size=n)
        p_true = 1.0 / (1.0 + np.exp(-2.0 * z))
        y = (rng.random(n) < p_true).astype(int)
        cls.df = pd.DataFrame({
            "outcome": np.where(y == 1, 1, 2),
            "quiet_ordinal": np.clip(np.round(-z * 5 + 8), 1, 12).astype(int),
            "depth": 9, "root_iter_depth": 12, "total_attempted": 12,
            "ply": 12, "improving": False, "tt_hit": False,
            "tt_move_present": False, "gives_check": False, "is_tt_move": False,
            "margin_beta": 0, "margin_alpha": 0,
        })

    def test_logistic_learns_and_calibrates(self):
        fcols = ["quiet_ordinal", "depth", "root_iter_depth", "total_attempted",
                 "ply", "improving", "tt_hit", "tt_move_present", "gives_check",
                 "is_tt_move", "margin_beta", "margin_alpha"]
        res = p3._fit_and_evaluate(self.df, fcols)
        self.assertGreater(res["logistic_auc"], 0.70)
        self.assertLess(res["logistic_ece"], 0.10)
        # isotonic leg must not degrade: ECE after isotonic <= logistic ECE
        self.assertLessEqual(res["isotonic_ece"], res["logistic_ece"] + 1e-9)
        self.assertGreater(res["n_train"], res["n_test"])


class TestMdTable(unittest.TestCase):
    def test_markdown_shape(self):
        df = pd.DataFrame({"a": [1, 2], "b": [0.5, 1.5], "c": ["x", "y"]})
        out = p3._md_table(df)
        lines = out.splitlines()
        self.assertEqual(lines[0], "| a | b | c |")
        self.assertEqual(lines[1], "| --- | --- | --- |")
        self.assertIn("0.5", lines[2])


class TestReportSmoke(unittest.TestCase):
    def test_report_runs_on_synthetic_dir(self):
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            rows_dir = Path(tmp) / "rows" / "d17"
            rows_dir.mkdir(parents=True)
            rows = []
            for i in range(400):
                o = [1, 2, 2, 3][i % 4]
                rows.append(p3._derived(
                    _decision(serial=i, depth=1 + i % 17, st=60 - (i % 121),
                              improving=bool(i % 2), tt_hit=bool(i % 3)),
                    _attempt(serial=0, node_serial=i, outcome=o,
                             quiet_ordinal=1 + i % 8)))
            with open(rows_dir / "root-x.attempts.jsonl", "w") as fh:
                for r in rows:
                    fh.write(_json.dumps(r, sort_keys=True) + "\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = p3.report.__wrapped__ if hasattr(p3.report, "__wrapped__") else p3.report
                import argparse
                # call through the argparse-less path: build Namespace
                args = argparse.Namespace(dataset=tmp, only_depths=None)
                rc = p3.report(args)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(tmp) / "baseline-report.md").is_file())
            self.assertTrue((Path(tmp) / "baseline-report.json").is_file())
            md = (Path(tmp) / "baseline-report.md").read_text()
            self.assertIn("OBSERVED_FAIL_HIGH", md)


if __name__ == "__main__":
    unittest.main()
