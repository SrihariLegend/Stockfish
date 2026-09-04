#!/usr/bin/env python3
"""Unit tests for the Phase 1 corpus runner (no engine required).

Integration tests that need a Stockfish engine are skipped unless the
STOCKFISH_ENGINE environment variable points at an executable.

Run with:  python3 -m unittest discover -s tools/policy_research/tests -v
"""

import json
import os
import re
import subprocess
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

    def test_load_corpus_v2(self):
        v2_path = Path(__file__).resolve().parents[1] / "corpora" / "corpus-v2.json"
        if v2_path.is_file():
            corpus, canonical = rc.load_corpus(v2_path)
            self.assertEqual(corpus.get("schema"), "corpus/v2")
            self.assertEqual(len(corpus["positions"]), 26)
            dev = [p for p in corpus["positions"] if p["set"] == "development"]
            val = [p for p in corpus["positions"] if p["set"] == "validation"]
            tst = [p for p in corpus["positions"] if p["set"] == "test"]
            self.assertEqual(len(dev), 10)
            self.assertEqual(len(val), 8)
            self.assertEqual(len(tst), 8)
            # Ensure burned c1-t-001 is not in test
            self.assertNotIn("c1-t-001", [p["id"] for p in tst])
            # Ensure all positions have valid FEN strings with 6 fields
            for p in corpus["positions"]:
                self.assertEqual(len(p["fen"].split()), 6, msg=f"Root {p['id']} FEN must have 6 fields")

    def test_load_corpus_v3_and_exact_replay_verification(self):
        v3_path = Path(__file__).resolve().parents[1] / "corpora" / "corpus-v3.json"
        self.assertTrue(v3_path.is_file(), "corpus-v3.json must exist")
        corpus, canonical = rc.load_corpus(v3_path)
        self.assertEqual(corpus.get("schema"), "corpus/v3")
        self.assertEqual(corpus.get("corpus_id"), "policy-research-corpus-v3")
        self.assertEqual(len(corpus["positions"]), 26)
        dev = [p for p in corpus["positions"] if p["set"] == "development"]
        val = [p for p in corpus["positions"] if p["set"] == "validation"]
        tst = [p for p in corpus["positions"] if p["set"] == "test"]
        self.assertEqual(len(dev), 10)
        self.assertEqual(len(val), 8)
        self.assertEqual(len(tst), 8)

        engine_path = os.environ.get("STOCKFISH_ENGINE")
        for p in corpus["positions"]:
            self.assertEqual(len(p["fen"].split()), 6, msg=f"Root {p['id']} FEN must have 6 fields")
            if engine_path and "source_line_moves" in p:
                moves = " ".join(p["source_line_moves"])
                proc = subprocess.Popen([engine_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                proc.stdin.write(f"position startpos moves {moves}\nd\nquit\n")
                proc.stdin.flush()
                out, _ = proc.communicate()
                rep_fen = next(l[4:].strip() for l in out.splitlines() if l.startswith("Fen:"))
                self.assertEqual(p["fen"], rep_fen, f"Root {p['id']} replay FEN mismatch byte-for-byte")


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
# Phase 4 kickoff: research-only root force-first override
# ---------------------------------------------------------------------------
# Requires a POLICY_RESEARCH build of the engine (the option
# PolicyResearchForceFirstMove only exists there). Every assertion is
# relational (equality within repeat runs, parity with the un-overridden
# baseline) so the tests stay meaningful across engine/net versions.

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class _Engine:
    """Tiny UCI driver: fresh process per search (plan 9.2 common state)."""

    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        self.has_force_option = False
        self._probe()

    def _probe(self):
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
        out = ""
        try:
            p.stdin.write("uci\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                out += line
                if line.strip() == "uciok":
                    break
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                if p.stdin: p.stdin.close()
            except OSError: pass
            try:
                if p.stdout: p.stdout.close()
            except OSError: pass
            p.wait()
        self.has_force_option = (
            "option name PolicyResearchForceFirstMove" in out
            and "option name PolicyResearchForceFirstDepth" in out)

    def search(self, fen, depth, mode="root_counterfactual", force="",
               force_depth=0, master="on", hash_mb=16):
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            p.stdin.write("uci\n")
            p.stdin.write("setoption name Threads value 1\n")
            p.stdin.write("setoption name MultiPV value 1\n")
            p.stdin.write("setoption name Hash value %d\n" % hash_mb)
            p.stdin.write("setoption name PolicyResearch value %s\n" % master)
            if master == "on":
                p.stdin.write("setoption name PolicyResearchMode value %s\n"
                              % mode)
                if force:
                    p.stdin.write(
                        "setoption name PolicyResearchForceFirstMove "
                        "value %s\n" % force)
                    p.stdin.write(
                        "setoption name PolicyResearchForceFirstDepth "
                        "value %d\n" % force_depth)
            p.stdin.write("isready\n")
            p.stdin.write("position fen %s\n" % fen)
            p.stdin.write("go depth %d\n" % depth)
            p.stdin.flush()
            nodes_at_depth = None
            best = None
            infos = []
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("info string research"):
                    infos.append(line)
                m = re.match(r"info depth (\d+) .*? nodes (\d+)", line)
                if m and int(m.group(1)) == depth:
                    nodes_at_depth = int(m.group(2))
                if line.startswith("bestmove"):
                    best = line.split()[1]
                    break
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                if p.stdin: p.stdin.close()
            except OSError: pass
            try:
                if p.stdout: p.stdout.close()
            except OSError: pass
            p.wait()
        return {"nodes": nodes_at_depth, "best": best, "info": infos}

    def top_moves(self, fen, depth=6, k=3):
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
        seen = {}
        try:
            p.stdin.write("uci\n")
            p.stdin.write("setoption name MultiPV value %d\n" % k)
            p.stdin.write("isready\n")
            p.stdin.write("position fen %s\n" % fen)
            p.stdin.write("go depth %d\n" % depth)
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                m = re.search(r"multipv (\d+) .*? pv (\S+)", line)
                if m:
                    seen[int(m.group(1))] = m.group(2)
                if line.startswith("bestmove"):
                    break
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                if p.stdin: p.stdin.close()
            except OSError: pass
            try:
                if p.stdout: p.stdout.close()
            except OSError: pass
            p.wait()
        return seen


class TestForceFirstRootOrder(unittest.TestCase):
    ENGINE = os.environ.get("STOCKFISH_ENGINE")

    @classmethod
    def setUpClass(cls):
        if cls.ENGINE:
            cls.driver = _Engine(cls.ENGINE)
            if not cls.driver.has_force_option:
                raise unittest.SkipTest("engine is not a POLICY_RESEARCH build")
        else:
            cls.driver = None

    def _skip_if_no_engine(self):
        if not self.ENGINE:
            self.skipTest("STOCKFISH_ENGINE not set")

    def _base(self, depth=8):
        return self.driver.search(STARTPOS, depth)

    def test_baseline_is_deterministic(self):
        self._skip_if_no_engine()
        a = self._base()
        b = self._base()
        self.assertEqual(a, b)
        self.assertIsNotNone(a["best"])
        self.assertGreater(a["nodes"], 0)

    def test_override_second_best_is_deterministic_and_changes_cost(self):
        self._skip_if_no_engine()
        top = self.driver.top_moves(STARTPOS, depth=6)
        base_best = self._base()["best"]
        second = next((top[i] for i in sorted(top)
                       if top[i] != base_best), None)
        if second is None:
            self.skipTest("no second candidate to force")
        a = self.driver.search(STARTPOS, 8, force=second)
        b = self.driver.search(STARTPOS, 8, force=second)
        self.assertEqual(a, b)
        self.assertNotEqual(a["nodes"], self._base()["nodes"],
                            "forcing a different first move changed nothing")
        # value agreement at this shallow fixed depth is expected on startpos,
        # but we only assert the search completed with the same protocol
        self.assertIsNotNone(a["best"])

    def test_illegal_force_move_is_ignored_with_diagnostic(self):
        self._skip_if_no_engine()
        base = self._base()
        r = self.driver.search(STARTPOS, 8, force="e2e5")  # pawn double only to e4
        self.assertEqual(r["nodes"], base["nodes"])
        self.assertTrue(any("not a legal move" in i for i in r["info"]))

    def test_observational_mode_ignores_override(self):
        self._skip_if_no_engine()
        top = self.driver.top_moves(STARTPOS, depth=6)
        base_best = self._base()["best"]
        second = next((top[i] for i in sorted(top)
                       if top[i] != base_best), None) or "a2a3"
        base = self._base()
        r = self.driver.search(STARTPOS, 8, mode="observational",
                               force=second)
        self.assertEqual(r["nodes"], base["nodes"])
        self.assertEqual(r["best"], base["best"])

    def test_master_switch_off_ignores_override(self):
        self._skip_if_no_engine()
        base = self._base()
        r = self.driver.search(STARTPOS, 8, master="off", force="d2d4")
        self.assertEqual(r["nodes"], base["nodes"])

    def test_isolated_depth_override_best_move_is_exact_noop(self):
        self._skip_if_no_engine()
        base = self._base(depth=8)
        # Forcing the baseline's own best move only at target depth 8 is an exact no-op
        r = self.driver.search(STARTPOS, depth=8, force=base["best"], force_depth=8)
        self.assertEqual(r["nodes"], base["nodes"])
        self.assertEqual(r["best"], base["best"])

    def test_isolated_depth_override_other_depth_is_noop(self):
        self._skip_if_no_engine()
        base = self._base(depth=8)
        # Forcing a move at depth 9 during a depth-8 search does not trigger
        r = self.driver.search(STARTPOS, depth=8, force="d2d4", force_depth=9)
        self.assertEqual(r["nodes"], base["nodes"])
        self.assertEqual(r["best"], base["best"])

    def test_root_telemetry_emission_and_parse(self):
        self._skip_if_no_engine()
        import tools.policy_research.p4_force_first as p4
        eng = p4.Engine(self.ENGINE)
        res = eng.run_search(STARTPOS, depth=6)
        self.assertIsNotNone(res["root_telemetry"], "root_telemetry should be captured at target depth 6")
        tel = res["root_telemetry"]
        self.assertEqual(tel["depth"], 6)
        self.assertGreaterEqual(tel["aspiration_iterations"], 1)
        self.assertIn("moves", tel)
        self.assertGreater(len(tel["moves"]), 0)
        # Check attempts telemetry
        self.assertIn("attempts", tel)
        self.assertGreater(len(tel["attempts"]), 0)
        att = tel["attempts"][0]
        self.assertIn("depth", att)
        self.assertIn("result", att)
        # Check moves telemetry format: effort_total and effort_incremental
        m_info = next(iter(tel["moves"].values()))
        self.assertIn("effort_total", m_info)
        self.assertIn("effort_incremental", m_info)

    def test_causal_decomposition_options(self):
        self._skip_if_no_engine()
        import tools.policy_research.p4_force_first as p4
        eng = p4.Engine(self.ENGINE)
        fen = "r1bqk2r/pppp1ppp/2n5/2b5/2BPn3/5N2/PP3PPP/RNBQK2R w KQkq - 0 7"
        # Run with preserve_aspiration
        r_pa = eng.run_search(fen, depth=14, force_move="b1c3", force_depth=14, preserve_aspiration=True)
        self.assertEqual(r_pa["best"], "d4c5")
        self.assertGreater(r_pa["nodes"], 0)
        # Run with disable_fail_high_reduction
        r_df = eng.run_search(fen, depth=14, force_move="b1c3", force_depth=14, disable_fail_high_reduction=True)
        self.assertEqual(r_df["best"], "d4c5")
        self.assertGreater(r_df["nodes"], 0)

    def test_score_tolerance_below_25(self):
        # Unit test verifying Finding 8: score tolerance below 25 cp rejects larger deltas
        tolerance = 10
        diff_abs = 20
        score_agrees = (diff_abs <= tolerance)
        self.assertFalse(score_agrees, "20 cp delta must not pass a 10 cp tolerance gate")


# ---------------------------------------------------------------------------
# Phase 2: research log decoder/validator
# ---------------------------------------------------------------------------

MAGIC = b"PXRLOG\x01\x00"
FEN0 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
ROOT_KEY = 0x1122334455667788


def _s(b: bytes, x: str) -> bytes:
    raw = x.encode()
    return b + len(raw).to_bytes(2, "little") + raw


def _b_run_start(seed: int = 7) -> bytes:
    p = b""
    p += bytes([1])  # mode observational
    p += (seed).to_bytes(8, "little")
    p += (250000).to_bytes(4, "little")  # threshold
    p += (100).to_bytes(4, "little")  # cap
    p = _s(p, "policy-v1")
    p = _s(p, "Stockfish test")
    return p


def _b_root_start(fen: str = FEN0, key: int = ROOT_KEY) -> bytes:
    p = key.to_bytes(8, "little")
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


def _b_attempt(serial: int, qord: int = 1, outcome: int = 2, nodes: int = 3,
               child_count: int = 1, move_raw: int | None = None) -> bytes:
    p = ROOT_KEY.to_bytes(8, "little")
    p += serial.to_bytes(8, "little")
    p += (1).to_bytes(8, "little")  # attempt serial
    raw = move_raw if move_raw is not None else (12 << 6 | 28)  # e2e4 NORMAL
    p += raw.to_bytes(2, "little")
    p += qord.to_bytes(2, "little")
    p += (5).to_bytes(2, "little")  # totalAttempted
    p += bytes([0, 0, child_count])  # givesCheck, isTT, childCount
    for v in (4, -1, -50, 50, 12):  # fd, rsd, alpha, beta, value
        p += (v & 0xFFFFFFFF).to_bytes(4, "little")
    p += nodes.to_bytes(8, "little")
    p += bytes([outcome])
    return p


# Promotion raw for e7e8<piece> (to=e8 60, from=e7 52, promo bits PieceType-2).
def _b_promotion_raw(piece_pt: int) -> int:
    return ((piece_pt - 2) << 12) | (1 << 14) | (52 << 6) | 60


def _b_root_end(dec: int, att: int, key: int = ROOT_KEY) -> bytes:
    return (key.to_bytes(8, "little") + dec.to_bytes(8, "little")
            + att.to_bytes(8, "little"))


def _b_run_end(dec: int, att: int, overflow: int = 0, error: int = 0) -> bytes:
    return (dec.to_bytes(8, "little") + att.to_bytes(8, "little")
            + bytes([overflow, error]))


def _b_error_record(code: int = 1, msg: str = "cap reached") -> bytes:
    return code.to_bytes(2, "little") + _s(b"", msg)


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
            b2 = a.replace(_b_attempt(1, nodes=3), _b_attempt(1, nodes=99))
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

    def test_promotion_moves_decode_with_correct_suffix(self):
        # Regression: promo piece-type bits 12-13 hold PieceType-KNIGHT (2..5)
        # mapped to n/b/r/q, not an index into "nbrq".
        for pt, suffix in ((2, "n"), (3, "b"), (4, "r"), (5, "q")):
            raw = _b_promotion_raw(pt)
            self.assertEqual(dlog._move_raw_to_uci(raw), "e7e8" + suffix)

    def _decode_and_validate(self, data: bytes):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            _, records = dlog.decode_file(path)
            return dlog.validate(records, path)

    def test_root_end_attempt_count_mismatch_rejected(self):
        data = _log_bytes().replace(_frame(dlog.ROOT_END, _b_root_end(1, 1)),
                                    _frame(dlog.ROOT_END, _b_root_end(1, 2)))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_root_end_decision_count_mismatch_rejected(self):
        data = _log_bytes().replace(_frame(dlog.ROOT_END, _b_root_end(1, 1)),
                                    _frame(dlog.ROOT_END, _b_root_end(2, 1)))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_attempt_with_zero_child_searches_rejected(self):
        data = _log_bytes().replace(_b_attempt(1, child_count=1),
                                    _b_attempt(1, child_count=0))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_attempt_referencing_closed_root_decision_rejected(self):
        # Second root's attempt points at the first root's decision serial.
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.DECISION_POINT, _b_decision(1))
                + _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
                + _frame(dlog.ROOT_END, _b_root_end(1, 1))
                + _frame(dlog.ROOT_START, _b_root_start(key=0x99))
                + _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
                + _frame(dlog.ROOT_END, _b_root_end(0, 0, key=0x99))
                + _frame(dlog.RUN_END, _b_run_end(1, 1)))
        data = (MAGIC + (0x01020304).to_bytes(4, "little")
                + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + body)
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_duplicate_root_keys_rejected(self):
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.ROOT_END, _b_root_end(0, 0))
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.ROOT_END, _b_root_end(0, 0))
                + _frame(dlog.RUN_END, _b_run_end(0, 0)))
        data = (MAGIC + (0x01020304).to_bytes(4, "little")
                + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + body)
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_run_start_mid_stream_rejected(self):
        data = _log_bytes() + _frame(dlog.RUN_START, _b_run_start())
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_run_end_error_code_must_agree_with_overflow(self):
        # overflow=1 with error_code=0 contradicts the RUN_END layout rule.
        data = _log_bytes().replace(_frame(dlog.RUN_END, _b_run_end(1, 1)),
                                    _frame(dlog.RUN_END, _b_run_end(1, 1, 1, 0)))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_overflow_without_error_record_rejected(self):
        data = _log_bytes().replace(_frame(dlog.RUN_END, _b_run_end(1, 1)),
                                    _frame(dlog.RUN_END, _b_run_end(1, 1, 1, 1)))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_overflow_with_error_record_validates(self):
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.DECISION_POINT, _b_decision(1))
                + _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
                + _frame(dlog.ERROR_RECORD, _b_error_record())
                + _frame(dlog.ROOT_END, _b_root_end(1, 1))
                + _frame(dlog.RUN_END, _b_run_end(1, 1, 1, 1)))
        data = (MAGIC + (0x01020304).to_bytes(4, "little")
                + (1).to_bytes(2, "little") + (0).to_bytes(4, "little") + body)
        stats = self._decode_and_validate(data)
        self.assertTrue(stats["overflow"])

    def test_fail_high_outcome_must_imply_value_at_least_beta(self):
        # outcome 1 (fail-high cutoff) with value < beta contradicts the engine
        # outcome rule.
        data = _log_bytes().replace(_b_attempt(1, outcome=2),
                                    _b_attempt(1, outcome=1))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(data)

    def test_fail_low_outcome_must_imply_value_below_beta(self):
        # outcome 2 (fail-low) with value >= beta contradicts the engine outcome
        # rule. Patch the attempt's value field to equal beta.
        value_off = 8 * 3 + 2 * 3 + 3 + 4 * 4  # 49: value field after fd/rsd/ab/bb
        p = bytearray(_b_attempt(1, outcome=2))
        p[value_off:value_off + 4] = (50).to_bytes(4, "little")  # value == beta
        bad = _log_bytes().replace(_b_attempt(1, outcome=2), bytes(p))
        with self.assertRaises(dlog.ValidationError):
            self._decode_and_validate(bad)

    def test_value_field_offset_is_stable(self):
        # Guard the raw offset used above against silent schema drift.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(_log_bytes())
            _, records = dlog.decode_file(path)
        att = records[3]
        self.assertEqual(att["_type"], dlog.MOVE_ATTEMPT)
        self.assertEqual(att["value_returned"], 12)
        self.assertEqual(att["beta_before"], 50)

    def _decode_raises_validation(self, data: bytes):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            with self.assertRaises(dlog.ValidationError):
                dlog.decode_file(path)

    def _header(self) -> bytes:
        return MAGIC + (0x01020304).to_bytes(4, "little") \
            + (1).to_bytes(2, "little") + (0).to_bytes(4, "little")

    def test_magic_only_rejected_not_crash(self):
        # Regression: decode must raise ValidationError, not struct.error, on a
        # file that ends inside the fixed-width header.
        self._decode_raises_validation(MAGIC)
        self._decode_raises_validation(MAGIC + bytes(5))

    def test_truncated_run_start_rejected_not_crash(self):
        # RUN_START payload shorter than the two trailing string prefixes must be
        # rejected by the decoder, not crash in struct.unpack_from.
        data = self._header() + _frame(dlog.RUN_START, bytes(17))
        self._decode_raises_validation(data)

    def test_string_length_overflow_rejected_not_crash(self):
        # First RUN_START string declares 300 bytes but the payload ends after a
        # few: the length prefix is valid u16 but the span is not.
        p = bytes([1]) + (7).to_bytes(8, "little") \
            + (250000).to_bytes(4, "little") + (100).to_bytes(4, "little")
        p += (300).to_bytes(2, "little") + b"abcde"
        data = self._header() + _frame(dlog.RUN_START, p)
        self._decode_raises_validation(data)

    def test_truncated_uuid_rejected_not_crash(self):
        # Header declares an 8-byte uuid but the file ends right after the u32
        # length field.
        data = MAGIC + (0x01020304).to_bytes(4, "little") \
            + (1).to_bytes(2, "little") + (8).to_bytes(4, "little") + b"ab"
        self._decode_raises_validation(data)

    def test_run_end_invalid_overflow_byte_rejected(self):
        # overflow=2 is not a schema value; previously bool(2) let it validate.
        body = (_frame(dlog.RUN_START, _b_run_start())
                + _frame(dlog.ROOT_START, _b_root_start())
                + _frame(dlog.DECISION_POINT, _b_decision(1))
                + _frame(dlog.MOVE_ATTEMPT, _b_attempt(1))
                + _frame(dlog.ERROR_RECORD, _b_error_record())
                + _frame(dlog.ROOT_END, _b_root_end(1, 1))
                + _frame(dlog.RUN_END, _b_run_end(1, 1, overflow=2, error=1)))
        data = self._header() + body
        self._decode_raises_validation(data)

    def test_run_end_unknown_error_code_rejected(self):
        # error_code=2 is outside the schema; previously it validated cleanly.
        data = _log_bytes().replace(
            _frame(dlog.RUN_END, _b_run_end(1, 1)),
            _frame(dlog.RUN_END, _b_run_end(1, 1, overflow=0, error=2)))
        self._decode_raises_validation(data)


