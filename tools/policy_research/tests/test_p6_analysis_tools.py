"""Tests for the p5b_analysis / p6_explanatory analysis tools.

Uses synthetic internal-counterfactual/3 rows (no evidence files): exercises
ratio-of-sums aggregation, oracle definitions, abstention semantics, the
FH->FL/FL->FH accounting, and the measurement-A categorization.
Convention in the fixture: fail_high == (value >= 0); the baseline carries
value 42 (FH) or -42 (FL) unless overridden.
"""
import gzip
import json
import os
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import p5b_analysis
import p6_explanatory

MOVES = ["p0", "p1", "p2", "p3"] + [f"q{i}" for i in range(4)]


def make_row(base_nodes=100, base_fh=True, own=None, cost=None, value=None):
    """Synthetic decision row.

    own[k] (k in 1..3): True when the forced probe of ordinal k cut off by
    itself at slot 1. cost[k]: forced whole-node nodes. value[k]: forced
    whole-node value (fail_high = value >= 0). Defaults: no own-cuts;
    probes preserve the baseline classification and cost base_nodes + 50.
    """
    own = own or {}
    cost = cost or {}
    value = value or {}
    base_v = 42 if base_fh else -42
    probes = []
    for o in (1, 2, 3):
        v = value.get(o, base_v)
        c = cost.get(o, base_nodes + 50)
        owncut = bool(own.get(o, False))
        probes.append({
            "move": MOVES[o], "prob": 1.0, "nodes": c, "completed": True,
            "stop": "none", "budget_hit": False, "value": v,
            "forced_slot1": True, "fail_high": v >= 0,
            "first": {"emitted": True, "searched": True, "child_nodes": c,
                      "value": v},
            "cutoff": {"cutoff_seen": owncut, "cutoff_move": MOVES[o],
                       "cutoff_ordinal": o if owncut else None,
                       "cutoff_value": v if owncut else None,
                       "cutoff_by_first": owncut},
            "prefix_pops": 0,
        })
    return {
        "schema": "internal-counterfactual/3", "type": "decision",
        "root_key": 1, "pos_key": 2, "fen": "", "ply": 3, "entry_depth": 4,
        "depth": 3, "root_depth": 8, "alpha": -20, "beta": -19,
        "static_eval": 0, "improving": True, "tt_hit": False, "tt_move": None,
        "cut_node": False, "rule50": 0, "fullmove": 1, "sample_seed": 5,
        "sample_id": 6, "sample_rate": 1.0, "selection": "probe_all",
        "n_candidates": 8, "node_budget": 5000,
        "baseline": {"nodes": base_nodes, "decision_point_nodes": 3,
                     "ss1_cutoff_cnt": 0, "completed": True, "stop": "none",
                     "budget_hit": False, "value": base_v,
                     "fail_high": base_fh,
                     "first": {"emitted": True, "searched": True,
                               "child_nodes": 4, "value": base_v},
                     "cutoff": {"cutoff_seen": base_fh, "cutoff_move": MOVES[0],
                                "cutoff_ordinal": 0 if base_fh else None,
                                "cutoff_value": base_v if base_fh else None,
                                "cutoff_by_first": base_fh}},
        "candidates": [
            {"move": MOVES[i], "ordinal": i, "stage": "tt",
             "stage_score": None, "main_hist": 0, "capture_hist": None,
             "pawn_hist": None, "cont_hist": None, "low_ply_hist": None,
             "see_bucket": 0, "check": False, "capture": False,
             "tt_move": i == 0, "prob": 1.0, "selected": True}
            for i in range(8)],
        "probes": probes,
    }


