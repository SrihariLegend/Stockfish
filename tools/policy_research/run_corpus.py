#!/usr/bin/env python3
"""Deterministic research corpus runner and run manifest (policy research, Phase 1).

Purpose
-------
Run fixed-depth, single-thread searches over a versioned root corpus with a fresh
engine process per root, write a versioned run manifest (engine commit, compiler,
corpus checksum, UCI settings, ...), and verify determinism by running the corpus
twice and requiring exact agreement on best move, info rows (excluding wall-clock
`time` and derived `nps`), and node counts.

Guarantees the Phase 1 exit gate: repeated single-thread fixed-depth runs produce
no unexplained differences.

Usage
-----
    # Single run (writes manifest.json + results.json)
    python3 run_corpus.py run --depth 12

    # Determinism gate: run twice and compare (default depth 10)
    python3 run_corpus.py verify --depth 12 --hash 16

    # Smoke test a subset of roots
    python3 run_corpus.py verify --only c1-d-001,c1-v-003 --depth 8

All artifacts are written under --outdir (default tools/policy_research/runs,
git-ignored). Only manifests/scripts/fixtures are committed to the repository.

Schema versions:
    corpus/1           -- corpus file layout
    research-run/1     -- run manifest layout
    research-result/1  -- per-run results layout
    research-compare/1 -- verification comparison layout
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# Decoder/validator for the Phase 2 binary logs lives next to this runner.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import decode_research_log as dlog  # noqa: E402

SCHEMA_CORPUS = "corpus/v1"
SCHEMA_RUN = "research-run/1"
SCHEMA_RESULT = "research-result/1"
SCHEMA_COMPARE = "research-compare/1"

SETS = ("development", "validation", "test")

# UCI options the runner pins for every search (deterministic research profile).
PINNED_UCI_OPTIONS = [
    ("Threads", "1"),
    ("MultiPV", "1"),
    ("Ponder", "false"),
    ("Skill Level", "20"),
    ("UCI_LimitStrength", "false"),
    ("UCI_Chess960", "false"),
    ("SyzygyPath", "<empty>"),
]

# Options recorded as engine-declared defaults in the manifest (complement of the
# pinned set; anything not pinned must stay at its default for the runs to be
# comparable to the documented protocol).
DEFAULT_OPTIONS_TO_RECORD = [
    "Threads",
    "Hash",
    "MultiPV",
    "Ponder",
    "Skill Level",
    "UCI_LimitStrength",
    "UCI_Chess960",
    "SyzygyPath",
    "EvalFile",
]

# Research options for the verify-research passes (defaults are recorded only
# when the engine actually declares the option, i.e. POLICY_RESEARCH builds).
RESEARCH_OPTIONS_TO_RECORD = [
    "PolicyResearch",
    "PolicyResearchMode",
    "PolicyResearchLogPath",
    "PolicyResearchSeed",
    "PolicyResearchSampleRate",
    "PolicyResearchMaxRecords",
    "PolicyResearchPolicyVersion",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Corpus handling
# ---------------------------------------------------------------------------

def load_corpus(path: Path) -> tuple[dict, bytes]:
    """Validate and load a corpus file. Returns (corpus_dict, canonical_bytes)."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        corpus = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corpus {path} is not valid JSON: {exc}") from exc

    if corpus.get("schema") != SCHEMA_CORPUS:
        raise ValueError(f"corpus {path}: schema must be {SCHEMA_CORPUS}")
    if not isinstance(corpus.get("corpus_id"), str) or not corpus["corpus_id"]:
        raise ValueError(f"corpus {path}: corpus_id must be a non-empty string")
    positions = corpus.get("positions")
    if not isinstance(positions, list) or not positions:
        raise ValueError(f"corpus {path}: positions must be a non-empty list")

    seen_ids: set[str] = set()
    for pos in positions:
        for key in ("id", "set", "fen", "source", "license"):
            if not isinstance(pos.get(key), str) or not pos.get(key):
                raise ValueError(f"corpus {path}: position missing string field '{key}'")
        if pos["id"] in seen_ids:
            raise ValueError(f"corpus {path}: duplicate position id '{pos['id']}'")
        seen_ids.add(pos["id"])
        if pos["set"] not in SETS:
            raise ValueError(
                f"corpus {path}: position '{pos['id']}' has unknown set '{pos['set']}' "
                f"(expected one of {SETS})"
            )
        fen_fields = pos["fen"].split()
        if len(fen_fields) != 6:
            raise ValueError(
                f"corpus {path}: position '{pos['id']}' FEN must have 6 fields, "
                f"got {len(fen_fields)}"
            )
    return corpus, canonical_json_bytes(corpus)


def corpus_sha256(canonical_bytes: bytes) -> str:
    return sha256_bytes(canonical_bytes)


# ---------------------------------------------------------------------------
# Engine / environment information
# ---------------------------------------------------------------------------

def _run_process(engine: Path, script: str, timeout: float, cwd: Path) -> tuple[str, str, int]:
    try:
        proc = subprocess.run(
            [str(engine)],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"engine {engine} timed out after {timeout}s") from exc
    return proc.stdout, proc.stderr, proc.returncode


