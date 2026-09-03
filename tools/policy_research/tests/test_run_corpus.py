#!/usr/bin/env python3
"""Unit tests for the Phase 1 corpus runner (no engine required).

Integration tests that need a Stockfish engine are skipped unless the
STOCKFISH_ENGINE environment variable points at an executable.

Run with:  python3 -m unittest discover -s tools/policy_research/tests -v
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_corpus as rc


MINIMAL_POSITION = {
    "id": "x-001",
    "set": "development",
    "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "source": "test",
    "license": "public domain (position facts)",
}


def write_corpus(tmp: Path, positions) -> Path:
    data = {
        "schema": rc.SCHEMA_CORPUS,
        "corpus_id": "unit-test-corpus",
        "positions": positions,
    }
    path = tmp / "corpus.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestCorpus(unittest.TestCase):
    def test_valid_corpus_loads_and_hashes_stably(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_corpus(Path(d), [MINIMAL_POSITION])
            corpus, canonical = rc.load_corpus(path)
            self.assertEqual(corpus["corpus_id"], "unit-test-corpus")
            # canonical bytes are key-sorted: identical even if source order differs
            again, canonical2 = rc.load_corpus(path)
            self.assertEqual(canonical, canonical2)
            self.assertEqual(len(rc.corpus_sha256(canonical)), 64)

    def test_rejects_bad_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text(json.dumps({"schema": "corpus/999", "corpus_id": "x", "positions": []}))
            with self.assertRaises(ValueError):
                rc.load_corpus(path)

    def test_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_corpus(Path(d), [MINIMAL_POSITION, dict(MINIMAL_POSITION, fen=MINIMAL_POSITION["fen"])])
            with self.assertRaises(ValueError) as ctx:
                rc.load_corpus(path)
            self.assertIn("duplicate position id", str(ctx.exception))

    def test_rejects_unknown_set(self):
        with tempfile.TemporaryDirectory() as d:
            bad = dict(MINIMAL_POSITION, set="training")
            path = write_corpus(Path(d), [bad])
            with self.assertRaises(ValueError) as ctx:
                rc.load_corpus(path)
            self.assertIn("unknown set", str(ctx.exception))

    def test_rejects_malformed_fen(self):
        with tempfile.TemporaryDirectory() as d:
            bad = dict(MINIMAL_POSITION, fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq")
            path = write_corpus(Path(d), [bad])
            with self.assertRaises(ValueError) as ctx:
                rc.load_corpus(path)
            self.assertIn("must have 6 fields", str(ctx.exception))


class TestNormalize(unittest.TestCase):
    def test_strips_time_and_nps_keeps_nodes(self):
        row = (
            "info depth 11 seldepth 15 multipv 1 score cp 26 nodes 12455 nps 800000 "
            "hashfull 4 tbhits 0 time 15 pv e2e4 c7c5"
        )
        norm = rc.normalize_info_row(row)
        self.assertNotIn("time", norm)
        self.assertNotIn("nps", norm)
        self.assertIn("nodes 12455", norm)
        self.assertIn("score cp 26", norm)
        self.assertIn("pv e2e4 c7c5", norm)

    def test_summary_parse(self):
        row = (
            "info depth 11 seldepth 15 multipv 1 score cp 26 nodes 12455 nps 800000 "
            "hashfull 4 tbhits 0 time 15 pv e2e4 c7c5"
        )
        summary = rc.summarize_info_row(row)
        self.assertEqual(summary["score"], "cp 26")
        self.assertEqual(summary["nodes"], 12455)
        self.assertEqual(summary["pv"], "e2e4 c7c5")
        self.assertIsNone(summary["bound"])

    def test_summary_parse_lowerbound(self):
        row = "info depth 12 score cp 40 lowerbound nodes 500 pv e2e4"
        summary = rc.summarize_info_row(row)
        self.assertEqual(summary["bound"], "lowerbound")


class TestCompare(unittest.TestCase):
    def _result(self, pos, bestmove, rows):
        return {"positions": [{"id": pos, "bestmove": bestmove, "rows": rows, "summary": {}}]}

    def test_identical_passes(self):
        a = self._result("p1", "e2e4 ponder c7c5", ["info depth 5 nodes 100 pv e2e4"])
        b = self._result("p1", "e2e4 ponder c7c5", ["info depth 5 nodes 100 pv e2e4"])
        ok, issues = rc.compare_results(a, b)
        self.assertTrue(ok)
        self.assertTrue(issues[0]["ok"])

    def test_bestmove_mismatch_fails(self):
        a = self._result("p1", "e2e4", ["info depth 5 nodes 100 pv e2e4"])
        b = self._result("p1", "d2d4", ["info depth 5 nodes 100 pv e2e4"])
        ok, issues = rc.compare_results(a, b)
        self.assertFalse(ok)
        self.assertIn("bestmove", issues[0]["differences"][0])

    def test_rows_mismatch_fails(self):
        a = self._result("p1", "e2e4", ["info depth 5 nodes 100 pv e2e4"])
        b = self._result("p1", "e2e4", ["info depth 5 nodes 99 pv e2e4"])
        ok, issues = rc.compare_results(a, b)
        self.assertFalse(ok)
        self.assertTrue(any("info rows differ" in d for d in issues[0]["differences"]))

    def test_missing_position_fails(self):
        a = {"positions": [{"id": "p1", "bestmove": "e2e4", "rows": []}]}
        b = {"positions": []}
        ok, issues = rc.compare_results(a, b)
        self.assertFalse(ok)
        self.assertEqual(issues[0]["differences"], ["missing in run B"])

    def test_extra_position_in_run_b_fails(self):
        a = {"positions": [{"id": "p1", "bestmove": "e2e4", "rows": []}]}
        b = {
            "positions": [
                {"id": "p1", "bestmove": "e2e4", "rows": []},
                {"id": "p2", "bestmove": "d2d4", "rows": []},
            ]
        }
        ok, issues = rc.compare_results(a, b)
        self.assertFalse(ok)
        self.assertTrue(any("extra position in run B" in d for x in issues for d in x["differences"]))


class TestCompletion(unittest.TestCase):
    FULL_ROW = (
        "info depth 6 seldepth 8 multipv 1 score cp 40 nodes 500 hashfull 1 "
        "tbhits 0 pv e2e4 e7e5 g1f3"
    )

    def test_complete_search_passes(self):
        self.assertEqual(rc.validate_completion([self.FULL_ROW], 6), [])

    def test_complete_search_with_higher_final_depth_passes(self):
        deeper = self.FULL_ROW.replace("depth 6", "depth 7")
        self.assertEqual(rc.validate_completion([deeper], 6), [])

    def test_mate_announcement_passes_without_full_depth(self):
        row = "info depth 3 seldepth 6 multipv 1 score mate 2 nodes 42 hashfull 1 tbhits 0 pv g1f3 e7e5 f3g5"
        self.assertEqual(rc.validate_completion([row], 11), [])

    def test_aborted_search_fails(self):
        # The buffered-quit bug produced exactly this shape: depth 1, zero nodes,
        # empty PV, then a fallback bestmove.
        aborted = "info depth 1 seldepth 0 multipv 1 score cp 0 nodes 0 hashfull 0 tbhits 0 pv "
        problems = rc.validate_completion([aborted], 11)
        self.assertTrue(problems)
        joined = " ".join(problems)
        self.assertIn("no positive node count", joined)
        self.assertIn("no info row carries a non-empty PV", joined)
        self.assertIn("final row depth 1 < requested 11", joined)

    def test_no_info_rows_fails(self):
        self.assertTrue(rc.validate_completion([], 6))


class TestEvalResolution(unittest.TestCase):
    def test_missing_explicit_eval_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                rc._resolve_eval_file(Path("/nonexistent-engine"), Path(d), "/nonexistent-net.nnue")


class TestIntegration(unittest.TestCase):
    ENGINE = os.environ.get("STOCKFISH_ENGINE")

    @unittest.skipUnless(ENGINE, "STOCKFISH_ENGINE not set")
    def test_deterministic_double_run(self):
        from run_corpus import (
            execute_run,
            compare_results,
            load_corpus,
            corpus_sha256,
        )

        class Args:
            only = {"c1-d-001"}
            depth = 6
            hash = 8
            timeout = 120.0
            eval_file = None

        cwd = rc.repo_root()
        engine = Path(self.ENGINE).expanduser().resolve()
        corpus_path = cwd / "tools" / "policy_research" / "corpora" / "corpus-v1.json"
        corpus, canonical = load_corpus(corpus_path)
        sha = corpus_sha256(canonical)
        args = Args()
        args.corpus = corpus_path
        args.eval_file = rc._resolve_eval_file(engine, cwd, None)
        args.build_command = "make build ARCH=x86-64-avx2 (integration test)"
        self.assertIsNotNone(args.eval_file)
        run_a = execute_run(engine, cwd, corpus, sha, args)
        run_b = execute_run(engine, cwd, corpus, sha, args)
        ok, issues = compare_results(run_a, run_b)
        self.assertTrue(ok, msg=str(issues))
        # Phase 1 hardening: each root must have completed depth-6 search.
        for run in (run_a, run_b):
            pos = run["positions"][0]
            self.assertTrue(pos["summary"]["pv"], "root search produced no PV")
            self.assertGreaterEqual(
                int(re.search(r"depth (\d+)", pos["rows"][-1]).group(1)), args.depth
            )
            self.assertGreater(pos["summary"]["nodes"], 0)
            self.assertIsNotNone(pos["summary"]["score"])


if __name__ == "__main__":
    unittest.main()