class P6PolicyTests(unittest.TestCase):
    def test_oracle_cls_minimizes_among_same_classification(self):
        # ordinal 1 cheaper (60) but flips FH->FL (value -30);
        # ordinal 2 at 70 preserves FH and is the cheapest preserving.
        r = make_row(base_nodes=100, base_fh=True,
                     cost={1: 60, 2: 70, 3: 120},
                     value={1: -30, 2: 42, 3: 42})
        out = p6_explanatory.choose(r)
        self.assertEqual(out["ORACLE_CLS"], (70, 2))
        self.assertEqual(out["CHEAP_UNC"], (60, 1))
        self.assertEqual(out["ORACLE_EXACT"], (70, 2))
        self.assertEqual(out["BASE"], (100, 0))

    def test_q1_forced_promotes_expensive_owncut(self):
        # Only ordinal 1 self-cuts; its forced cost (200) exceeds baseline.
        r = make_row(base_nodes=100, own={1: True}, cost={1: 200}, value={1: 60})
        out = p6_explanatory.choose(r)
        # The forced-promotion variant must promote and lose to baseline...
        self.assertEqual(out["Q1_CHEAPEST_FORCED"], (200, 1))
        # ...while the abstaining variant keeps baseline.
        self.assertEqual(out["Q1_CHEAPEST_ABSTAIN"], (100, 0))
        self.assertEqual(out["Q1_SAFE_ABSTAIN"], (100, 0))

    def test_q1_abstain_promotes_when_owncut_is_cheaper(self):
        r = make_row(base_nodes=100, own={2: True}, cost={2: 40}, value={2: 60})
        out = p6_explanatory.choose(r)
        self.assertEqual(out["Q1_CHEAPEST_ABSTAIN"], (40, 2))
        self.assertEqual(out["Q1_SAFE_ABSTAIN"], (40, 2))

    def test_safe_abstain_excludes_classification_flipping_owncut(self):
        # own-cut ordinal 1 with fail-low whole-node outcome (flip FH->FL);
        # ordinal 2 is classification-preserving but not an own-cut.
        r = make_row(base_nodes=100, base_fh=True, own={1: True},
                     cost={1: 30, 2: 90, 3: 90}, value={1: -10, 2: 42, 3: 42})
        out = p6_explanatory.choose(r)
        # CHEAP_UNC may take the flipping candidate; the safe rule may not.
        self.assertEqual(out["CHEAP_UNC"], (30, 1))
        self.assertEqual(out["Q1_CHEAPEST_ABSTAIN"], (30, 1))
        self.assertEqual(out["Q1_SAFE_ABSTAIN"], (100, 0))
        # ORACLE_CLS falls back to the next cheapest preserving candidate.
        self.assertEqual(out["ORACLE_CLS"], (90, 2))

    def test_root_table_aggregate_and_flip_accounting(self):
        rows = [
            # row1: own-cut ordinal 1 forced at 200 but fails low (flip).
            make_row(base_nodes=100, base_fh=True, own={1: True},
                     cost={1: 200, 2: 120, 3: 120}, value={1: -10}),
            # row2: own-cut ordinal 3 at 150, baseline fail-low (FL->FH).
            make_row(base_nodes=300, base_fh=False, own={3: True},
                     cost={1: 400, 2: 400, 3: 150}, value={3: 60}),
        ]
        rt = p6_explanatory.root_table(rows)
        self.assertEqual(rt["rows"], 2)
        self.assertEqual(rt["BASE"]["baseline_nodes"], 400)
        # Q1_CHEAPEST_FORCED promotes ordinal 1 (200) and ordinal 3 (150).
        self.assertEqual(rt["Q1_CHEAPEST_FORCED"]["policy_nodes"], 350)
        self.assertAlmostEqual(rt["Q1_CHEAPEST_FORCED"]["save_pct"], 12.5)
        self.assertEqual(rt["Q1_CHEAPEST_FORCED"]["later_share_pct"], 100.0)
        self.assertEqual(rt["Q1_CHEAPEST_FORCED"]["fh_to_fl_rows"], 1)
        self.assertEqual(rt["Q1_CHEAPEST_FORCED"]["fl_to_fh_rows"], 1)
        # ORACLE_CLS: row1 baseline (100) is the cheapest preserving choice
        # (ord 2/3 cost 120); row2 baseline (300) beats all preserving
        # candidates (400/400; the 150 flips). -> 400 total, no later picks.
        self.assertEqual(rt["ORACLE_CLS"]["policy_nodes"], 400)
        self.assertEqual(rt["ORACLE_CLS"]["later_share_pct"], 0.0)
        # Abstaining own-cut rules: row1 keeps baseline (own-cut costs 200);
        # row2 promotes ordinal 3 (150); safe row2 keeps baseline (flip).
        self.assertEqual(rt["Q1_CHEAPEST_ABSTAIN"]["policy_nodes"], 250)
        self.assertEqual(rt["Q1_SAFE_ABSTAIN"]["policy_nodes"], 400)

    def test_heuristics_can_lose_to_baseline(self):
        rows = [make_row(base_nodes=100, own={1: True}, cost={1: 300},
                         value={1: 60})]
        rt = p6_explanatory.root_table(rows)
        self.assertLess(rt["Q1_CHEAPEST_FORCED"]["save_pct"], 0)
        self.assertEqual(rt["Q1_CHEAPEST_ABSTAIN"]["save_pct"], 0.0)