class EngineSession:
    """Interactive engine process.

    The engine's UCI loop returns immediately after `go` (so it can react to
    `stop`/`ponderhit`); a `quit` buffered directly after `go` therefore cancels
    the search. We must keep the pipe open and only send `quit` after the
    terminal `bestmove` line has been observed.
    """

    def __init__(self, engine: Path, cwd: Path):
        self.proc = subprocess.Popen(
            [str(engine)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(cwd),
        )
        self._lines: "queue.Queue[str | None]" = queue.Queue()

        def reader() -> None:
            try:
                for line in self.proc.stdout:  # type: ignore[union-attr]
                    self._lines.put(line)
            except Exception:
                pass
            finally:
                self._lines.put(None)  # EOF sentinel

        self._thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()

    def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def read_until(self, predicate, timeout: float, what: str) -> list[str]:
        """Read stdout lines until predicate(line) is true (or EOF/timeout).

        Returns every line read, including the matching one, with newlines
        stripped. The first line satisfying the predicate is included.
        """
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"engine did not produce {what} within {timeout}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"engine did not produce {what} within {timeout}s") from exc
            if line is None:
                raise RuntimeError(f"engine exited while waiting for {what}")
            line = line.rstrip("\n")
            collected.append(line)
            if predicate(line):
                return collected

    def close(self, timeout: float = 30.0) -> None:
        try:
            self.send("quit")
        except Exception:
            pass
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._thread.join(timeout=10)

    def terminate(self) -> None:
        self.proc.kill()
        self._thread.join(timeout=10)



def engine_compile_info(engine: Path, cwd: Path, timeout: float = 60.0) -> dict:
    """Ask the engine for its compile banner (via the `compiler` command)."""
    stdout, _, _ = _run_process(engine, "compiler\n", timeout, cwd)
    info: dict = {}
    patterns = {
        "compiled_by": r"Compiled by\s*:\s*(.*)",
        "arch": r"Compilation architecture\s*:\s*(.*)",
        "settings": r"Compilation settings\s*:\s*(.*)",
        "compiler_version": r"Compiler __VERSION__ macro\s*:\s*(.*)",
    }
    for key, pat in patterns.items():
        match = re.search(pat, stdout)
        if match:
            info[key] = match.group(1).strip()
    return info


def engine_research_options_meta(engine: Path, cwd: Path, timeout: float = 60.0) -> tuple[set, dict]:
    """(declared option names, spin min/max) parsed from the engine's own `uci`
    reply.

    An out-of-range spin value is silently rejected by the engine (ucioption.cpp
    bounds check), so the runner validates CLI research values against the same
    declared ranges instead of discovering the rejection from silent logs.
    """
    stdout, _, _ = _run_process(engine, "uci\nquit\n", timeout, cwd)
    names: set = set()
    bounds: dict = {}
    for line in stdout.splitlines():
        m = re.match(r"option name (.+?) type (\S+)", line)
        if not m:
            continue
        names.add(m.group(1))
        if m.group(2) == "spin":
            b = re.search(r"min (-?\d+) max (-?\d+)", line)
            if b:
                bounds[m.group(1)] = (int(b.group(1)), int(b.group(2)))
    return names, bounds


def engine_banner(engine: Path, cwd: Path, timeout: float = 60.0) -> str | None:
    """Return the engine's identity line from `uci` (e.g. the embedded commit).

    The first line of the `uci` reply is the `id name` banner, which embeds the
    source commit the executable was built from. This is recorded because the
    repository HEAD is not a reliable proxy for what a given binary contains.
    """
    stdout, _, _ = _run_process(engine, "uci\nquit\n", timeout, cwd)
    for line in stdout.splitlines():
        line = line.strip()
        if line and not line.startswith(("option ", "uciok")):
            return line
    return None


def engine_uci_defaults(engine: Path, cwd: Path, timeout: float = 60.0) -> dict:
    """Return engine-declared defaults for the UCI options we record."""
    stdout, _, _ = _run_process(engine, "uci\nquit\n", timeout, cwd)
    defaults: dict = {}
    for line in stdout.splitlines():
        if not line.startswith("option name "):
            continue
        name_match = re.match(r"option name (.+?) type \w+", line)
        if not name_match:
            continue
        name = name_match.group(1)
        if name in DEFAULT_OPTIONS_TO_RECORD:
            default_match = re.search(r"default (.*)$", line)
            defaults[name] = default_match.group(1) if default_match else None
    return defaults


def host_info() -> dict:
    cpu = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        cpu = platform.processor() or "unknown"
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": cpu,
        "python": platform.python_version(),
    }


