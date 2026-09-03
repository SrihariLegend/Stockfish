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
import decode_research_log as dlog


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


# ---------------------------------------------------------------------------
# Phase 2: research log decoder/validator
# ---------------------------------------------------------------------------

MAGIC = b"PXRLOG\x01\x00"
FEN0 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
ROOT_KEY = 0x1122334455667788


def _s(b: bytes, x: str) -> bytes:
    raw = x.encode()
    return b + len(raw).to_bytes(2, "little") + raw


def _b_run_start() -> bytes:
    p = b""
    p += bytes([1])  # mode observational
    p += (7).to_bytes(8, "little")  # seed
    p += (250000).to_bytes(4, "little")  # threshold
    p += (100).to_bytes(4, "little")  # cap
    p = _s(p, "policy-v1")
    p = _s(p, "Stockfish test")
    return p


def _b_root_start(fen: str = FEN0) -> bytes:
    p = ROOT_KEY.to_bytes(8, "little")
    p += (6).to_bytes(4, "little")
    return _s(p, fen)


def _b_decision(serial: int, fen: str = FEN0, key: int = 0xABCD) -> bytes:
    p = ROOT_KEY.to_bytes(8, "little")
    p += serial.to_bytes(8, "little")
    p += key.to_bytes(8, "little")
    for v in (0, 6, 6, -50, 50, 10):  # ply, depth, rootIter, alpha, beta, staticEval
        p += (v & 0xFFFFFFFF).to_bytes(4, "little")
    p += bytes([0b001])  # flags: improving
    p += (2).to_bytes(2, "little")  # rule50
    p += bytes([0])  # stm white
    return _s(p, fen)


def _b_attempt(serial: int, qord: int = 1, outcome: int = 2, nodes: int = 3) -> bytes:
    p = ROOT_KEY.to_bytes(8, "little")
    p += serial.to_bytes(8, "little")
    p += (1).to_bytes(8, "little")  # attempt serial
    p += (12 << 6 | 28).to_bytes(2, "little")  # e2e4: NORMAL from e2(12) to e4(28)
    p += qord.to_bytes(2, "little")
    p += (5).to_bytes(2, "little")  # totalAttempted
    p += bytes([0, 0, 1])  # givesCheck, isTT, childCount
    for v in (4, -1, -50, 50, 12):  # fd, rsd, alpha, beta, value
        p += (v & 0xFFFFFFFF).to_bytes(4, "little")
    p += nodes.to_bytes(8, "little")
    p += bytes([outcome])
    return p


def _b_root_end(dec: int, att: int) -> bytes:
    return (ROOT_KEY.to_bytes(8, "little") + dec.to_bytes(8, "little")
            + att.to_bytes(8, "little"))


def _b_run_end(dec: int, att: int) -> bytes:
    return (dec.to_bytes(8, "little") + att.to_bytes(8, "little")
            + bytes([0, 0]))


def _frame(rtype: int, payload: bytes) -> bytes:
    return (rtype.to_bytes(2, "little") + (1).to_bytes(2, "little")
            + len(payload).to_bytes(4, "little") + payload)


def _log_bytes() -> bytes:
    out = MAGIC + (0x01020304).to_bytes(4, "little") + (1).to_bytes(2, "little")
    out += (0).to_bytes(4, "little")  # uuid len 0
    out += _frame(dlog.RUN_START, _b_run_start())
    out += _frame(dlog.ROOT_START, _b_root_start())
    out += _frame(dlog.DECISION_POINT, _b_decision(1))
    out += _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
    out += _frame(dlog.ROOT_END, _b_root_end(1, 1))
    out += _frame(dlog.RUN_END, _b_run_end(1, 1))
    return out


class TestDecodeResearchLog(unittest.TestCase):
    def _decode(self, data: bytes):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            header, records = dlog.decode_file(path)
            stats = dlog.validate(records, path)
            return header, records, stats

    def test_valid_log_decodes_and_validates(self):
        header, records, stats = self._decode(_log_bytes())
        self.assertEqual(header["format_version"], 1)
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]["_type"], dlog.RUN_START)
        self.assertEqual(stats["run_decisions"], 1)
        self.assertEqual(stats["run_attempts"], 1)
        self.assertEqual(stats["outcomes"], {"fail_low": 1})
        self.assertEqual(records[3]["move_uci"], "e2e4")

    def test_payload_equality_across_runs_and_difference_detection(self):
        a = _log_bytes()
        with tempfile.TemporaryDirectory() as d:
            pa = Path(d) / "a.bin"
            pb = Path(d) / "b.bin"
            pa.write_bytes(a)
            pb.write_bytes(a)
            res = dlog.compare(pa, pb)
            self.assertTrue(res["equal"])
            b2 = a.replace(_b_attempt(1, outcome=2), _b_attempt(1, outcome=1))
            self.assertNotEqual(a, b2)
            p2 = Path(d) / "c.bin"
            p2.write_bytes(b2)
            res2 = dlog.compare(pa, p2)
            self.assertFalse(res2["equal"])

    def test_bad_magic_rejected(self):
        data = bytearray(_log_bytes())
        data[0] = 0
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(bytes(data))
            with self.assertRaises(dlog.ValidationError):
                dlog.decode_file(path)

    def test_unknown_record_type_rejected(self):
        data = _log_bytes().replace(
            _frame(dlog.RUN_START, _b_run_start()),
            _frame(99, b""),
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                dlog.decode_file(path)

    def test_unterminated_root_rejected(self):
        data = _log_bytes().replace(_frame(dlog.ROOT_END, _b_root_end(1, 1)), b"")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                _, records = dlog.decode_file(path)
                dlog.validate(records, path)

    def test_attempt_without_decision_rejected(self):
        # root with an attempt that has no matching DECISION_POINT
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
                + _frame(dlog.ROOT_END, _b_root_end(0, 1))
                + _frame(dlog.RUN_END, _b_run_end(0, 1)))
        data = (MAGIC + (0x01020304).to_bytes(4, "little")
                + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + body)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                _, records = dlog.decode_file(path)
                dlog.validate(records, path)

    def test_reserved_counterfactual_type_rejected(self):
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.COUNTERFACTUAL_RESULT, b"")
                + _frame(dlog.ROOT_END, _b_root_end(0, 0))
                + _frame(dlog.RUN_END, _b_run_end(0, 0)))
        data = (MAGIC + (0x01020304).to_bytes(4, "little")
                + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + body)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                dlog.decode_file(path)

    def test_run_end_totals_mismatch_rejected(self):
        data = _log_bytes().replace(_frame(dlog.RUN_END, _b_run_end(1, 1)),
                                    _frame(dlog.RUN_END, _b_run_end(9, 9)))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                _, records = dlog.decode_file(path)
                dlog.validate(records, path)


if __name__ == "__main__":
    unittest.main()
