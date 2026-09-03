#!/usr/bin/env python3
"""Phase 3 dataset collection and calibration-baseline reporting.

One analysis row per *searched quiet move* in a sampled eligible node
(DECISION_POINT + its MOVE_ATTEMPTs) of the committed research-data/1 schema,
behavior-conditioned on the node/attempt context recorded at decision time.
Statuses derivable from research-data/1:
  OBSERVED_FAIL_HIGH  (outcome 1: value >= beta, cutoff -> survival event)
  OBSERVED_FAIL_LOW   (outcome 2: exact negative event at the recorded depths)
  ABORTED_STOP        (outcome 3: single generic right-censored class; the
                       schema records no stop reason, so SEARCH_ABORTED vs
                       BUDGET_CENSORED are not separable)

Subcommands:
  collect   run corpus roots at one or more fixed depths with a fresh engine
            process per (depth, root) and full observational logging (rate 1.0
            by default), decode+validate every log, cross-check RUN_START and
            ROOT_START against the request (protocol P2.1 conventions), and
            emit per-root joined rows (attempts + decisions) as JSONL.
  report    aggregate the collected rows and write the calibration-baseline
            report (report.json + report.md): status distribution, cutoff
            rates and cost by recorded context (depth, ply, ordinal, node
            type, margin, re-search, TT-move success), and a
            behavior-policy-conditioned logistic/isotonic calibration of the
            recorded features to cutoff probability.

All results are *behavior-policy-conditioned*: they describe the engine's own
policy as observed at sampled nodes. They must not be treated as unbiased for
unsearched moves (plan.md section 8).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import datetime as _dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_corpus as rc
import decode_research_log as dlog

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# FEN helpers (offline reconstruction of per-move context)
# ---------------------------------------------------------------------------

_PIECES = "PNBRQKpnbrqk"


def _fen_board(fen: str) -> dict[int, str]:
    """Return {square_index: piece_char} from a FEN's placement field.

    square_index counts a1=0 .. h1=7, a2=8 ... h8=63 (rank-major).
    """
    board: dict[int, str] = {}
    rank = 7  # FEN lists rank 8 first
    file_ = 0
    for ch in fen.split()[0]:
        if ch == "/":
            rank -= 1
            file_ = 0
        elif ch.isdigit():
            file_ += int(ch)
        else:
            board[rank * 8 + file_] = ch
            file_ += 1
    return board


def _piece_at(fen: str, square: str) -> str:
    """Piece char (P/p...K/k) at an algebraic square, or '' if empty/off-board."""
    try:
        col = ord(square[0]) - ord("a")
        row = int(square[1]) - 1
    except (IndexError, ValueError):
        return ""
    if not (0 <= col < 8 and 0 <= row < 8):
        return ""
    return _fen_board(fen).get(row * 8 + col, "")


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------

def _derived(d: dict, a: dict) -> dict:
    """Join one DECISION_POINT + one MOVE_ATTEMPT into a modeling row.

    Numeric decision features are copied onto the attempt so each row is
    self-contained for fitting; the full decision FEN stays in the decisions
    table only.
    """
    beta = d["beta"]
    alpha = d["alpha"]
    st = d["static_eval"]
    row = {
        "root_key": a["root_key"],
        "node_serial": a["node_serial"],
        "attempt_serial": a["attempt_serial"],
        "move_uci": a["move_uci"],
        "move_type": dlog.MOVE_TYPE_NAMES.get(
            (a["move_raw"] >> 12) & 3, "?"),
        "from_sq": a["move_uci"][:2],
        "to_sq": a["move_uci"][2:4],
        "piece": _piece_at(d["fen"], a["move_uci"][:2]),
        "gives_check": a["gives_check"],
        "is_tt_move": a["is_tt_move"],
        "quiet_ordinal": a["quiet_ordinal"],
        "total_attempted": a["total_attempted"],
        "child_search_count": a["child_search_count"],
        "first_child_depth": a["first_child_depth"],
        "re_search_depth": a["research_depth"],
        "alpha_before": a["alpha_before"],
        "beta_before": a["beta_before"],
        "value_returned": a["value_returned"],
        "nodes_consumed": a["nodes_consumed"],
        "outcome": a["outcome"],
        "outcome_name": {1: "OBSERVED_FAIL_HIGH", 2: "OBSERVED_FAIL_LOW",
                         3: "ABORTED_STOP"}.get(a["outcome"], "?"),
        # decision context
        "key": d["key"],
        "ply": d["ply"],
        "depth": d["depth"],
        "root_iter_depth": d["root_iter_depth"],
        "alpha": alpha,
        "beta": beta,
        "static_eval": st,
        "improving": d["improving"],
        "tt_hit": d["tt_hit"],
        "tt_move_present": d["tt_move_present"],
        "rule50": d["rule50"],
        "side_to_move": d["side_to_move"],
        # derived windows
        "margin_beta": beta - st,       # beta - static_eval
        "margin_alpha": st - alpha,     # static_eval - alpha
        "window": beta - alpha,         # 1 by construction (NonPV null-window)
    }
    return row


def _split_records(records: list[dict]) -> tuple[dict, dict]:
    """Group decoded records into {root_key: {serial: decision}} and list of
    attempt dicts (already ordered)."""
    decisions: dict[dict] = {}
    attempts: list[dict] = []
    for r in records:
        t = r["_type"]
        if t == dlog.DECISION_POINT:
            decisions.setdefault(r["root_key"], {})[r["node_serial"]] = r
        elif t == dlog.MOVE_ATTEMPT:
            attempts.append(r)
    return decisions, attempts


def _rows_from_log(path: Path, position_id: str, position_set: str,
                   depth: int) -> tuple[list[dict], list[dict]]:
    """Decode one validated log into (attempt rows, decision rows).

    Decision rows keep the FEN (for reconstruction); attempt rows carry the
    numeric decision features inline (see _derived).
    """
    header, records = dlog.decode_file(path)
    stats = dlog.validate(records, path)
    if stats["overflow"]:
        raise RuntimeError(
            f"{path}: collection cap was hit during the run (ERROR_RECORD "
            f"present); a dataset file must not be truncated")
    decisions, attempts = _split_records(records)
    root_key = next(iter(stats["roots"]))
    ds = decisions[root_key]
    dec_rows = []
    for ser in sorted(ds):
        d = ds[ser]
        dec_rows.append({
            "position_id": position_id, "position_set": position_set,
            "root_depth": depth, "root_key": root_key, "node_serial": ser,
            "key": d["key"], "ply": d["ply"], "depth": d["depth"],
            "root_iter_depth": d["root_iter_depth"], "alpha": d["alpha"],
            "beta": d["beta"], "static_eval": d["static_eval"],
            "improving": d["improving"], "tt_hit": d["tt_hit"],
            "tt_move_present": d["tt_move_present"], "rule50": d["rule50"],
            "side_to_move": d["side_to_move"], "fen": d["fen"],
        })
    att_rows = []
    for a in attempts:
        d = ds[a["node_serial"]]
        row = _derived(d, a)
        row.update({"position_id": position_id, "position_set": position_set,
                    "root_depth": depth})
        att_rows.append(row)
    return att_rows, dec_rows


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True))
            fh.write("\n")


def collect(args) -> int:
    cwd = rc.repo_root()
    engine = Path(args.engine or (cwd / "src" / "stockfish")).expanduser().resolve()
    if not engine.is_file():
        raise SystemExit(f"engine not found: {engine}")
    corpus_path = Path(
        args.corpus
        or (cwd / "tools" / "policy_research" / "corpora" / "corpus-v1.json")
    ).resolve()
    if not corpus_path.is_file():
        raise SystemExit(f"corpus not found: {corpus_path}")
    corpus, canonical = rc.load_corpus(corpus_path)
    corpus_sha = rc.corpus_sha256(canonical)
    eval_file = rc._resolve_eval_file(engine, cwd, args.eval_file)
    only = set(args.only.split(",")) if args.only else None
    if only:
        known = {pos["id"] for pos in corpus["positions"]}
        unknown = only - known
        if unknown:
            raise SystemExit(f"--only ids not in corpus: {sorted(unknown)}")
    positions = [p for p in corpus["positions"] if (not only or p["id"] in only)]
    depths = sorted({int(x) for x in args.depths.split(",") if x.strip()})
    if not depths:
        raise SystemExit("--depths must be a non-empty comma list")
    excluded: dict[str, str] = {}
    for tok in (args.exclude_cells or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "@" not in tok:
            raise SystemExit(f"--exclude-cells entries must be 'id@depth': {tok}")
        pid, dep = tok.split("@", 1)
        excluded[f"{pid}@{int(dep)}"] = tok
    known = {pos["id"] for pos in corpus["positions"]}
    for pid in {t.split("@", 1)[0] for t in excluded}:
        if pid not in known:
            raise SystemExit(f"--exclude-cells id not in corpus: {pid}")

    if not (0.0 < args.rate <= 1.0):
        raise SystemExit("--rate must be in (0, 1]")
    if args.max_records <= 0:
        raise SystemExit("--max-records must be positive")
    # Fail-fast engine/option preflight (mirrors verify-research).
    names, spin_bounds = rc.engine_research_options_meta(engine, cwd)
    required = ["PolicyResearch", "PolicyResearchMode", "PolicyResearchSeed",
                "PolicyResearchSampleRate", "PolicyResearchMaxRecords",
                "PolicyResearchPolicyVersion", "PolicyResearchLogPath"]
    missing = [n for n in required if n not in names]
    if missing:
        raise SystemExit(
            "engine does not declare required research options "
            f"({', '.join(missing)}); build with 'make -C src research-build' "
            "or pass a research executable via --engine")
    for flag, opt, value in (
        ("seed", "PolicyResearchSeed", args.seed),
        ("max_records", "PolicyResearchMaxRecords", args.max_records),
    ):
        lo, hi = spin_bounds[opt]
        if not (lo <= value <= hi):
            raise SystemExit(
                f"--{flag}: value {value} is outside the engine's declared "
                f"range [{lo}, {hi}] for option '{opt}'")

    print(f"corpus: {corpus['corpus_id']} ({len(positions)} roots) "
          f"sha256={corpus_sha[:16]}... depths={depths} rate={args.rate}")
    print(f"engine: {engine}  hash={args.hash}MB  eval={eval_file or '<engine default>'}")

    out_root = Path(args.outdir or (cwd / "tools" / "policy_research" / "runs")).expanduser().resolve()
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    depths_tag = "d" + "-".join(str(d) for d in depths)
    label_dir = out_root / (
        f"{corpus['corpus_id']}-{depths_tag}-h{args.hash}-rate{args.rate}-"
        f"dataset-{stamp}")
    logs_dir = label_dir / "logs"
    rows_dir = label_dir / "rows"

    manifest = rc.make_manifest(
        engine=engine, cwd=cwd, corpus_path=corpus_path, corpus_sha=corpus_sha,
        corpus_id=corpus["corpus_id"], eval_file=eval_file, depth=max(depths),
        hash_mb=args.hash, build_command=args.build_command,
        extra_uci_options=[
            ("PolicyResearch", "on"),
            ("PolicyResearchMode", "observational"),
            ("PolicyResearchSeed", str(args.seed)),
            ("PolicyResearchSampleRate", str(args.rate)),
            ("PolicyResearchMaxRecords", str(args.max_records)),
            ("PolicyResearchPolicyVersion", args.policy_version),
        ],
    )
    manifest.update({
        "schema_version": "research-dataset/1",
        "phase": "3-observational",
        "depths": depths,
        "sample_rate": args.rate,
        "seed": args.seed,
        "max_records": args.max_records,
        "policy_version": args.policy_version,
        "research_data_schema": "research-data/1",
        "research_log_container": "research-log/1",
        "rows_schema": "research-dataset-row/1",
        "uci_options_applied": manifest.get("uci_options_applied", {}),
    })
    manifest["uci_options_applied"]["PolicyResearch"] = "on"
    manifest["uci_options_applied"]["PolicyResearchMode"] = "observational"
    manifest["uci_options_applied"]["PolicyResearchLogPath"] = "<per root per depth>"
    manifest["uci_options_applied"]["PolicyResearchPolicyVersion"] = args.policy_version

    banner = (manifest.get("engine_banner") or "").strip()
    if banner.startswith("id name "):
        banner = banner[len("id name "):]
    engine_version = banner.removesuffix(" by the Stockfish developers (see AUTHORS file)")
    expected_run_start = {
        "mode": 1,  # Research::Mode::Observational
        "seed": args.seed,
        "sample_threshold": int(args.rate * 1_000_000 + 0.5),
        "max_records": min(args.max_records, 1 << 22),
        "policy_version": args.policy_version,
    }

    static = [
        ("PolicyResearch", "on"),
        ("PolicyResearchMode", "observational"),
        ("PolicyResearchSeed", str(args.seed)),
        ("PolicyResearchSampleRate", str(args.rate)),
        ("PolicyResearchMaxRecords", str(args.max_records)),
        ("PolicyResearchPolicyVersion", args.policy_version),
    ]

    entries = []          # per (depth, root) decode summary
    total_attempts = 0
    total_decisions = 0
    problems = []
    t_start = time.perf_counter()
    for depth in depths:
        for pos in positions:
            if f"{pos['id']}@{depth}" in excluded:
                print(f"  EXCL {pos['id']:<12} d{depth} (see --exclude-cells)")
                continue
            log_path = logs_dir / f"d{depth}" / f"root-{pos['id']}.bin"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            extra = static + [("PolicyResearchLogPath", str(log_path))]
            try:
                result = rc.run_root(engine, cwd, pos["fen"], depth, args.hash,
                                     eval_file, args.timeout, extra)
            except (RuntimeError, TimeoutError) as exc:
                problems.append(f"{pos['id']} d{depth}: {exc}")
                print(f"  FAIL {pos['id']} d{depth}: {exc}", file=sys.stderr)
                continue
            try:
                s = rc._decode_and_validate_log(log_path, pos["fen"],
                                                expected_run_start, engine_version)
                att_rows, dec_rows = _rows_from_log(log_path, pos["id"], pos["set"], depth)
                _write_rows(rows_dir / f"d{depth}" / f"root-{pos['id']}.attempts.jsonl",
                            att_rows)
                _write_rows(rows_dir / f"d{depth}" / f"root-{pos['id']}.decisions.jsonl",
                            dec_rows)
            except (dlog.ValidationError, RuntimeError, OSError) as exc:
                problems.append(f"{pos['id']} d{depth}: decode/row build failed: {exc}")
                print(f"  FAIL {pos['id']} d{depth} decode: {exc}", file=sys.stderr)
                continue
            if len(att_rows) != s["attempts"] or len(dec_rows) != s["decisions"]:
                problems.append(
                    f"{pos['id']} d{depth}: row count mismatch "
                    f"attempts {len(att_rows)} vs decoded {s['attempts']}, "
                    f"decisions {len(dec_rows)} vs {s['decisions']}")
            total_attempts += len(att_rows)
            total_decisions += len(dec_rows)
            entries.append({
                "id": pos["id"], "set": pos["set"], "depth": depth,
                "decisions": len(dec_rows), "attempts": len(att_rows),
                "log": str(log_path.relative_to(label_dir)),
                "attempts_file": str((rows_dir / f"d{depth}" / f"root-{pos['id']}.attempts.jsonl").relative_to(label_dir)),
                "decisions_file": str((rows_dir / f"d{depth}" / f"root-{pos['id']}.decisions.jsonl").relative_to(label_dir)),
                "wall_ms": result["wall_ms"], "nodes": result["summary"].get("nodes"),
            })
            print(f"  {pos['id']:<12} d{depth:<3} decisions={len(dec_rows):<7} "
                  f"attempts={len(att_rows):<8} {result['wall_ms'] / 1000.0:5.1f}s")

    wall_s = time.perf_counter() - t_start
    manifest["dataset_dir"] = str(label_dir)
    manifest["files"] = entries
    manifest["totals"] = {
        "roots": len(entries), "decisions": total_decisions,
        "attempts": total_attempts,
        "wall_seconds": round(wall_s, 1),
    }
    if excluded:
        manifest["excluded_cells"] = [
            {"cell": c, "reason":
             "full-rate (root, depth) record volume exceeds the engine's "
             "per-run hard cap (4,194,304 records / 256 MiB); research-data/1 "
             "cannot hold the run in one file"} for c in excluded]
    manifest["problems"] = problems
    manifest["run_uuid"] = str(uuid.uuid4())
    manifest["timestamp_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat()

    label_dir.mkdir(parents=True, exist_ok=True)
    with open(label_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"\ntotal rows: decisions={total_decisions} attempts={total_attempts} "
          f"over {len(entries)} (depth, root) runs in {wall_s:.1f}s")
    if problems:
        print("problems:")
        for p in problems:
            print("  " + p)
        print("dataset INCOMPLETE (see manifest.json problems)")
        return 1
    print("dataset complete (no cap overflow, all logs decode + cross-check)")
    print(f"artifacts under {label_dir}")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

_STATUS = {1: "OBSERVED_FAIL_HIGH", 2: "OBSERVED_FAIL_LOW", 3: "ABORTED_STOP"}


def _md_table(df: pd.DataFrame) -> str:
    """Render a DataFrame as GitHub-flavored markdown (no tabulate dep)."""
    cols = list(df.columns)
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (int, np.integer)):
                cells.append(f"{int(v):,}")
            elif isinstance(v, (float, np.floating)):
                if np.isnan(v):
                    cells.append("")
                else:
                    cells.append(f"{float(v):.6g}")
            else:
                cells.append(str(v))
        rows.append("| " + " | ".join(cells) + " |")
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    return "\n".join([head, sep] + rows)


def _load_attempts(dataset_dir: Path, only_depths: str | None = None):
    """Load every attempts JSONL under a dataset dir into one DataFrame."""
    rows_dir = dataset_dir / "rows"
    files = sorted(rows_dir.rglob("*.attempts.jsonl"))
    if only_depths:
        keep = {int(x) for x in only_depths.split(",")}
        files = [f for f in files if int(f.parent.name[1:]) in keep]
    if not files:
        raise SystemExit(f"no attempts rows found under {rows_dir}")
    parts = []
    for f in files:
        parts.append(pd.read_json(f, lines=True))
    df = pd.concat(parts, ignore_index=True)
    # Schema guards on decoded inputs.
    assert (df["window"] == 1).all(), "non-null-window rows present (schema violation)"
    return df


def _bucket(series: pd.Series, edges: list[int]) -> pd.Series:
    labels = []
    for i, e in enumerate(edges):
        if i + 1 < len(edges):
            labels.append(f"[{e},{edges[i + 1]})" if edges[i + 1] != np.inf
                          else f">={e}")
    return pd.cut(series, bins=edges, right=False, labels=labels)


def _status_table(df: pd.DataFrame) -> pd.DataFrame:
    tab = df.groupby("outcome_name", observed=True).agg(
        rows=("outcome_name", "size"),
        nodes_total=("nodes_consumed", "sum"),
        nodes_mean=("nodes_consumed", "mean"),
        nodes_median=("nodes_consumed", "median"),
    ).reset_index()
    tab["share"] = tab["rows"] / tab["rows"].sum()
    return tab


def _mean_ci95(x: np.ndarray) -> tuple[float, float]:
    """Normal-approximation 95% CI for the mean (cheap at these sizes)."""
    m = float(x.mean())
    if len(x) < 2:
        return m, m
    se = float(x.std(ddof=1) / np.sqrt(len(x)))
    return m - 1.96 * se, m + 1.96 * se


def _cost_by_bucket(df: pd.DataFrame, col: str, edges: list[int]) -> pd.DataFrame:
    """Mean nodes consumed (with CI) and rows per bucket, for fail-low rows."""
    x = df[df["outcome"] == 2]
    out = []
    for name, grp in x.groupby(_bucket(x[col], edges), observed=True):
        vals = grp["nodes_consumed"].to_numpy(dtype=np.float64)
        lo, hi = _mean_ci95(vals)
        out.append({"bucket": str(name), "rows": int(len(vals)),
                    "mean_nodes": float(vals.mean()), "ci95_low": lo,
                    "ci95_high": hi, "median_nodes": float(np.median(vals))})
    return pd.DataFrame(out)


def _cutoff_by_bucket(df: pd.DataFrame, col: str, edges: list[int],
                      include_censored: bool = False) -> pd.DataFrame:
    """Cutoff (fail-high) rate among completed attempts per bucket."""
    sub = df if include_censored else df[df["outcome"] != 3]
    out = []
    for name, grp in sub.groupby(_bucket(sub[col], edges), observed=True):
        n = int(len(grp))
        c = int((grp["outcome"] == 1).sum())
        p = c / n if n else float("nan")
        se = (p * (1 - p) / n) ** 0.5 if n else float("nan")
        out.append({"bucket": str(name), "rows": n, "cutoffs": c,
                    "cutoff_rate": p, "se": se})
    return pd.DataFrame(out)


def _ordinal_survival(df: pd.DataFrame, max_k: int = 12) -> pd.DataFrame:
    """P(cutoff | the attempt is at quiet ordinal k) with a raw cutoff-rate and
    its SE; restricted to completed attempts."""
    sub = df[(df["outcome"] != 3) & (df["quiet_ordinal"] <= max_k)]
    out = []
    for k, grp in sub.groupby("quiet_ordinal", observed=True):
        n = int(len(grp))
        c = int((grp["outcome"] == 1).sum())
        p = c / n if n else float("nan")
        se = (p * (1 - p) / n) ** 0.5 if n else float("nan")
        out.append({"quiet_ordinal": int(k), "rows": n, "cutoffs": c,
                    "cutoff_rate": p, "se": se})
    return pd.DataFrame(out)


# --- calibration helpers (numpy only, no sklearn) -------------------------

def _logistic_irls(X: np.ndarray, y: np.ndarray, iters: int = 40,
                   reg: float = 1e-4) -> np.ndarray:
    """Ridge-regularized IRLS logistic regression (intercept appended)."""
    Xb = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        eta = Xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        s = p * (1 - p)
        H = Xb.T @ (Xb * s[:, None]) + reg * np.eye(Xb.shape[1])
        g = Xb.T @ (y - p) - reg * w
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        w += step
        if np.max(np.abs(step)) < 1e-7:
            break
    return w


def _pav_monotone(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Nondecreasing isotonic regression over weighted means (small arrays).

    Classic PAV: block means that violate monotonicity are pooled. Returns the
    fitted (calibrated) value for each input position.
    """
    vals = y.astype(np.float64).copy()
    wt = w.astype(np.float64).copy()
    n = len(vals)
    out = np.empty(n)
    if n == 0:
        return out
    starts = list(range(n))
    bv = vals.copy()
    bw = wt.copy()
    while True:
        merged = False
        new_s, new_bv, new_bw = [starts[0]], [bv[0]], [bw[0]]
        for i in range(1, len(starts)):
            # means of last block and current block
            m_prev = new_bv[-1] / new_bw[-1]
            m_cur = bv[i] / bw[i]
            if m_prev <= m_cur:
                new_s.append(starts[i]); new_bv.append(bv[i]); new_bw.append(bw[i])
            else:
                # pool current into previous block
                new_bv[-1] += bv[i]
                new_bw[-1] += bw[i]
                merged = True
        starts, bv, bw = new_s, new_bv, new_bw
        if not merged:
            break
    # expand block means back to positions (blocks are contiguous runs)
    pos = 0
    for blk in range(len(bv)):
        # block b covers positions from starts[blk] to start of next block
        end = starts[blk + 1] if blk + 1 < len(starts) else n
        m = bv[blk] / bw[blk]
        out[starts[blk]: end] = m
    return out


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((y - p) ** 2))


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    """Mann-Whitney U AUC on predicted probabilities."""
    order = np.argsort(p, kind="mergesort")
    r = np.arange(1, len(p) + 1, dtype=np.float64)
    # average ranks inside ties
    ranks = np.empty(len(p), dtype=np.float64)
    i = 0
    while i < len(p):
        j = i + 1
        while j < len(p) and p[order[j]] == p[order[i]]:
            j += 1
        ranks[order[i:j]] = r[i:j].mean()
        i = j
    y = np.asarray(y)
    pos_sum = ranks[y == 1].sum()
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((pos_sum - n1 * (n1 + 1) / 2) / (n1 * n0))


