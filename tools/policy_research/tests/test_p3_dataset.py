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
import stats

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
        out = stats.pav_isotonic(np.array([1.0, 0.0, 2.0]),
                                 np.array([1.0, 1.0, 1.0]))
        np.testing.assert_allclose(out, [0.5, 0.5, 2.0])

    def test_weighted_sums(self):
        # observations {1,1,0,0,0,2,2,2,2} -> block means 2/5 then 2 -> ok
        out = stats.pav_isotonic(np.array([1.0, 0.0, 2.0]),
                                 np.array([2.0, 3.0, 4.0]))
        np.testing.assert_allclose(out, [0.2, 0.2, 0.5])

    def test_already_monotone_is_identity(self):
        out = stats.pav_isotonic(np.array([1.0, 2.0, 3.0]),
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
                # node identity + local cost (within-node probe inputs)
                "root_depth": 9, "node_serial": np.arange(n),
                "nodes_consumed": rng.integers(1, 9, size=n),
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
        # within-node reordering probe + root-balanced sensitivity present
        self.assertIn("reorder_probe_test", full)
        pr = full["reorder_probe_test"]
        self.assertIn("pairs", pr)
        self.assertIn("node_above_all_share", pr)
        rb = res["root_balanced"]
        self.assertGreater(rb["logistic_test"]["pooled"]["auc"], 0.70)
        self.assertIn("coefficient_delta_vs_row", rb)
        self.assertEqual(len(rb["coefficient_delta_vs_row"]),
                         1 + len(res["features"]["full"]))
        # uniform P3.2-style data: no IPW path triggered
        self.assertFalse(res["weights"]["ipw_non_uniform"])

    def test_grouped_calibration_ipw_non_uniform(self):
        df = _synthetic_cal_df()
        df["node_weight"] = np.where(
            df["position_set"] == "development",
            np.where(df["position_id"] == "dev1", 2.0, 0.5), 1.0)
        res = p3._grouped_calibration(df)
        self.assertTrue(res["weights"]["ipw_non_uniform"])
        # root-balanced fit still well above chance under non-uniform data
        self.assertGreater(
            res["root_balanced"]["logistic_test"]["pooled"]["auc"], 0.70)
        self.assertIn("reorder_probe_test", res["models"]["full"])

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


def _set_node_weights(tmp, wmap):
    """Rewrite node_weight on attempt rows per root (IPW smoke test)."""
    for p in sorted(Path(tmp).glob("rows/*/root-*.attempts.jsonl")):
        rid = p.name.removeprefix("root-").removesuffix(".attempts.jsonl")
        w = wmap.get(rid)
        if w is None:
            continue
        lines = []
        for line in p.read_text().splitlines():
            rec = _json.loads(line)
            rec["node_weight"] = float(w)
            lines.append(_json.dumps(rec, sort_keys=True))
        p.write_text("\n".join(lines) + "\n")


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
            # report/3 additions
            self.assertEqual(js["schema_version"], "research-baseline-report/3")
            self.assertTrue(js["weights"]["uniform"])
            self.assertIn("within_node_probe", js)
            self.assertIn("root_balanced_sensitivity", js)
            self.assertIn("coefficient_delta_vs_row", js)
            self.assertIn("opportunity_accounting", js)
            self.assertIn("reliability_worst_gap", js)
            self.assertIn("reorder_probe_test", js["calibration"]["models"]["full"])
            self.assertIn("Root-balanced sensitivity", mp.read_text())

    def test_report_runs_with_nonuniform_node_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            _synthetic_dataset_dir(tmp)
            _set_node_weights(tmp, {"rdev": 1.0, "rval": 3.0, "rtest": 2.0})
            buf = io.StringIO()
            import argparse
            with redirect_stdout(buf):
                rc = p3.report(argparse.Namespace(dataset=tmp,
                                                  only_depths=None))
            self.assertEqual(rc, 0)
            js = _json.loads((Path(tmp) / "baseline-report.json").read_text())
            self.assertFalse(js["weights"]["uniform"])
            self.assertTrue(js["calibration"]["weights"]["ipw_non_uniform"])
            md = (Path(tmp) / "baseline-report.md").read_text()
            self.assertIn("node_weight-weighted", md)
            self.assertIn("Sampling weights", md)
            self.assertIn("within_node_probe", js)


class TestReorderProbe(unittest.TestCase):
    def test_probe_counts_and_accuracy(self):
        rows = [
            # (root, rdepth, node, ordinal, outcome, cost, pred)
            # root r1 node A: two fail-low predecessors, late cutoff above
            # both -> model correct
            ("r1", 1, 1, 1, 2, 5, 0.30),
            ("r1", 1, 1, 2, 2, 3, 0.40),
            ("r1", 1, 1, 3, 1, 1, 0.90),
            # root r1 node B: cutoff at ordinal 1 -> not late
            ("r1", 1, 2, 1, 1, 2, 0.50),
            # root r1 node C: late cutoff but model prefers the predecessor
            ("r1", 1, 3, 1, 2, 9, 0.80),
            ("r1", 1, 3, 2, 1, 2, 0.20),
            # root r2 node D: correct
            ("r2", 1, 1, 1, 2, 4, 0.10),
            ("r2", 1, 1, 2, 1, 1, 0.60),
            # root r2 node E: two predecessors, late cutoff above both
            ("r2", 1, 2, 1, 2, 2, 0.70),
            ("r2", 1, 2, 2, 2, 1, 0.30),
            ("r2", 1, 2, 3, 1, 4, 0.90),
        ]
        f = pd.DataFrame(rows, columns=["position_id", "root_depth",
                                        "node_serial", "quiet_ordinal",
                                        "outcome", "nodes_consumed",
                                        "pred"])
        res = p3._reorder_probe(f, f["pred"].to_numpy())
        self.assertEqual(res["nodes_with_quiet_attempt"], 5)
        self.assertEqual(res["nodes_quiet_cutoff"], 5)
        self.assertEqual(res["nodes_late_cutoff"], 4)
        self.assertEqual(res["pairs"], 6)
        # A(2 ok), C(1 wrong), D(1 ok), E(2 ok) => 5/6
        self.assertAlmostEqual(res["pair_acc"], 5.0 / 6.0)
        self.assertAlmostEqual(res["pair_strict"], 5.0 / 6.0)
        self.assertAlmostEqual(res["pair_ties"], 0.0)
        # cost-weighted: (5+3+0+4+2+1)/(5+3+9+4+2+1)
        self.assertAlmostEqual(res["cost_weighted_acc"], 15.0 / 24.0)
        # above all predecessors: A yes, C no, D yes, E yes => 3/4
        self.assertAlmostEqual(res["node_above_all_share"], 0.75)
        # macro over the two roots: r1 2/3, r2 1
        self.assertEqual(res["macro"]["pair_acc"]["n_roots"], 2)
        self.assertAlmostEqual(
            res["macro"]["pair_acc"]["macro_mean"], 5.0 / 6.0)

    def test_probe_empty_when_no_late_cutoffs(self):
        f = pd.DataFrame({
            "position_id": ["r1"] * 3, "root_depth": [1] * 3,
            "node_serial": [1] * 3, "quiet_ordinal": [1, 2, 3],
            "outcome": [1, 2, 2], "nodes_consumed": [1, 1, 1],
        })
        res = p3._reorder_probe(f, np.array([0.1, 0.2, 0.3]))
        self.assertEqual(res["nodes_late_cutoff"], 0)
        self.assertEqual(res["pairs"], 0)
        self.assertTrue(np.isnan(res["pair_acc"]))


class TestWeights(unittest.TestCase):
    def test_prep_weights_uniform_or_none(self):
        self.assertIsNone(p3._prep_weights(None))
        self.assertIsNone(p3._prep_weights(np.ones(10)))
        self.assertIsNone(p3._prep_weights(np.full(5, 2.5)))
        w = p3._prep_weights(np.array([1.0, 2.0, 1.0]))
        np.testing.assert_allclose(w, [1.0, 2.0, 1.0])

    def test_weighted_rate(self):
        self.assertAlmostEqual(p3._weighted_rate(
            np.array([1.0, 0.0, 1.0]), np.array([1.0, 1.0, 2.0])), 0.75)

    def test_quantile_monotone_and_direction(self):
        x = np.array([1.0, 2.0, 3.0, 100.0])
        q = np.array([0.1, 0.5, 0.9])
        a = stats.weighted_quantile(x, np.ones(4), q)
        self.assertTrue(np.all(np.diff(a) >= 0))
        # moving all mass to the largest value pushes interior quantiles up
        c = stats.weighted_quantile(x, np.array([0.0, 0.0, 0.0, 1.0]), q)
        self.assertGreater(c[1], a[1])
        self.assertGreater(c[2], a[2])

    def test_macro_rate_ipw_pooled_and_rootwise(self):
        frame = pd.DataFrame({
            "position_id": ["a"] * 3 + ["b"] * 3,
            "ev": [1, 0, 0, 0, 1, 1],
            "w": [1.0, 1.0, 1.0, 10.0, 10.0, 10.0],
        })
        t = p3._macro_rate_table(frame, pd.Series(["x"] * 6),
                                 frame["ev"] == 1, w=frame["w"])
        row = t.iloc[0]
        # pooled IPW = (1 + 20) / (3 + 30)
        self.assertAlmostEqual(row["event_rate"], 21.0 / 33.0, places=12)
        # per-root rates remain unweighted means: 1/3 and 2/3
        self.assertAlmostEqual(row["macro_mean"], 0.5, places=12)

    def test_weighted_logistic_parity_unit(self):
        rng = np.random.default_rng(3)
        X = rng.normal(size=(400, 3))
        p = 1.0 / (1.0 + np.exp(-(X[:, 0] - 0.5 * X[:, 1])))
        y = (rng.random(400) < p).astype(float)
        mu, sd = X.mean(0), X.std(0)
        sd[sd == 0] = 1.0
        Z = (X - mu) / sd
        c1, b1, _, _ = stats.irls_logistic(
            Z, y, lam=1e-3, penalize_intercept=False, tol=1e-9, max_iter=80)
        c2, b2, _, _ = stats.irls_logistic(
            Z, y, lam=1e-3, penalize_intercept=False, tol=1e-9, max_iter=80,
            weights=np.ones(len(y)))
        np.testing.assert_allclose(c1, c2, atol=1e-12)
        np.testing.assert_allclose(b1, b2, atol=1e-12)

    def test_weighted_logistic_sign_flip_under_imbalance(self):
        rng = np.random.default_rng(5)
        z = rng.uniform(-1.0, 1.0, size=1000)
        y0 = (z > 0).astype(float)
        y = y0.copy()
        y[:100] = 1.0 - y0[:100]  # first 100 rows: flipped relation
        Z = z[:, None]
        w = np.ones(1000)
        w[:100] = 60.0
        c_unw = stats.irls_logistic(
            Z, y, lam=1e-3, penalize_intercept=False, tol=1e-9,
            max_iter=80)[0][0]
        c_ipw = stats.irls_logistic(
            Z, y, lam=1e-3, penalize_intercept=False, tol=1e-9, max_iter=80,
            weights=w)[0][0]
        self.assertGreater(c_unw, 0)
        self.assertLess(c_ipw, 0)

    def test_weighted_isotonic_parity_unit(self):
        rng = np.random.default_rng(7)
        p = np.sort(rng.random(500))
        y = (rng.random(500) < p).astype(float)
        e1, c1 = p3._fit_isotonic_map(p, y)
        e2, c2 = p3._fit_isotonic_map(p, y, row_w=np.ones(500))
        np.testing.assert_allclose(e1, e2)
        np.testing.assert_allclose(c1, c2)

    def test_root_equal_weights_under_volume_imbalance(self):
        frame = pd.DataFrame({
            "position_id": ["big"] * 300 + ["small"] * 30,
            "node_weight": [2.0] * 330,
        })
        w = p3._root_equal_weights(frame)
        self.assertAlmostEqual(float(w[:300].sum()), 1.0, places=9)
        self.assertAlmostEqual(float(w[300:].sum()), 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