def git_state(cwd: Path) -> dict:
    """Engine-commit + dirty status of the repository containing cwd."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        commit = "unknown"
    try:
        porcelain = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout
        dirty_files = [line for line in porcelain.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        dirty_files = []
    return {
        "commit": commit,
        "commit_short": commit[:12] if commit and commit != "unknown" else "unknown",
        "dirty": bool(dirty_files),
        "dirty_file_count": len(dirty_files),
    }


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Search execution
# ---------------------------------------------------------------------------

_NPS_TIME_RE = re.compile(r"\s+(?:time|nps) \d+")
_WS_RE = re.compile(r"\s+")


def normalize_info_row(line: str) -> str:
    """Canonicalize one `info ...` row for equality comparison.

    Wall time and its derived nps are excluded by design (plan section 6.4);
    nodes, hashfull, score, bounds, PV, and all other content must match exactly.
    """
    line = _NPS_TIME_RE.sub("", line)
    return _WS_RE.sub(" ", line).strip()


def summarize_info_row(line: str) -> dict:
    """Extract {score, bound, nodes, pv} from the final info row of a search."""
    summary = {"score": None, "bound": None, "nodes": None, "pv": None}
    score_match = re.search(r"\bscore (cp -?\d+|mate -?\d+)\b", line)
    if score_match:
        summary["score"] = score_match.group(1)
    bound_match = re.search(r"\b(lowerbound|upperbound)\b", line)
    if bound_match:
        summary["bound"] = bound_match.group(1)
    nodes_match = re.search(r"\bnodes (\d+)\b", line)
    if nodes_match:
        summary["nodes"] = int(nodes_match.group(1))
    pv_match = re.search(r"\bpv (.*)$", line)
    if pv_match:
        summary["pv"] = pv_match.group(1).strip()
    return summary


def validate_completion(info_lines: list[str], expected_depth: int) -> list[str]:
    """Return a list of problems proving the fixed-depth search completed.

    An engine that was cancelled (e.g. by a stray `quit`) can emit an
    `info depth 1 ... pv` row with zero nodes and then a fallback bestmove; two
    such identical aborted searches would otherwise satisfy the determinism
    gate. Completion requires, for a fixed-depth `go depth N` search:

    - at least one info row with a score and a non-empty PV;
    - a positive node count in the final row;
    - the final row reaches depth N, OR a mate was announced at any depth
      (mate announcements legitimately end the iteration ladder early).
    """
    problems: list[str] = []
    if not info_lines:
        return ["no info rows were emitted"]

    rows = [normalize_info_row(ln) for ln in info_lines]
    scores = [r for r in rows if re.search(r"\bscore (cp -?\d+|mate -?\d+)\b", r)]
    if not scores:
        problems.append("no info row carries a score")

    pv_rows = [r for r in rows if re.search(r"\bpv [a-h][1-8]", r)]
    if not pv_rows:
        problems.append("no info row carries a non-empty PV")

    last = rows[-1]
    nodes_match = re.search(r"\bnodes (\d+)\b", last)
    if not nodes_match or int(nodes_match.group(1)) <= 0:
        problems.append("final row has no positive node count")

    if not any(re.search(r"\bscore mate -?\d+\b", r) for r in rows):
        depth_match = re.search(r"\bdepth (\d+)\b", last)
        if not depth_match or int(depth_match.group(1)) < expected_depth:
            problems.append(
                f"final row depth {depth_match.group(1) if depth_match else '?'} "
                f"< requested {expected_depth} without a mate announcement"
            )
    return problems


def run_root(
    engine: Path,
    cwd: Path,
    fen: str,
    depth: int,
    hash_mb: int,
    eval_file: str,
    timeout: float,
    extra_options: list[tuple[str, str]] | None = None,
) -> dict:
    """Run one root in a fresh engine process. Returns per-root result dict.

    extra_options are applied (in order) after the pinned profile; used by the
    research passes to set PolicyResearch* options per root.
    """
    session = EngineSession(engine, cwd)
    wall_ms = 0
    try:
        start = time.perf_counter()
        session.send("uci")
        lines = session.read_until(lambda line: line.strip() == "uciok", timeout, "uciok")
        for name, value in PINNED_UCI_OPTIONS:
            session.send(f"setoption name {name} value {value}")
        session.send(f"setoption name Hash value {hash_mb}")
        if eval_file:
            session.send(f"setoption name EvalFile value {eval_file}")
        for name, value in (extra_options or []):
            session.send(f"setoption name {name} value {value}")
        session.send("isready")
        session.read_until(lambda line: line.strip() == "readyok", timeout, "readyok")
        session.send("ucinewgame")
        session.send("isready")
        session.read_until(lambda line: line.strip() == "readyok", timeout, "readyok")
        session.send(f"position fen {fen}")
        session.send(f"go depth {depth}")
        lines = session.read_until(
            lambda line: line.startswith("bestmove "), timeout, "bestmove"
        )
        wall_ms = int((time.perf_counter() - start) * 1000)
    except (TimeoutError, RuntimeError) as exc:
        session.terminate()
        raise RuntimeError(f"engine failed for root:\n{exc}") from exc
    finally:
        session.close()

    info_lines = [ln for ln in lines if ln.startswith("info ")]
    bestmove = None
    for ln in lines:
        if ln.startswith("bestmove "):
            bestmove = ln[len("bestmove ") :].strip()
            break
    if "CRITICAL ERROR" in "\n".join(lines) or bestmove is None:
        tail = "\n".join(lines)[-1200:]
        raise RuntimeError(f"engine failed for root:\n{tail}")

    completion_problems = validate_completion(info_lines, depth)
    if completion_problems:
        raise RuntimeError(
            "search for root did not demonstrably complete fixed-depth search "
            f"(expected depth {depth}); problems: {completion_problems}"
        )

    rows = [normalize_info_row(ln) for ln in info_lines]
    pv_rows = [ln for ln in info_lines if re.search(r"\bpv ", ln)]
    summary = summarize_info_row(pv_rows[-1]) if pv_rows else {}
    return {
        "bestmove": bestmove,
        "rows": rows,
        "summary": summary,
        "wall_ms": wall_ms,
    }


# ---------------------------------------------------------------------------
# Run assembly
# ---------------------------------------------------------------------------

def make_manifest(
    *,
    engine: Path,
    cwd: Path,
    corpus_path: Path,
    corpus_sha: str,
    corpus_id: str,
    eval_file: str | None,
    depth: int,
    hash_mb: int,
    build_command: str | None = None,
    extra_uci_options: list[tuple[str, str]] | None = None,
) -> dict:
    compile_info = engine_compile_info(engine, cwd)
    uci_defaults = engine_uci_defaults(engine, cwd)
    git = git_state(cwd)
    hinfo = host_info()
    applied = {name: value for name, value in PINNED_UCI_OPTIONS}
    applied["Hash"] = str(hash_mb)
    applied["EvalFile"] = eval_file if eval_file else uci_defaults.get("EvalFile", "<engine default>")
    for name, value in (extra_uci_options or []):
        # Applied after the pinned profile (per-root policy options; see notes).
        applied[name] = value

    manifest = {
        "schema_version": SCHEMA_RUN,
        "run_uuid": str(uuid.uuid4()),
        "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "corpus_id": corpus_id,
        "engine_commit": git["commit"],
        "engine_commit_short": git["commit_short"],
        "worktree_dirty": git["dirty"],
        "worktree_dirty_file_count": git["dirty_file_count"],
        "engine_executable": str(engine.resolve()),
        "engine_executable_sha256": file_sha256(engine),
        "engine_banner": engine_banner(engine, cwd),
        "build_command": build_command,
        "compiler": compile_info,
        "host": hinfo,
        "threads": 1,
        "hash_mb": hash_mb,
        "search_depth": depth,
        "value_network": {
            "path": str(Path(eval_file).resolve()) if eval_file else None,
            "sha256": file_sha256(Path(eval_file)) if eval_file else None,
        },
        "policy_network": None,
        "corpus": {"path": str(Path(corpus_path).resolve()), "sha256": corpus_sha},
        "uci_options_applied": applied,
        "uci_engine_defaults": {
            **{k: uci_defaults.get(k) for k in DEFAULT_OPTIONS_TO_RECORD},
            **{k: uci_defaults.get(k) for k in RESEARCH_OPTIONS_TO_RECORD},
        },
        "random_seed": None,
        "notes": (
            "Deterministic research profile: 1 thread, fixed depth, MultiPV 1, "
            "ponder off, full strength, UCI_Chess960 false, no external stop, "
            "tablebases disabled (SyzygyPath explicitly cleared to <empty>), "
            "fresh engine process per root. Record engine_executable_sha256 / "
            "engine_banner (embedded source commit) / build_command to identify "
            "the exact binary; the repository HEAD alone is not sufficient."
        ),
    }
    return manifest


def execute_run(engine: Path, cwd: Path, corpus: dict, corpus_sha: str, args) -> dict:
    manifest = make_manifest(
        engine=engine,
        cwd=cwd,
        corpus_path=args.corpus,
        corpus_sha=corpus_sha,
        corpus_id=corpus["corpus_id"],
        eval_file=args.eval_file,
        depth=args.depth,
        hash_mb=args.hash,
        build_command=getattr(args, "build_command", None),
    )
    positions = []
    for pos in corpus["positions"]:
        if args.only and pos["id"] not in args.only:
            continue
        result = run_root(
            engine, cwd, pos["fen"], args.depth, args.hash, args.eval_file, args.timeout
        )
        positions.append(
            {
                "id": pos["id"],
                "set": pos["set"],
                "fen": pos["fen"],
                "bestmove": result["bestmove"],
                "rows": result["rows"],
                "summary": result["summary"],
                "wall_ms": result["wall_ms"],
            }
        )
        nodes = result["summary"].get("nodes")
        nodes_str = str(nodes) if nodes is not None else "-"
        print(
            f"  {pos['id']:<12} {pos['set']:<12} nodes={nodes_str:>10} "
            f"best={result['bestmove']:<24} {result['wall_ms'] / 1000.0:6.1f}s"
        )
    return {"schema_version": SCHEMA_RESULT, "manifest": manifest, "positions": positions}


def write_run(out_dir: Path, run: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(run["manifest"], fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(out_dir / "results.json", "w", encoding="utf-8") as fh:
        json.dump(run, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return out_dir


# ---------------------------------------------------------------------------
# Determinism comparison
# ---------------------------------------------------------------------------

def compare_results(run_a: dict, run_b: dict) -> tuple[bool, list[dict]]:
    """Compare two runs. Equality is exact on bestmove and normalized info rows."""
    by_id_b = {pos["id"]: pos for pos in run_b["positions"]}
    issues: list[dict] = []
    ok = True
    for pos_a in run_a["positions"]:
        pos_b = by_id_b.get(pos_a["id"])
        entry = {"id": pos_a["id"], "ok": True, "differences": []}
        if pos_b is None:
            entry.update({"ok": False, "differences": ["missing in run B"]})
            issues.append(entry)
            ok = False
            continue
        if pos_a["bestmove"] != pos_b["bestmove"]:
            entry["differences"].append(
                f"bestmove: A={pos_a['bestmove']} B={pos_b['bestmove']}"
            )
        if pos_a["rows"] != pos_b["rows"]:
            a_rows, b_rows = pos_a["rows"], pos_b["rows"]
            n = min(len(a_rows), len(b_rows))
            first_diff = next(
                (i for i in range(n) if a_rows[i] != b_rows[i]), min(len(a_rows), len(b_rows))
            )
            entry["differences"].append(
                f"info rows differ (A={len(a_rows)}, B={len(b_rows)}); "
                f"first divergence at row {first_diff}: "
                f"A={a_rows[first_diff] if first_diff < len(a_rows) else '<end>'} | "
                f"B={b_rows[first_diff] if first_diff < len(b_rows) else '<end>'}"
            )
        entry["ok"] = not entry["differences"]
        ok = ok and entry["ok"]
        issues.append(entry)
    # Asymmetry check: positions present in run B but not in run A.
    ids_a = {pos["id"] for pos in run_a["positions"]}
    for pos_b in run_b["positions"]:
        if pos_b["id"] not in ids_a:
            issues.append(
                {"id": pos_b["id"], "ok": False, "differences": ["extra position in run B"]}
            )
            ok = False
    return ok, issues


# ---------------------------------------------------------------------------
# Phase 2: research logging gate (verify-research)
# ---------------------------------------------------------------------------


def _research_static_options(args) -> list[tuple[str, str]]:
    """Research options shared by every root of an `on` pass (log path is
    per root and appended by the caller)."""
    return [
        ("PolicyResearch", "on"),
        ("PolicyResearchMode", "observational"),
        ("PolicyResearchSeed", str(args.research_seed)),
        ("PolicyResearchSampleRate", str(args.research_sample_rate)),
        ("PolicyResearchMaxRecords", str(args.research_max_records)),
        ("PolicyResearchPolicyVersion", args.research_policy_version),
    ]


def execute_research_pass(
    engine: Path,
    cwd: Path,
    corpus: dict,
    corpus_sha: str,
    args,
    pass_label: str,
    log_root: Path,
    enabled: bool,
) -> dict:
    """One corpus pass with optional observational logging (per-root log files
    under log_root). Returns the run dict (same shape as execute_run)."""
    static = _research_static_options(args) if enabled else []
    manifest = make_manifest(
        engine=engine,
        cwd=cwd,
        corpus_path=Path(args.corpus),
        corpus_sha=corpus_sha,
        corpus_id=corpus["corpus_id"],
        eval_file=args.eval_file,
        depth=args.depth,
        hash_mb=args.hash,
        build_command=getattr(args, "build_command", None),
        extra_uci_options=(static if enabled else None),
    )
    if enabled:
        manifest["research_log_root"] = str(log_root)
        manifest["research_pass"] = pass_label
        manifest["random_seed"] = args.research_seed
        manifest["research_data_schema"] = "research-data/1"
        manifest["research_log_container"] = "research-log/1"
        manifest["uci_options_applied"]["PolicyResearchLogPath"] = (
            "<per root>" + " " + str(log_root / "root-<POSITION_ID>.bin")
        )

    positions = []
    for pos in corpus["positions"]:
        if args.only and pos["id"] not in args.only:
            continue
        extra = None
        log_path = None
        if enabled:
            log_root.mkdir(parents=True, exist_ok=True)
            log_path = log_root / f"root-{pos['id']}.bin"
            extra = static + [("PolicyResearchLogPath", str(log_path))]
        result = run_root(
            engine, cwd, pos["fen"], args.depth, args.hash, args.eval_file, args.timeout, extra
        )
        positions.append(
            {
                "id": pos["id"],
                "set": pos["set"],
                "fen": pos["fen"],
                "bestmove": result["bestmove"],
                "rows": result["rows"],
                "summary": result["summary"],
                "wall_ms": result["wall_ms"],
                "log_path": str(log_path) if log_path else None,
            }
        )
        nodes = result["summary"].get("nodes")
        nodes_str = str(nodes) if nodes is not None else "-"
        print(
            f"  {pos['id']:<12} {pos['set']:<12} nodes={nodes_str:>10} "
            f"best={result['bestmove']:<24} {result['wall_ms'] / 1000.0:6.1f}s"
        )
    return {"schema_version": SCHEMA_RESULT, "manifest": manifest, "positions": positions}


def _decode_and_validate_log(log_path: Path, expected_fen: str,
                             expected_run_start: dict | None = None,
                             engine_version: str | None = None):
    """Decode + validate one research log; cross-check the root FEN against the
    corpus root, and (for `on` passes) cross-check RUN_START's recorded research
    settings (mode/seed/threshold/cap/policy) and engine identity against what
    the runner requested. Returns the decode stats dict."""
    header, records = dlog.decode_file(log_path)
    stats = dlog.validate(records, log_path)
    roots = stats["roots"]
    if len(roots) != 1:
        raise RuntimeError(f"{log_path}: expected exactly one root in the log")
    (rk, root_stats), = roots.items()
    if root_stats["fen"] != expected_fen:
        raise RuntimeError(
            f"{log_path}: logged root FEN does not match corpus root\n"
            f"  log:    {root_stats['fen']}\n  corpus: {expected_fen}"
        )
    run_start = records[0]
    if expected_run_start is not None:
        got = {k: run_start[k] for k in expected_run_start}
        if got != expected_run_start:
            raise RuntimeError(
                f"{log_path}: RUN_START research settings do not match the run request\n"
                f"  requested: {expected_run_start}\n  logged:    {got}"
            )
    if engine_version is not None:
        logged_version = run_start["engine_info"].splitlines()[0] if run_start["engine_info"] else ""
        if logged_version != engine_version:
            raise RuntimeError(
                f"{log_path}: RUN_START engine identity does not match the run executable\n"
                f"  log:    {logged_version}\n  expect: {engine_version}"
            )
    return {"root_key": rk, "decisions": root_stats["decisions"],
            "attempts": stats["run_attempts"], "outcomes": stats["outcomes"],
            "overflow": stats["overflow"]}


def run_research_gate(args) -> int:
    """Protocol P2.1 (docs/policy-research/experiment-protocols.md):

    1. off pass (logging disabled),
    2. on passes 1 and 2 (identical research options, per-root logs),
    3. enforce: off == on1 == on2 search results (bestmove/rows), on1 logs ==
       on2 logs (payload-identical), every log decodes cleanly, every logged
       root FEN matches the corpus root FEN.
    """
    cwd = repo_root()
    engine = Path(args.engine)
    corpus_path = Path(args.corpus)
    corpus, canonical = load_corpus(corpus_path)
    corpus_sha = corpus_sha256(canonical)

    out_root = Path(args.outdir or (cwd / "tools" / "policy_research" / "runs")).expanduser().resolve()
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    label_dir = out_root / f"{corpus['corpus_id']}-d{args.depth}-h{args.hash}-research-{stamp}"

    print("== research pass: off (logging disabled) ==")
    off_run = execute_research_pass(engine, cwd, corpus, corpus_sha, args,
                                    "off", label_dir / "logs-off", False)
    print("== research pass: on #1 ==")
    on1_run = execute_research_pass(engine, cwd, corpus, corpus_sha, args,
                                    "on-1", label_dir / "logs-on-1", True)
    print("== research pass: on #2 ==")
    on2_run = execute_research_pass(engine, cwd, corpus, corpus_sha, args,
                                    "on-2", label_dir / "logs-on-2", True)

    dir_off = write_run(label_dir / "run-off", off_run)
    dir_on1 = write_run(label_dir / "run-on-1", on1_run)
    dir_on2 = write_run(label_dir / "run-on-2", on2_run)

    problems: list[dict] = []

    def record(ok: bool, what: str, detail: str = ""):
        problems.append({"ok": ok, "check": what, "detail": detail})

    # 1. search-result equality across all three passes.
    for pair, name_a, name_b in (
        ((off_run, on1_run), "off", "on-1"),
        ((off_run, on2_run), "off", "on-2"),
        ((on1_run, on2_run), "on-1", "on-2"),
    ):
        ok, issues = compare_results(pair[0], pair[1])
        detail = ""
        if not ok:
            detail = "; ".join(
                f"{e['id']}: {e['differences'][0]}" for e in issues if not e["ok"]
            )
        record(ok, f"search results identical ({name_a} vs {name_b})", detail)

    # 2. decode + validate every log; check root FEN matches the corpus root and
    #    that RUN_START records the research settings and engine the runner
    #    actually requested (so a silently unapplied option fails the gate).
    logs_on1 = {p["id"]: Path(p["log_path"]) for p in on1_run["positions"]}
    logs_on2 = {p["id"]: Path(p["log_path"]) for p in on2_run["positions"]}
    banner = (on1_run["manifest"].get("engine_banner") or "").strip()
    if banner.startswith("id name "):
        banner = banner[len("id name "):]
    engine_version = banner.removesuffix(" by the Stockfish developers (see AUTHORS file)")
    expected_run_start = {
        "mode": 1,  # Research::Mode::Observational
        "seed": args.research_seed,
        "sample_threshold": int(args.research_sample_rate * 1_000_000 + 0.5),
        "max_records": min(args.research_max_records, 1 << 22),
        "policy_version": args.research_policy_version,
    }
    stats_on1: dict = {}
    stats_on2: dict = {}
    for pos in corpus["positions"]:
        if args.only and pos["id"] not in args.only:
            continue
        try:
            s1 = _decode_and_validate_log(logs_on1[pos["id"]], pos["fen"],
                                          expected_run_start, engine_version)
            s2 = _decode_and_validate_log(logs_on2[pos["id"]], pos["fen"],
                                          expected_run_start, engine_version)
            stats_on1[pos["id"]] = s1
            stats_on2[pos["id"]] = s2
        except (RuntimeError, dlog.ValidationError, OSError) as exc:
            record(False, f"log decode/validate {pos['id']}", str(exc))
            continue

    # 3. decoded record-stream equality between on passes (deterministic sampling).
    all_logs_equal = True
    for pid in stats_on1:
        try:
            res = dlog.compare(logs_on1[pid], logs_on2[pid])
        except (dlog.ValidationError, OSError) as exc:
            all_logs_equal = False
            record(False, f"decoded records identical ({pid})", str(exc))
            continue
        if not res["equal"]:
            all_logs_equal = False
            record(False, f"decoded records identical ({pid})",
                   f"records_on1={res['count_a']} records_on2={res['count_b']}")
    record(all_logs_equal, "decoded record streams identical (on-1 vs on-2)")

    log_rows = [
        {"id": pid, **s} for pid, s in stats_on1.items()
    ]
    ok_total = all(p["ok"] for p in problems)
    summary = {
        "schema_version": "research-gate/1",
        "overall_ok": ok_total,
        "checks": problems,
        "logs": {"on-1": stats_on1, "on-2": stats_on2},
        "run_dirs": {"off": dir_off.name, "on-1": dir_on1.name, "on-2": dir_on2.name},
    }
    _ = log_rows
    with open(label_dir / "research_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print("\n== research logging gate result ==")
    for p in problems:
        print(f"  {'PASS' if p['ok'] else 'FAIL'}  {p['check']}"
              + (f"  [{p['detail']}]" if p["detail"] else ""))
    for pid, s in stats_on1.items():
        print(f"  log {pid:<12} decisions={s['decisions']:<7} attempts={s['attempts']:<7} "
              f"outcomes={s['outcomes']}")
    print(f"overall: {'PASS' if ok_total else 'FAIL'}")
    print(f"artifacts under {label_dir}")
    return 0 if ok_total else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_eval_file(engine: Path, cwd: Path, explicit: str | None) -> str | None:
    """Resolve the value-network file, mirroring the engine's search order.

    The engine looks for its EvalFile default relative to the binary directory
    and the current directory. We additionally try <repo>/src. Raises SystemExit
    when nothing is found: research runs must always pin an explicit network
    whose checksum is recorded in the manifest.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"--eval-file not found: {path}")
        return str(path)
    name = engine_uci_defaults(engine, cwd).get("EvalFile")
    if not name:
        raise SystemExit("engine reported no EvalFile default; pass --eval-file explicitly")
    candidates = [
        cwd / "src" / name,
        engine.resolve().parent / name,
        cwd / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise SystemExit(
        f"cannot locate value network '{name}' next to the engine or in the "
        f"repository; build it with 'make -C src net' or pass --eval-file"
    )


def common_parser(subp) -> None:
    subp.add_argument("--engine", default=None, help="engine executable (default <repo>/src/stockfish)")
    subp.add_argument(
        "--corpus",
        default=None,
        help=f"corpus JSON (default <repo>/tools/policy_research/corpora/corpus-v1.json)",
    )
    subp.add_argument("--depth", type=int, default=10, help="fixed search depth (default 10)")
    subp.add_argument("--hash", type=int, default=16, help="TT hash size in MB (default 16)")
    subp.add_argument("--outdir", default=None, help="output directory (default <repo>/tools/policy_research/runs)")
    subp.add_argument("--eval-file", default=None, help="absolute path to the value network file")
    subp.add_argument("--only", default=None, help="comma-separated subset of corpus ids (smoke tests)")
    subp.add_argument("--timeout", type=float, default=900.0, help="per-root timeout in seconds")
    subp.add_argument(
        "--build-command",
        default=None,
        help="verbatim build command that produced --engine (recorded in the manifest)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run the corpus once and write a run manifest")
    common_parser(run_p)
    ver_p = sub.add_parser("verify", help="run the corpus twice and enforce the determinism gate")
    common_parser(ver_p)
    res_p = sub.add_parser(
        "verify-research",
        help="run the corpus three times (off / on / on) and enforce the Phase 2 "
        "research logging gate (protocol P2.1)",
    )
    common_parser(res_p)
    res_p.add_argument("--research-sample-rate", type=float, default=0.05,
                       help="PolicyResearchSampleRate for the `on` passes")
    res_p.add_argument("--research-seed", type=int, default=101,
                       help="PolicyResearchSeed for the `on` passes")
    res_p.add_argument("--research-max-records", type=int, default=250000,
                       help="PolicyResearchMaxRecords for the `on` passes")
    res_p.add_argument("--research-policy-version", default="baseline-observational-v1",
                       help="PolicyResearchPolicyVersion for the `on` passes")
    args = parser.parse_args(argv)

    cwd = repo_root()
    engine = Path(args.engine or (cwd / "src" / "stockfish")).expanduser().resolve()
    if not engine.is_file():
        raise SystemExit(f"engine not found: {engine}")
    corpus_path = Path(
        args.corpus
        or (cwd / "tools" / "policy_research" / "corpora" / "corpus-v1.json")
    ).resolve()
    if not corpus_path.is_file():
        raise SystemExit(f"corpus not found: {corpus_path}")

    corpus, canonical = load_corpus(corpus_path)
    corpus_sha = corpus_sha256(canonical)
    eval_file = _resolve_eval_file(engine, cwd, args.eval_file)
    args.only = set(args.only.split(",")) if args.only else None
    args.corpus = str(corpus_path)
    args.engine = str(engine)
    args.eval_file = eval_file

    if args.only:
        known = {pos["id"] for pos in corpus["positions"]}
        unknown = args.only - known
        if unknown:
            raise SystemExit(f"--only ids not in corpus: {sorted(unknown)}")
        selected = [pos for pos in corpus["positions"] if pos["id"] in args.only]
    else:
        selected = corpus["positions"]
    print(f"corpus: {corpus['corpus_id']} ({len(selected)} roots) sha256={corpus_sha[:16]}...")
    print(f"engine: {engine}  depth={args.depth} hash={args.hash}MB  eval={eval_file or '<engine default>'}")

    out_root = Path(args.outdir or (cwd / "tools" / "policy_research" / "runs")).expanduser().resolve()
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    label_dir = out_root / f"{corpus['corpus_id']}-d{args.depth}-h{args.hash}-{stamp}"

    if args.command == "verify-research":
        if not (0.0 < args.research_sample_rate <= 1.0):
            raise SystemExit("--research-sample-rate must be in (0, 1]")
        if args.research_max_records <= 0:
            raise SystemExit("--research-max-records must be positive")
        # Fail fast when the engine is not a POLICY_RESEARCH build or when a
        # research spin value falls outside the engine's own declared range (the
        # engine silently ignores out-of-range spin values, which would otherwise
        # surface as an unapplied-option gate failure deep into the run).
        names, spin_bounds = engine_research_options_meta(engine, cwd)
        required = ["PolicyResearch", "PolicyResearchMode", "PolicyResearchSeed",
                    "PolicyResearchSampleRate", "PolicyResearchMaxRecords",
                    "PolicyResearchPolicyVersion"]
        missing = [n for n in required if n not in names]
        if missing:
            raise SystemExit(
                "engine does not declare required research options "
                f"({', '.join(missing)}); build with 'make -C src research-build' "
                "or pass a research executable via --engine"
            )
        for flag, opt, value in (
            ("research_seed", "PolicyResearchSeed", args.research_seed),
            ("research_max_records", "PolicyResearchMaxRecords",
             args.research_max_records),
        ):
            lo, hi = spin_bounds.get(opt, (0, (1 << 31) - 1))
            if not (lo <= value <= hi):
                raise SystemExit(
                    f"--{flag.replace('_', '-')}: value {value} is outside the engine's "
                    f"declared range [{lo}, {hi}] for option '{opt}'"
                )
        print(f"corpus: {corpus['corpus_id']} ({len(selected)} roots) "
              f"sha256={corpus_sha[:16]}...")
        print(f"engine: {engine}  depth={args.depth} hash={args.hash}MB "
              f"eval={eval_file or '<engine default>'}")
        return run_research_gate(args)

    if args.command == "run":
        print("== run A (single pass) ==")
        run_a = execute_run(engine, cwd, corpus, corpus_sha, args)
        run_dir = write_run(label_dir / f"run-{run_a['manifest']['run_uuid'][:8]}", run_a)
        print(f"wrote {run_dir}")
        print(f"determinism gate NOT enforced (single pass); use `verify`.")
        return 0

    print("== run A ==")
    run_a = execute_run(engine, cwd, corpus, corpus_sha, args)
    print("== run B ==")
    run_b = execute_run(engine, cwd, corpus, corpus_sha, args)

    ok, issues = compare_results(run_a, run_b)
    dir_a = write_run(label_dir / f"run-{run_a['manifest']['run_uuid'][:8]}", run_a)
    dir_b = write_run(label_dir / f"run-{run_b['manifest']['run_uuid'][:8]}", run_b)
    comparison = {
        "schema_version": SCHEMA_COMPARE,
        "overall_ok": ok,
        "run_a": dir_a.name,
        "run_b": dir_b.name,
        "excluded_fields": ["time", "nps", "wall_ms"],
        "positions": issues,
    }
    compare_path = label_dir / "comparison.json"
    with open(compare_path, "w", encoding="utf-8") as fh:
        json.dump(comparison, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print("== determinism result ==")
    for entry in issues:
        status = "PASS" if entry["ok"] else "FAIL"
        print(f"  {entry['id']:<12} {status}")
        for diff in entry["differences"]:
            print(f"      {diff}")
    print(f"overall: {'PASS' if ok else 'FAIL'}")
    print(f"artifacts under {label_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