def _ece_table(y: np.ndarray, p: np.ndarray, bins: int = 10
               ) -> tuple[float, list[dict]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    table = []
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if n == 0:
            continue
        acc = float(y[m].mean())
        conf = float(p[m].mean())
        ece += (n / len(y)) * abs(acc - conf)
        table.append({"bin": f"[{edges[b]:.1f},{edges[b + 1]:.1f})",
                      "rows": n, "accuracy": acc, "confidence": conf})
    return float(ece), table


def _fit_and_evaluate(df: pd.DataFrame, feature_cols: list[str],
                      seed: int = 7) -> dict:
    """Behavior-policy-conditioned cutoff calibration on completed attempts.

    70/30 split; ridge logistic on the recorded features; then isotonic
    (PAV, nondecreasing) calibration fit on binned training predictions and
    applied to the held-out test predictions.
    """
    sub = df[df["outcome"] != 3].copy()
    y = (sub["outcome"] == 1).to_numpy(dtype=np.float64)
    X = sub[feature_cols].to_numpy(dtype=np.float64)
    n = len(y)
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    cut = int(n * 0.7)
    tr_i, te_i = perm[:cut], perm[cut:]

    w = _logistic_irls(X[tr_i], y[tr_i])
    def predict(Xk: np.ndarray) -> np.ndarray:
        Xb = np.column_stack([np.ones(len(Xk)), Xk])
        return 1.0 / (1.0 + np.exp(-np.clip(Xb @ w, -30, 30)))
    p_tr = predict(X[tr_i])
    p_te = predict(X[te_i])

    # --- isotonic on binned training predictions (<= N_BIN quantile bins) ---
    n_bin = min(100, len(tr_i))
    q_edges = np.quantile(p_tr, np.linspace(0, 1, n_bin + 1))
    q_edges = np.unique(q_edges)
    if len(q_edges) < 2:
        q_edges = np.array([0.0, 1.0])
    b_tr = np.clip(np.digitize(p_tr, q_edges[1:-1]), 0, len(q_edges) - 2)
    b_te = np.clip(np.digitize(p_te, q_edges[1:-1]), 0, len(q_edges) - 2)
    n_b = len(q_edges) - 1
    bin_acc = np.array([y[tr_i][b_tr == b].mean() if (b_tr == b).any() else np.nan
                        for b in range(n_b)])
    bin_cnt = np.array([int((b_tr == b).sum()) for b in range(n_b)])
    # Isotonic fit over non-empty bins only; empty bins (quantile ties leave
    # gaps) are filled by index-interpolation of the fitted values.
    nb = np.flatnonzero(bin_cnt > 0)
    if len(nb) == 0:
        cal = np.full(len(te_i), float(y[tr_i].mean()))
    elif len(nb) == 1:
        cal = np.full(len(te_i), bin_acc[nb[0]])
    else:
        # PAV over weighted observations: pass block sums (acc*count) with the
        # counts as weights so block means are acc-weighted averages.
        fitted = _pav_monotone(bin_acc[nb] * bin_cnt[nb],
                               bin_cnt[nb].astype(np.float64))
        iso_full = np.interp(np.arange(n_b), nb.astype(np.float64),
                             fitted)
        cal = iso_full[b_te]

    return {
        "n_train": int(len(tr_i)), "n_test": int(len(te_i)),
        "base_rate_test": float(y[te_i].mean()),
        "logistic_brier": _brier(y[te_i], p_te),
        "logistic_logloss": _log_loss(y[te_i], p_te),
        "logistic_auc": _auc(y[te_i], p_te),
        "logistic_ece": _ece_table(y[te_i], p_te)[0],
        "isotonic_brier": _brier(y[te_i], cal),
        "isotonic_logloss": _log_loss(y[te_i], cal),
        "isotonic_auc": _auc(y[te_i], cal),
        "isotonic_ece": _ece_table(y[te_i], cal)[0],
        "logistic_reliability": _ece_table(y[te_i], p_te)[1],
    }


def report(args) -> int:
    dataset_dir = Path(args.dataset).expanduser().resolve()
    if not dataset_dir.is_dir():
        raise SystemExit(f"dataset dir not found: {dataset_dir}")
    df = _load_attempts(dataset_dir, args.only_depths)
    n = len(df)
    if n == 0:
        raise SystemExit("no attempt rows loaded")
    total_nodes = int(df["nodes_consumed"].sum())

    out: dict = {
        "schema_version": "research-baseline-report/1",
        "dataset_dir": str(dataset_dir),
        "note": ("behavior-policy-conditioned: describes the engine's own "
                 "observational policy at sampled nodes; not unbiased for "
                 "unsearched moves (plan.md section 8)"),
        "rows": n,
        "nodes_consumed_total": total_nodes,
    }
    md = []
    md.append(f"# Phase 3 calibration baseline report\n")
    md.append(f"Dataset: `{dataset_dir}`  \n"
              f"Rows (searched quiet moves at sampled nodes): {n}  \n"
              f"Total nodes consumed by these attempts: {total_nodes}\n")

    # -- status table
    tab = _status_table(df)
    md.append("\n## Status distribution\n\n"
              + _md_table(tab) + "\n")
    out["status_distribution"] = tab.to_dict(orient="records")
    censored = int((df["outcome"] == 3).sum())
    out["censored_aborted"] = censored

    # -- cutoff rates / survival by ordinal
    ord_tab = _ordinal_survival(df)
    md.append("\n## Cutoff rate by quiet ordinal (completed attempts)\n\n"
              + _md_table(ord_tab) + "\n")
    out["ordinal_cutoff"] = ord_tab.to_dict(orient="records")

    # -- cost by context (fail-low rows, exact negative events)
    cost_cols = [
        ("cost_by_remaining_depth", "depth",
         [1, 3, 5, 7, 9, 11, 13, np.inf]),
        ("cost_by_ply", "ply", [0, 5, 10, 15, 20, 25, 30, 40, np.inf]),
        ("cost_by_margin_beta", "margin_beta",
         list(range(-1200, 1201, 300)) + [np.inf]),
    ]
    for key, col, edges in cost_cols:
        t = _cost_by_bucket(df, col, edges)
        md.append(f"\n## Mean nodes consumed (fail-low attempts) by {col}\n\n"
                  + _md_table(t) + "\n")
        out[key] = t.to_dict(orient="records")

    # -- cutoff rate by context
    cut_cols = [
        ("cutoff_by_remaining_depth", "depth", [1, 3, 5, 7, 9, 11, 13, np.inf]),
        ("cutoff_by_ply", "ply", [0, 5, 10, 15, 20, 25, 30, 40, np.inf]),
        ("cutoff_by_margin_beta", "margin_beta",
         list(range(-1200, 1201, 300)) + [np.inf]),
    ]
    for key, col, edges in cut_cols:
        t = _cutoff_by_bucket(df, col, edges)
        md.append(f"\n## Cutoff rate by {col}\n\n"
                  + _md_table(t) + "\n")
        out[key] = t.to_dict(orient="records")

    # -- node type flags
    flags = ["improving", "tt_hit", "tt_move_present", "gives_check", "is_tt_move"]
    flag_rows = []
    for f in flags:
        sub = df[df["outcome"] != 3]
        for v, label in [(True, f), (False, f"not_{f}")]:
            g = sub[sub[f] == v]
            if len(g) == 0:
                continue
            flag_rows.append({
                "flag": label, "rows": len(g),
                "cutoff_rate": float((g["outcome"] == 1).mean()),
                "mean_nodes_fl": float(g.loc[g["outcome"] == 2, "nodes_consumed"].mean())
                if (g["outcome"] == 2).any() else float("nan"),
            })
    flag_tab = pd.DataFrame(flag_rows)
    md.append("\n## Cutoff rate / fail-low cost by node-type flags\n\n"
              + _md_table(flag_tab) + "\n")
    out["flag_cutoff"] = flag_tab.to_dict(orient="records")

    # -- re-search rate and TT-move success
    rs = df[(df["child_search_count"] == 2) & (df["outcome"] != 3)]
    tt = df[(df["is_tt_move"]) & (df["outcome"] != 3)]
    extra = {
        "research_rate": float((df["child_search_count"] == 2).mean()),
        "research_rate_rows": int((df["child_search_count"] == 2).sum()),
        "tt_move_cutoff_rate": float((tt["outcome"] == 1).mean()) if len(tt) else float("nan"),
        "tt_move_rows": int(len(tt)),
        "non_tt_move_cutoff_rate": float(
            (df.loc[(~df["is_tt_move"]) & (df["outcome"] != 3), "outcome"] == 1).mean()),
        "re_search_mean_nodes_fl": float(rs.loc[rs["outcome"] == 2, "nodes_consumed"].mean())
        if (rs["outcome"] == 2).any() else float("nan"),
    }
    md.append("\n## Re-search and TT-move\n\n```\n"
              + json.dumps(extra, indent=2) + "\n```\n")
    out["research_tt"] = extra

    # -- behavior-policy-conditioned cutoff calibration
    feature_cols = ["quiet_ordinal", "total_attempted", "depth", "ply",
                    "root_iter_depth", "improving", "tt_hit", "tt_move_present",
                    "gives_check", "is_tt_move", "margin_beta", "margin_alpha"]
    md.append("\n## Behavior-policy-conditioned cutoff calibration "
              "(completed attempts, outcome 1 vs 2)\n\n"
              "Train/test split 70/30; logistic (ridge) on the recorded "
              "features, then PAV-isotonic on the logistic output.\n\n")
    cal = _fit_and_evaluate(df, feature_cols)
    md.append("```\n" + json.dumps({k: v for k, v in cal.items()
                                    if k != "logistic_reliability"},
                                   indent=2) + "\n```\n")
    md.append("### Logistic reliability table (test set)\n\n"
              + _md_table(pd.DataFrame(cal["logistic_reliability"]))
              + "\n")
    md.append("\nFeatures: `" + ", ".join(feature_cols) + "`\n")
    out["calibration"] = cal
    out["features"] = feature_cols

    out["censoring_note"] = ("ABORTED_STOP rows carry no value semantics; they "
                             "are excluded from cutoff-rate and calibration "
                             "denominators (kept in cost/total tables).")
    with open(dataset_dir / "baseline-report.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(dataset_dir / "baseline-report.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    print("\n".join(md))
    print(f"\nreport written to {dataset_dir / 'baseline-report.md'}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="collect the Phase 3 observational dataset")
    c.add_argument("--engine", default=None)
    c.add_argument("--corpus", default=None)
    c.add_argument("--depths", default="14,17,20",
                   help="comma-separated fixed depths per corpus root")
    c.add_argument("--hash", type=int, default=16)
    c.add_argument("--outdir", default=None)
    c.add_argument("--eval-file", default=None)
    c.add_argument("--only", default=None)
    c.add_argument("--exclude-cells", default="",
                   help="comma list of 'id@depth' cells to skip (e.g. "
                        "c1-d-004@20 when a root's full-rate volume exceeds "
                        "the engine's per-run hard cap); recorded in the "
                        "manifest")
    c.add_argument("--timeout", type=float, default=1800.0)
    c.add_argument("--build-command", default=None)
    c.add_argument("--rate", type=float, default=1.0,
                   help="PolicyResearchSampleRate (1.0 = sample every eligible node)")
    c.add_argument("--seed", type=int, default=101)
    c.add_argument("--max-records", type=int, default=2147483647)
    c.add_argument("--policy-version", default="baseline-observational-v1")
    c.set_defaults(func=collect)

    r = sub.add_parser("report", help="aggregate a dataset dir into the baseline report")
    r.add_argument("dataset")
    r.add_argument("--only-depths", default=None)
    r.set_defaults(func=report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