class P5bTests(unittest.TestCase):
    def _write(self, rows):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "synthetic.jsonl.gz")
        with gzip.open(p, "wt") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_ordinal_table_ratios_and_flips(self):
        rows = [
            make_row(base_nodes=100, base_fh=True, own={1: True},
                     cost={1: 200, 2: 50, 3: 50}, value={1: -5, 2: 20, 3: 20}),
            make_row(base_nodes=300, base_fh=False, cost={1: 600, 2: 600,
                                                          3: 600},
                     value={1: 60}),
        ]
        t = p5b_analysis.ordinal_table(rows)
        # ordinal 1: sums base 400, forced 800 -> 2.0 aggregate
        self.assertEqual(t["sum_base"][1], 400)
        self.assertEqual(t["sum_forced"][1], 800)
        self.assertEqual(t["ffl"][1], 1)   # row1 FH->FL
        self.assertEqual(t["flfh"][1], 1)  # row2 FL->FH
        self.assertEqual(t["n"][1], 2)
        # ordinal 2: forced = 50 + 600 over 400 -> 1.625
        self.assertAlmostEqual(t["sum_forced"][2] / t["sum_base"][2], 1.625)

    def test_oracle_savings_sums(self):
        rows = [
            make_row(base_nodes=100, cost={1: 40, 2: 60, 3: 60},
                     value={1: 42, 2: 42, 3: 42}),
            make_row(base_nodes=300, base_fh=False, cost={1: 200, 2: 150,
                                                          3: 320},
                     value={1: -30, 2: -30, 3: 42}),
        ]
        # row1: ordinal 1 cheapest preserving (FH stays true) -> 40.
        # row2: baseline fail-low; ordinals 1/2 fail low at 200/150; the 320
        # flips (FH); cheapest preserving = 150.
        b, o, later, block = p5b_analysis.oracle_savings(rows)
        self.assertEqual(b, 400)
        self.assertEqual(o, 190)
        self.assertEqual(later, 2)

    def test_measurement_a_categorization(self):
        rows = [
            make_row(base_nodes=100, own={2: True}, cost={2: 40}, value={2: 60}),
            make_row(base_nodes=100, base_fh=False),
            make_row(base_nodes=100, base_fh=True),
        ]
        ma = p5b_analysis.measurement_a(rows)
        self.assertEqual(ma["nat_loop"], 2)   # rows 1 and 3 cut off in-loop
        self.assertEqual(ma["nat_ord0"], 2)   # both on the natural ordinal 0
        self.assertEqual(ma["fl"], 1)
        self.assertEqual(ma["own_cut"][2], 1)
        self.assertEqual(ma["n"], 3)

    def test_pooled_multi_argument_aggregation(self):
        # Pooling over two files must sum, not average.
        p1 = self._write([make_row(base_nodes=100, own={1: True},
                                   cost={1: 200, 2: 120, 3: 120}, value={1: 60})])
        p2 = self._write([make_row(base_nodes=300, own={1: True},
                                   cost={1: 450, 2: 300, 3: 300}, value={1: 60})])
        files = p5b_analysis.load(p1) + p5b_analysis.load(p2)
        t = p5b_analysis.ordinal_table(files)
        self.assertEqual(t["sum_base"][1], 400)
        self.assertEqual(t["sum_forced"][1], 650)


if __name__ == "__main__":
    unittest.main()


class P6LearnabilitySmoke(unittest.TestCase):
    """Smoke test: the LOO learnability probe runs end-to-end on synthetic
    roots and reproduces exact-cost accounting for the BASE policy."""

    def test_loo_smoke_and_base_accounting(self):
        import subprocess
        from tests.test_p6_analysis_tools import make_row
        d = tempfile.mkdtemp()
        paths = []
        # three tiny "roots" with distinguishable patterns; seed fixed so
        # per-root behavior differs via cost values
        for root in range(3):
            rows = []
            for i in range(12):
                base = 100 + root * 50 + i
                own = {1: (i % 3) == 0}
                r = make_row(base_nodes=base, own=own,
                             cost={o: base * (1 + (o + root) * 0.2)
                                   for o in (1, 2, 3)},
                             value={1: 60})
                r["root_key"] = root
                r["sample_id"] = i + 1
                rows.append(r)
            p = os.path.join(d, f"syn_root{root}.jsonl.gz")
            with gzip.open(p, "wt") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            paths.append(p)
        out = os.path.join(d, "out.json")
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "p6_learnability.py"),
             *paths, "--json", out],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        with open(out) as fh:
            rep = json.load(fh)
        self.assertEqual(set(rep["pooled"]),
                         {"BASE", "Q", "CHEAP", "CHEAP_SAFE", "QE",
                          "ORACLE_CLS"})
        self.assertEqual(rep["pooled"]["BASE"]["policy_nodes"],
                         rep["pooled"]["BASE"]["baseline_nodes"])
        self.assertGreater(rep["pooled"]["BASE"]["baseline_nodes"], 0)
        self.assertEqual(rep["pooled"]["BASE"]["fhfl"], 0)
