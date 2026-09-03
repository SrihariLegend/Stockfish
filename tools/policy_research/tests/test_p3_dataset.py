#!/usr/bin/env python3
"""Unit tests for the Phase 3 dataset collector/reporter (no engine required).

Run with:  python3 -m unittest discover -s tools/policy_research/tests -v
"""

import io
import sys
import unittest
import tempfile
import json as _json
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


class TestMacroHelpers(unittest.TestCase):
    def test_macro_fields(self):
        f = p3._macro_fields([0.2, 0.4, 0.6])
        self.assertAlmostEqual(f["macro_mean"], 0.4)
        self.assertAlmostEqual(f["macro_sd"], 0.2)
        self.assertEqual(f["n_roots"], 3)

    def test_macro_rate_table(self):
        # two roots with known event counts
        frame = pd.DataFrame({
            "position_id": ["a"] * 10 + ["b"] * 10,
            "bucket": ["[0,1)"] * 10 + ["[0,1)"] * 10,
            "event": [1] * 2 + [0] * 8 + [1] * 6 + [0] * 4,
        })
        t = p3._macro_rate_table(
            frame, pd.Categorical(frame["bucket"]), frame["event"])
        row = t.iloc[0]
        self.assertEqual(int(row["rows"]), 20)
        self.assertEqual(int(row["events"]), 8)
        self.assertAlmostEqual(row["event_rate"], 0.4)
        # macro mean = mean of per-root rates (0.2 and 0.6)
        self.assertAlmostEqual(row["macro_mean"], 0.4)
        self.assertEqual(int(row["n_roots"]), 2)

    def test_macro_quant_table(self):
        frame = pd.DataFrame({
            "position_id": ["a"] * 4 + ["b"] * 4,
            "bucket": ["x"] * 8,
            "value": [1, 2, 3, 4, 5, 6, 7, 8],
        })
        t = p3._macro_quant_table(frame, pd.Categorical(frame["bucket"]),
                                  frame["value"])
        row = t.iloc[0]
        self.assertAlmostEqual(row["mean"], 4.5)
        self.assertAlmostEqual(row["median"], 4.5)
        self.assertEqual(int(row["n_roots"]), 2)
        self.assertAlmostEqual(row["macro_mean"], 4.5)


class TestNodeFrame(unittest.TestCase):
    def _frame(self):
        dec = pd.DataFrame([
            {"position_id": "r1", "position_set": "test", "root_depth": 17,
             "node_serial": 1, "key": 1, "ply": 2, "depth": 8,
             "root_iter_depth": 10, "alpha": 0, "beta": 1, "static_eval": 0,
             "improving": True, "tt_hit": False, "tt_move_present": False,
             "rule50": 30, "side_to_move": 0, "fen": START_FEN,
             "node_weight": 2.0},
            {"position_id": "r1", "position_set": "test", "root_depth": 17,
             "node_serial": 2, "key": 2, "ply": 2, "depth": 6,
             "root_iter_depth": 10, "alpha": 0, "beta": 1, "static_eval": 0,
             "improving": False, "tt_hit": False, "tt_move_present": False,
             "rule50": 30, "side_to_move": 0, "fen": START_FEN,
             "node_weight": 2.0},
            {"position_id": "r1", "position_set": "test", "root_depth": 17,
             "node_serial": 3, "key": 3, "ply": 2, "depth": 4,
             "root_iter_depth": 10, "alpha": 0, "beta": 1, "static_eval": 0,
             "improving": False, "tt_hit": False, "tt_move_present": False,
             "rule50": 30, "side_to_move": 0, "fen": START_FEN,
             "node_weight": 2.0},
        ])
        att = pd.DataFrame([
            # node 1: fail-low then quiet cutoff
            {"position_id": "r1", "root_depth": 17, "node_serial": 1,
             "quiet_ordinal": 1, "nodes_consumed": 4, "outcome": 2},
            {"position_id": "r1", "root_depth": 17, "node_serial": 1,
             "quiet_ordinal": 2, "nodes_consumed": 7, "outcome": 1},
            # node 2: two fail-lows, no cutoff
            {"position_id": "r1", "root_depth": 17, "node_serial": 2,
             "quiet_ordinal": 1, "nodes_consumed": 2, "outcome": 2},
            {"position_id": "r1", "root_depth": 17, "node_serial": 2,
             "quiet_ordinal": 2, "nodes_consumed": 3, "outcome": 2},
            # node 3: no quiet attempts at all
        ])
        return dec, att

    def test_wasted_before_cut(self):
        dec, att = self._frame()
        node = p3._node_frame(att, dec)
        n1 = node[node["node_serial"] == 1].iloc[0]
        self.assertEqual(int(n1["n_fh"]), 1)
        self.assertEqual(int(n1["fh_ord"]), 2)
        self.assertEqual(int(n1["wasted_before_cut"]), 4)
        n2 = node[node["node_serial"] == 2].iloc[0]
        self.assertEqual(int(n2["n_fh"]), 0)
        self.assertTrue(np.isnan(n2["wasted_before_cut"]))
        self.assertEqual(int(n2["no_cut_quiet_cost"]), 5)
        n3 = node[node["node_serial"] == 3].iloc[0]
        self.assertEqual(int(n3["n_quiet"]), 0)

    def test_two_cutoffs_raises(self):
        dec, att = self._frame()
        att2 = pd.concat([att, pd.DataFrame([
            {"position_id": "r1", "root_depth": 17, "node_serial": 1,
             "quiet_ordinal": 3, "nodes_consumed": 1, "outcome": 1}])],
            ignore_index=True)
        with self.assertRaises(RuntimeError):
            p3._node_frame(att2, dec)


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