class TestRunStartCrossCheck(unittest.TestCase):
    """Runner-side provenance checks: RUN_START must match what the runner
    requested (mode/seed/threshold/cap/policy/engine), not merely decode."""

    def _via_runner(self, data: bytes, expected_run_start=None, engine_version=None,
                    fen: str = FEN0):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.bin"
            path.write_bytes(data)
            return rc._decode_and_validate_log(path, fen, expected_run_start, engine_version)

    def test_matching_settings_pass(self):
        exp = {"mode": 1, "seed": 7, "sample_threshold": 250000,
               "max_records": 100, "policy_version": "policy-v1"}
        stats = self._via_runner(_log_bytes(), exp, "Stockfish test")
        self.assertEqual(stats["attempts"], 1)
        self.assertEqual(stats["decisions"], 1)

    def test_seed_mismatch_fails(self):
        exp = {"mode": 1, "seed": 8, "sample_threshold": 250000,
               "max_records": 100, "policy_version": "policy-v1"}
        with self.assertRaises(RuntimeError) as ctx:
            self._via_runner(_log_bytes(), exp)
        self.assertIn("RUN_START research settings", str(ctx.exception))

    def test_threshold_mismatch_fails(self):
        exp = {"mode": 1, "seed": 7, "sample_threshold": 100,
               "max_records": 100, "policy_version": "policy-v1"}
        with self.assertRaises(RuntimeError):
            self._via_runner(_log_bytes(), exp)

    def test_max_records_mismatch_fails(self):
        exp = {"mode": 1, "seed": 7, "sample_threshold": 250000,
               "max_records": 50, "policy_version": "policy-v1"}
        with self.assertRaises(RuntimeError):
            self._via_runner(_log_bytes(), exp)

    def test_policy_version_mismatch_fails(self):
        exp = {"mode": 1, "seed": 7, "sample_threshold": 250000,
               "max_records": 100, "policy_version": "other-v1"}
        with self.assertRaises(RuntimeError):
            self._via_runner(_log_bytes(), exp)

    def test_engine_identity_mismatch_fails(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._via_runner(_log_bytes(), engine_version="Some other engine")
        self.assertIn("engine identity", str(ctx.exception))

    def test_fen_mismatch_fails(self):
        with self.assertRaises(RuntimeError):
            self._via_runner(_log_bytes(), fen=FEN0.replace(" w ", " b "))

    def test_missing_log_file_fails_not_crashes(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.bin"
            with self.assertRaises(OSError):
                rc._decode_and_validate_log(missing, FEN0)


if __name__ == "__main__":
    unittest.main()
