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
    "SyzygyPath",
    "EvalFile",
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


def run_root(
    engine: Path,
    cwd: Path,
    fen: str,
    depth: int,
    hash_mb: int,
    eval_file: str | None,
    timeout: float,
) -> dict:
    """Run one root in a fresh engine process. Returns per-root result dict."""
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
) -> dict:
    compile_info = engine_compile_info(engine, cwd)
    uci_defaults = engine_uci_defaults(engine, cwd)
    git = git_state(cwd)
    hinfo = host_info()
    applied = {name: value for name, value in PINNED_UCI_OPTIONS}
    applied["Hash"] = str(hash_mb)
    applied["EvalFile"] = eval_file if eval_file else uci_defaults.get("EvalFile", "<engine default>")

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
        "compiler": compile_info,
        "host": hinfo,
        "threads": 1,
        "hash_mb": hash_mb,
        "search_depth": depth,
        "value_network": {
            "path": str(eval_file) if eval_file else None,
            "sha256": file_sha256(Path(eval_file)) if eval_file else None,
        },
        "policy_network": None,
        "corpus": {"path": str(Path(corpus_path).resolve()), "sha256": corpus_sha},
        "uci_options_applied": applied,
        "uci_engine_defaults": {k: uci_defaults.get(k) for k in DEFAULT_OPTIONS_TO_RECORD},
        "random_seed": None,
        "notes": (
            "Deterministic research profile: 1 thread, fixed depth, MultiPV 1, "
            "ponder off, full strength, no external stop, no tablebases "
            "(SyzygyPath default <empty>), fresh engine process per root."
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
    return ok, issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_eval_file(args, engine: Path, cwd: Path) -> str | None:
    if args.eval_file:
        path = Path(args.eval_file).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"--eval-file not found: {path}")
        return str(path)
    name = engine_uci_defaults(engine, cwd).get("EvalFile")
    if name:
        candidate = (cwd / "src" / name).resolve()
        if candidate.is_file():
            return str(candidate)
    return None


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run the corpus once and write a run manifest")
    common_parser(run_p)
    ver_p = sub.add_parser("verify", help="run the corpus twice and enforce the determinism gate")
    common_parser(ver_p)
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
    eval_file = _resolve_eval_file(args, engine, cwd)
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