def _synthetic_cal_df(seed=11):
    """Rows across development/validation/test roots with a strong signal."""
    rng = np.random.default_rng(seed)
    n_per = 6000
    sets = [("development", ["dev1", "dev2"]),
            ("validation", ["val1"]), ("test", ["test1"])]
    parts = []
    for pset, roots in sets:
        for rid in roots:
            n = n_per // len(roots)
            z = rng.normal(size=n)
            lo = rng.normal(size=n)
            p_true = 1.0 / (1.0 + np.exp(-(1.8 * z - 0.7 * lo)))
            y = (rng.random(n) < p_true).astype(int)
            df = pd.DataFrame({
                "position_id": rid, "position_set": pset,
                "outcome": np.where(y == 1, 1, 2),
                "quiet_ordinal": np.clip(np.round(-z * 4 + 6), 1, 12).astype(int),
                "total_attempted": 12, "depth": 9, "root_iter_depth": 12,
                "ply": 12, "improving": False, "tt_hit": False,
                "tt_move_present": False, "gives_check": False,
                "is_tt_move": False, "margin_beta": np.round(300 * lo).astype(int),
            })
            parts.append(df)
    return pd.concat(parts, ignore_index=True)


class TestGroupedCalibration(unittest.TestCase):
    def test_grouped_run(self):
        df = _synthetic_cal_df()
        res = p3._grouped_calibration(df)
        design = res["design"]
        self.assertEqual(design["rows_test"], 6000)
        self.assertEqual(design["test_roots"], ["test1"])
        self.assertIn("full", res["models"])
        full = res["models"]["full"]
        self.assertTrue(full["converged"])
        # coefficients: intercept + feature count
        self.assertEqual(len(res["coefficients_full"]),
                         1 + len(res["features"]["full"]))
        # margin_alpha must not appear anywhere in the feature sets
        self.assertNotIn("margin_alpha", res["features"]["full"])
        # pooled test AUC on synthetic signal should be well above chance
        self.assertGreater(full["test"]["pooled"]["auc"], 0.70)
        self.assertIn("isotonic_test", full)
        self.assertLess(full["isotonic_test"]["pooled"]["n"], design["rows_test"] + 1)
        # reliability tables present
        self.assertGreater(len(full["logistic_reliability_test"]), 0)

    def test_grouped_requires_each_set(self):
        df = _synthetic_cal_df()
        df = df[df["position_set"] != "validation"]
        with self.assertRaises(RuntimeError):
            p3._grouped_calibration(df)


class TestMdTable(unittest.TestCase):
    def test_markdown_shape(self):
        df = pd.DataFrame({"a": [1, 2], "b": [0.5, 1.5], "c": ["x", "y"]})
        out = p3._md_table(df)
        lines = out.splitlines()
        self.assertEqual(lines[0], "| a | b | c |")
        self.assertEqual(lines[1], "| --- | --- | --- |")
        self.assertIn("0.5", lines[2])


def _synthetic_dataset_dir(tmp):
    """One dataset dir with roots in each corpus set (report smoke)."""
    rows_dir = Path(tmp) / "rows" / "d17"
    rows_dir.mkdir(parents=True)
    sets = [("development", ["rdev"]), ("validation", ["rval"]),
            ("test", ["rtest"])]
    for pset, roots in sets:
        for rid in roots:
            for i in range(300):
                o = [1, 2, 2, 2][i % 4]
                d = _decision(serial=i, depth=1 + i % 17, st=60 - (i % 121),
                              improving=bool(i % 2), tt_hit=bool(i % 3))
                att = _attempt(serial=0, node_serial=i, outcome=o,
                               quiet_ordinal=1 + i % 8,
                               nodes_consumed=1 + i % 7)
                row = p3._derived(d, att)
                row.update({"position_id": rid, "position_set": pset,
                            "root_depth": 17, "node_weight": 2.0})
                with open(rows_dir / f"root-{rid}.attempts.jsonl", "a") as fh:
                    fh.write(_json.dumps(row, sort_keys=True) + "\n")
                dec = {"position_id": rid, "position_set": pset,
                       "root_depth": 17, "node_serial": i, "key": 100 + i,
                       "ply": 12, "depth": 1 + i % 17,
                       "root_iter_depth": 17, "alpha": d["alpha"],
                       "beta": d["beta"], "static_eval": d["static_eval"],
                       "improving": d["improving"], "tt_hit": d["tt_hit"],
                       "tt_move_present": d["tt_move_present"],
                       "rule50": 30, "side_to_move": 0,
                       "fen": START_FEN, "node_weight": 2.0}
                with open(rows_dir / f"root-{rid}.decisions.jsonl", "a") as fh:
                    fh.write(_json.dumps(dec, sort_keys=True) + "\n")


class TestReportSmoke(unittest.TestCase):
    def test_report_runs_on_synthetic_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            _synthetic_dataset_dir(tmp)
            buf = io.StringIO()
            import argparse
            with redirect_stdout(buf):
                args = argparse.Namespace(dataset=tmp, only_depths=None)
                rc = p3.report(args)
            self.assertEqual(rc, 0)
            jp = Path(tmp) / "baseline-report.json"
            mp = Path(tmp) / "baseline-report.md"
            self.assertTrue(mp.is_file())
            self.assertTrue(jp.is_file())
            js = _json.loads(jp.read_text())
            self.assertIn("OBSERVED_FAIL_HIGH", mp.read_text())
            self.assertIn("macro_mean", mp.read_text())
            self.assertIn("node_level_summary", js)
            self.assertIn("calibration", js)
            self.assertIn("calibration_table", js)
            self.assertEqual(js["limitations"]["n_test_roots"], 1)


if __name__ == "__main__":
    unittest.main()
