#!/usr/bin/env python3
"""Phase 3 dataset collection and calibration-baseline reporting.

One analysis row per *searched quiet move* in a sampled eligible node
(DECISION_POINT + its MOVE_ATTEMPTs) of the committed research-data/1 schema,
behavior-conditioned on the node/attempt context recorded at decision time.
Statuses derivable from research-data/1:
  OBSERVED_FAIL_HIGH  (outcome 1: value >= beta, cutoff)
  OBSERVED_FAIL_LOW   (outcome 2: exact negative event at the recorded depths)
  ABORTED_STOP        (outcome 3: single generic right-censored class; the
                       schema records no stop reason, so SEARCH_ABORTED vs
                       BUDGET_CENSORED are not separable)

Subcommands:
  collect   run corpus roots at a fixed depth with a fresh engine process per
            (depth, root) and uniform-rate observational logging, decode +
            validate every log, cross-check RUN_START and ROOT_START against
            the request (protocol P2.1 conventions), and emit per-root joined
            rows (attempts + decisions) as JSONL. Each row carries
            ``node_weight`` = 1 / sample_rate so non-uniform rate designs can
            be aggregated without bias.

            Dataset protocol P3.2 is *prefix-free and uniform*: one run per
            (depth, root) at one sample rate, with no repeated iterative-
            deepening prefixes (a deeper target run replays earlier
            iterations byte-identically, so re-collecting shallow targets
            would duplicate rows ~3x; the tool warns when more than one
            target depth is requested for the same root).

  report    aggregate the collected rows and write the calibration-baseline
            report (report.json + report.md): status distribution, cutoff
            rates and cost by recorded context (depth, ply, ordinal, node
            type, margin, re-search, TT-move success) with between-root macro
            columns, a decision-joined node-level section (quiet-cutoff
            structure, wasted cost before a quiet cutoff), and a
            behavior-policy-conditioned logistic/isotonic calibration of the
            recorded features to cutoff probability evaluated on held-out
            corpus test roots.

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
                   depth: int, node_weight: float) -> tuple[list[dict], list[dict]]:
    """Decode one validated log into (attempt rows, decision rows).

    Decision rows keep the FEN (for reconstruction); attempt rows carry the
    numeric decision features inline (see _derived).  Each row records
    ``node_weight`` = 1/sample_rate (the decision node's inclusion weight).
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
            "node_weight": node_weight,
        })
    att_rows = []
    for a in attempts:
        d = ds[a["node_serial"]]
        row = _derived(d, a)
        row.update({"position_id": position_id, "position_set": position_set,
                    "root_depth": depth, "node_weight": node_weight})
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
    if len(depths) > 1:
        print("warning: multiple target depths for the same root re-collect "
              "byte-identical iterative-deepening prefixes (a deeper run "
              "replays shallower iterations); protocol P3.2 recommends one "
              "target depth per root and the report assumes prefix-free rows",
              file=sys.stderr)

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
        "collection_protocol": (
            "P3.2 prefix-free uniform-rate: one fresh-process run per "
            "(depth, root) at one node-sample rate; iterative-deepening "
            "prefixes are not re-collected, so rows are prefix-free (no "
            "duplicated lower-depth traces) by construction; every row "
            "carries node_weight = 1/sample_rate"),
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
                att_rows, dec_rows = _rows_from_log(
                    log_path, pos["id"], pos["set"], depth,
                    1.0 / args.rate)
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


# ---------------------------------------------------------------------------
# Aggregation helpers (root-aware)
# ---------------------------------------------------------------------------

NODE_KEY = ["position_id", "root_depth", "node_serial"]


def _macro_fields(vals) -> dict:
    """Between-root summary of a per-root statistic (roots are the sample
    unit for uncertainty; row counts are not independent observations)."""
    v = np.asarray(list(vals), dtype=np.float64)
    if v.size == 0:
        return {"n_roots": 0, "macro_mean": float("nan"),
                "macro_sd": float("nan"), "macro_min": float("nan"),
                "macro_max": float("nan")}
    sd = float(np.std(v, ddof=1)) if v.size > 1 else 0.0
    return {"n_roots": int(v.size), "macro_mean": float(np.mean(v)),
            "macro_sd": sd, "macro_min": float(np.min(v)),
            "macro_max": float(np.max(v))}


def _macro_rate_table(frame, bucket, event, group=None):
    """Pooled event rate plus between-root macro columns, per bucket.

    Bucket/event/group are index-aligned series over ``frame`` (group
    defaults to the position_id column). Returns a DataFrame ordered by the
    bucket categories (may skip empty buckets).
    """
    b = pd.Series(pd.Categorical(bucket))
    ev = (event.astype(bool)).to_numpy()
    gr = np.asarray(frame["position_id"] if group is None else group)
    out = []
    for label in b.cat.categories:
        m = (b == label).to_numpy(dtype=bool)
        n = int(m.sum())
        if n == 0:
            continue
        e = ev[m].astype(np.int64)
        g = gr[m]
        pooled = float(e.sum()) / n
        per = pd.DataFrame({"g": g, "e": e}).groupby("g")["e"].agg(
            ["sum", "count"])
        rate = (per["sum"] / per["count"]).to_numpy(dtype=np.float64)
        rec = {"bucket": str(label), "rows": n, "events": int(e.sum()),
               "event_rate": pooled, **_macro_fields(rate)}
        out.append(rec)
    return pd.DataFrame(out)


def _macro_quant_table(frame, bucket, value, group=None,
                       quantiles=(0.5, 0.9, 0.99)):
    """Pooled quantiles/mean plus macro (between-root) mean, per bucket."""
    b = pd.Series(pd.Categorical(bucket))
    vals_all = np.asarray(value, dtype=np.float64)
    gr_all = np.asarray(frame["position_id"] if group is None else group)
    out = []
    for label in b.cat.categories:
        m = (b == label).to_numpy(dtype=bool)
        n = int(m.sum())
        if n == 0:
            continue
        v = vals_all[m]
        q = np.quantile(v, quantiles)
        per = pd.DataFrame({"g": gr_all[m], "v": v}).groupby("g")["v"].mean()
        rec = {"bucket": str(label), "rows": n,
               "mean": float(np.mean(v)), "median": float(np.quantile(v, 0.5)),
               "p90": float(q[1]), "p99": float(q[2]),
               **_macro_fields(per.to_numpy(dtype=np.float64))}
        out.append(rec)
    return pd.DataFrame(out)


# --- calibration helpers (numpy only, no sklearn) -------------------------

def _logistic_irls_std(Z: np.ndarray, y: np.ndarray, reg: float = 1e-3,
                       max_iter: int = 80, tol: float = 1e-9):
    """Ridge-regularized IRLS logistic regression on *standardized* features
    (Z holds no intercept column). The intercept is added and is NOT ridge
    penalized (regularization applies to feature coefficients only)."""
    Xb = np.column_stack([np.ones(len(Z)), Z])
    pen = np.zeros(Xb.shape[1])
    pen[1:] = reg
    w = np.zeros(Xb.shape[1])
    iters = 0
    converged = False
    for it in range(1, max_iter + 1):
        iters = it
        eta = Xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        s = p * (1 - p)
        H = (Xb * s[:, None]).T @ Xb + np.diag(pen)
        g = Xb.T @ (y - p) - pen * w
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        w = w + step
        if np.max(np.abs(step)) < tol:
            converged = True
            break
    return w, iters, converged


def _predict_std(Z, mu, sd, w) -> np.ndarray:
    Zs = (Z - mu) / sd
    Xb = np.column_stack([np.ones(len(Zs)), Zs])
    return 1.0 / (1.0 + np.exp(-np.clip(Xb @ w, -30, 30)))


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
    pos = 0
    for blk in range(len(bv)):
        end = starts[blk + 1] if blk + 1 < len(starts) else n
        m = bv[blk] / bw[blk]
        out[starts[blk]: end] = m
    return out


def _fit_isotonic_map(p_cal: np.ndarray, y_cal: np.ndarray,
                      n_bin: int = 100):
    """Quantile-bin PAV calibration fitted on calibration predictions.

    Returns (edges, cal_bins): ``cal_bins[b]`` is the calibrated probability
    for a prediction that falls in bin b of ``edges`` (len(edges)-1 bins).
    Empty interior bins (quantile ties) are filled by index-interpolation
    over the fitted non-empty bins.
    """
    edges = np.unique(np.quantile(p_cal, np.linspace(0, 1, n_bin + 1)))
    if len(edges) < 2:
        edges = np.array([0.0, 1.0])
    b = np.clip(np.digitize(p_cal, edges[1:-1]), 0, len(edges) - 2)
    n_b = len(edges) - 1
    acc = np.full(n_b, np.nan)
    cnt = np.zeros(n_b, dtype=np.int64)
    for k in range(n_b):
        m = b == k
        cnt[k] = int(m.sum())
        if cnt[k]:
            acc[k] = float(y_cal[m].mean())
    nb = np.flatnonzero(cnt > 0)
    cal = np.empty(n_b)
    if len(nb) == 0:
        cal[:] = float(y_cal.mean())
    elif len(nb) == 1:
        cal[:] = acc[nb[0]]
    else:
        fit = _pav_monotone(acc[nb] * cnt[nb], cnt[nb].astype(np.float64))
        cal = np.interp(np.arange(n_b), nb.astype(np.float64), fit)
    return edges, cal


def _apply_isotonic(p_eval: np.ndarray, edges: np.ndarray,
                    cal_bins: np.ndarray) -> np.ndarray:
    b = np.clip(np.digitize(p_eval, edges[1:-1]), 0, len(edges) - 2)
    return cal_bins[b]


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((y - p) ** 2))


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    """Mann-Whitney U AUC on predicted probabilities."""
    order = np.argsort(p, kind="mergesort")
    r = np.arange(1, len(p) + 1, dtype=np.float64)
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


def _metrics(y: np.ndarray, p: np.ndarray) -> dict:
    return {"n": int(len(y)), "base_rate": float(np.mean(y)),
            "auc": _auc(y, p), "brier": _brier(y, p),
            "logloss": _log_loss(y, p), "ece10": _ece_table(y, p)[0]}


def _metrics_block(y: np.ndarray, p: np.ndarray,
                   roots: np.ndarray) -> dict:
    """Pooled metrics plus macro (between-root) metrics over root groups."""
    pooled = _metrics(y, p)
    per = []
    for r in np.unique(roots):
        m = roots == r
        per.append({"root": str(r), **_metrics(y[m], p[m])})
    macro = {}
    for k in ("base_rate", "auc", "brier", "logloss", "ece10"):
        vals = np.array([x[k] for x in per], dtype=np.float64)
        macro[k] = _macro_fields(vals)
    return {"pooled": pooled, "macro": macro, "per_root": per}


# ---------------------------------------------------------------------------
# Node-level aggregation (join attempts onto their decision nodes)
# ---------------------------------------------------------------------------

def _load_decisions(dataset_dir: Path, only_depths: str | None = None):
    rows_dir = dataset_dir / "rows"
    files = sorted(rows_dir.rglob("*.decisions.jsonl"))
    if only_depths:
        keep = {int(x) for x in only_depths.split(",")}
        files = [f for f in files if int(f.parent.name[1:]) in keep]
    if not files:
        raise SystemExit(f"no decision rows found under {rows_dir}")
    parts = [pd.read_json(f, lines=True) for f in files]
    df = pd.concat(parts, ignore_index=True)
    df["margin_beta"] = df["beta"] - df["static_eval"]
    df["margin_alpha"] = df["static_eval"] - df["alpha"]
    return df


def _node_frame(att: pd.DataFrame, dec: pd.DataFrame) -> pd.DataFrame:
    """One row per sampled decision node (attempt summaries joined on).

    Adds the node-level event structure used by the report:
      n_quiet             searched quiet attempts recorded at the node
      quiet_cost          sum of their local attempt subtree counts
      n_fh                quiet fail-high count (0 or 1 by construction)
      fh_ord              quiet ordinal of the fail-high, else -1
      wasted_before_cut   sum of fail-low attempt costs preceding the cutoff
                          (NaN when the node had no quiet cutoff)
      no_cut_quiet_cost   full quiet-loop cost when no quiet move cut off
                          (NaN when a quiet move cut off)
    """
    f = att.assign(
        _fh=(att["outcome"] == 1).astype(np.int8),
        _fh_ord=np.where(att["outcome"] == 1, att["quiet_ordinal"], -1),
        _fh_cost=np.where(att["outcome"] == 1, att["nodes_consumed"], 0.0),
    )
    summ = f.groupby(NODE_KEY, sort=False).agg(
        n_quiet=("nodes_consumed", "size"),
        quiet_cost=("nodes_consumed", "sum"),
        n_fh=("_fh", "sum"),
        fh_ord=("_fh_ord", "max"),
        max_ord=("quiet_ordinal", "max"),
        fh_cost=("_fh_cost", "sum"),
    ).reset_index()
    bad = summ[summ["n_fh"] > 1]
    if len(bad):
        raise RuntimeError(
            f"{len(bad)} sampled nodes have >1 quiet fail-high "
            "(schema invariant violated: a cutoff ends the moves loop)")
    late = summ[(summ["n_fh"] == 1) & (summ["fh_ord"] < summ["max_ord"])]
    if len(late):
        raise RuntimeError(
            f"{len(late)} sampled nodes recorded quiet moves after their "
            "fail-high (schema invariant violated)")
    node = dec.merge(summ, on=NODE_KEY, how="left")
    node["n_quiet"] = node["n_quiet"].fillna(0).astype(np.int64)
    node["fh_ord"] = node["fh_ord"].fillna(-1).astype(np.int64)
    node["wasted_before_cut"] = np.where(
        node["n_fh"] > 0, node["quiet_cost"] - node["fh_cost"], np.nan)
    node["no_cut_quiet_cost"] = np.where(
        node["n_fh"] == 0, node["quiet_cost"], np.nan)
    return node


# --- report tables (markdown helpers) -------------------------------------

def _records(df: pd.DataFrame) -> list[dict]:
    return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# Grouped (root-held-out) cutoff calibration
# ---------------------------------------------------------------------------

# Feature groups.  margin_alpha is deliberately excluded: beta-alpha == 1 at
# these null-window nodes, so margin_alpha == 1 - margin_beta exactly, and the
# two margins are collinear with the intercept.
_FEATURE_GROUPS = {
    "ordinal": ["quiet_ordinal", "total_attempted"],
    "tt": ["is_tt_move", "gives_check"],
    "context": ["depth", "ply", "root_iter_depth", "improving", "tt_hit",
                "tt_move_present", "margin_beta"],
}
_FEATURE_ORDER = ["ordinal", "tt", "context"]


def _grouped_calibration(df: pd.DataFrame) -> dict:
    """Cutoff calibration with root-position-held-out evaluation.

    Protocol: logistic is fit on the corpus development-set roots, isotonic
    (PAV over quantile bins) is fit on the validation-set roots, and all
    reported metrics are computed only on the test-set roots.  Roots are the
    sample unit; per-test-root macro columns are reported alongside pooled
    numbers.
    """
    comp = df[df["outcome"] != 3]
    y_all = (comp["outcome"] == 1).to_numpy(dtype=np.float64)
    root_all = comp["position_id"].to_numpy(dtype=object)
    set_all = comp["position_set"].to_numpy(dtype=object)

    def _split(name: str):
        m = set_all == name
        return comp.iloc[np.flatnonzero(m)], y_all[m], root_all[m]

    dev, y_dev, _ = _split("development")
    val, y_val, _ = _split("validation")
    test, y_test, root_test = _split("test")
    for nm, (fr, yy, _) in (("development", (dev, y_dev, None)),
                            ("validation", (val, y_val, None)),
                            ("test", (test, y_test, None))):
        if len(fr) == 0:
            raise RuntimeError(
                f"grouped calibration requires non-empty '{nm}' root set "
                "(corpus position_set rows missing)")

    all_feats = [f for g in _FEATURE_ORDER for f in _FEATURE_GROUPS[g]]
    models: dict[str, dict] = {}
    full_w = full_meta = None
    for name, feats in [("full", all_feats)] + \
                       [(g, _FEATURE_GROUPS[g]) for g in _FEATURE_ORDER]:
        X_dev = dev[feats].to_numpy(dtype=np.float64)
        mu = X_dev.mean(axis=0)
        sd = X_dev.std(axis=0)
        sd[sd == 0] = 1.0
        w, iters, conv = _logistic_irls_std((X_dev - mu) / sd, y_dev)
        p_val = _predict_std(val[feats].to_numpy(dtype=np.float64), mu, sd, w)
        p_test = _predict_std(test[feats].to_numpy(dtype=np.float64), mu, sd, w)
        models[name] = {
            "features": feats,
            "test": _metrics_block(y_test, p_test, root_test),
            "converged": bool(conv), "iterations": int(iters),
        }
        if name == "full":
            full_w, full_meta = w, (mu, sd, feats)

    # Isotonic calibration: fit on validation-root logistic predictions only,
    # evaluate on test-root rows.
    mu, sd, feats = full_meta
    X_val = val[feats].to_numpy(dtype=np.float64)
    X_test = test[feats].to_numpy(dtype=np.float64)
    p_val = _predict_std(X_val, mu, sd, full_w)
    p_test = _predict_std(X_test, mu, sd, full_w)
    edges, cal_bins = _fit_isotonic_map(p_val, y_val)
    p_cal_test = _apply_isotonic(p_test, edges, cal_bins)
    models["full"]["isotonic_test"] = _metrics_block(
        y_test, p_cal_test, root_test)
    models["full"]["isotonic_fit"] = {
        "method": "quantile-bin PAV (validation roots)",
        "n_bins": int(len(edges) - 1),
        "n_calibration_rows": int(len(p_val)),
    }
    models["full"]["logistic_reliability_test"] = _ece_table(
        y_test, p_test)[1]
    models["full"]["isotonic_reliability_test"] = _ece_table(
        y_test, p_cal_test)[1]

    return {
        "design": {
            "split": "by corpus root-position set (never by row)",
            "train": "development-set roots",
            "calibrate": "validation-set roots (isotonic fit)",
            "test": "test-set roots only (all metrics)",
            "rows_dev": int(len(dev)), "rows_val": int(len(val)),
            "rows_test": int(len(test)),
            "test_roots": sorted(set(map(str, np.unique(root_test)))),
            "completed_only": True,
        },
        "features": {"groups": _FEATURE_GROUPS,
                     "order": _FEATURE_ORDER,
                     "full": all_feats,
                     "margin_note": ("margin_alpha == 1 - margin_beta exactly "
                                     "at null-window nodes; only margin_beta "
                                     "is used")},
        "models": models,
        "coefficients_full": [
            {"feature": ("intercept" if i == 0 else feats[i - 1]),
             "coefficient": float(c)}
            for i, c in enumerate(full_w)],
    }


def _ordinal_table(df: pd.DataFrame, max_k: int = 12) -> pd.DataFrame:
    """P(cutoff | the attempt sits at quiet ordinal k) among searched quiet
    attempts, with pooled and between-root macro rates.

    This is a conditional hazard on the engine's own quiet loop, not a
    full cutoff-rank distribution over nodes (nodes that cut off on captures
    or never search a quiet move are not in the attempt table; see the
    node-level section).
    """
    sub = df[(df["outcome"] != 3) & (df["quiet_ordinal"] <= max_k)]
    out = []
    for k in range(1, max_k + 1):
        g = sub[sub["quiet_ordinal"] == k]
        n = len(g)
        if n == 0:
            continue
        ev = (g["outcome"] == 1)
        per = pd.DataFrame({"g": g["position_id"], "e": ev.astype(int)}
                           ).groupby("g")["e"].agg(["sum", "count"])
        rate = (per["sum"] / per["count"]).to_numpy(dtype=np.float64)
        out.append({"quiet_ordinal": int(k), "rows": int(n),
                    "events": int(ev.sum()), "event_rate": float(ev.mean()),
                    **_macro_fields(rate)})
    return pd.DataFrame(out)


def report(args) -> int:
    dataset_dir = Path(args.dataset).expanduser().resolve()
    if not dataset_dir.is_dir():
        raise SystemExit(f"dataset dir not found: {dataset_dir}")
    df = _load_attempts(dataset_dir, args.only_depths)
    n = len(df)
    if n == 0:
        raise SystemExit("no attempt rows loaded")
    dec = _load_decisions(dataset_dir, args.only_depths)
    manifest: dict = {}
    mpath = dataset_dir / "manifest.json"
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text())
        except (OSError, json.JSONDecodeError):
            manifest = {}

    completed = df[df["outcome"] != 3]
    fl = df[df["outcome"] == 2]
    total_attempt_cost = int(df["nodes_consumed"].sum())
    engine_nodes = sum(e.get("nodes") or 0 for e in manifest.get("files", []))
    sample_rate = float(manifest.get("sample_rate", 1.0))

    out: dict = {
        "schema_version": "research-baseline-report/2",
        "dataset_dir": str(dataset_dir),
        "note": ("behavior-policy-conditioned: describes the engine's own "
                 "observational policy at sampled nodes; not unbiased for "
                 "unsearched moves (plan.md section 8)"),
        "rows": n,
        "decision_nodes": int(len(dec)),
        "sample_rate": sample_rate,
        "rows_full_rate_equivalent": int(round(n / sample_rate))
        if sample_rate > 0 else n,
        "attempt_cost_sum_nested": total_attempt_cost,
        "engine_root_search_nodes_total": engine_nodes,
    }
    set_counts_dec = {s: int((dec['position_set'] == s).sum())
                     for s in ('development', 'validation', 'test')}
    set_counts_att = {s: int((df['position_set'] == s).sum())
                      for s in ('development', 'validation', 'test')}

    md = [f"# Phase 3 calibration baseline report (protocol P3.2)\n",
          f"Dataset: `{dataset_dir}`  \n"
          f"Rows (searched quiet moves at sampled nodes): {n:,}  "
          f"(sampled decision nodes: {len(dec):,}; recorded at node-sample "
          f"rate {sample_rate:g}; full-rate-equivalent rows ≈ "
          f"{out['rows_full_rate_equivalent']:,})  \n"
          f"Rows are prefix-free: one run per (depth, root), so no "
          f"iterative-deepening trace appears twice.  \n"
          f"Sampled nodes / attempt rows per corpus set: development "
          f"{set_counts_dec['development']:,} / {set_counts_att['development']:,} · "
          f"validation {set_counts_dec['validation']:,} / "
          f"{set_counts_att['validation']:,} · test {set_counts_dec['test']:,} / "
          f"{set_counts_att['test']:,}  \n"
          f"Roots are the sample unit (n = "
          f"{dec['position_id'].nunique()}); between-root macro columns "
          f"(`macro_*`, `n_roots`) report the spread across roots and are the "
          f"appropriate uncertainty for root-correlated rows.\n"]
    out["set_counts"] = {"decision_nodes": set_counts_dec,
                        "attempt_rows": set_counts_att}

    # -- design/provenance
    design_lines = []
    if manifest:
        build = (manifest.get('build_command')
                 or (manifest.get('engine_executable', '') + " "
                     + str(manifest.get('engine_executable_sha256', ''))))
        design_lines.append(
            f"Engine: `{manifest.get('engine_banner', '?')}`  \n"
            f"Research build: `{build or '?'}`  \n"
            f"Corpus: `{manifest.get('corpus_id', '?')}` sha256 "
            f"`{manifest.get('corpus', {}).get('sha256', '?')}`  \n"
            f"Depths: {manifest.get('depths')}; sample rate "
            f"{manifest.get('sample_rate')}; seed {manifest.get('seed')}; "
            f"max records {manifest.get('max_records')}  \n"
            f"Policy version: `{manifest.get('policy_version', '?')}`  \n"
            f"Excluded cells: {manifest.get('excluded_cells', [])}  \n"
            f"Problems: {manifest.get('problems')}  \n"
            f"Collection protocol: {manifest.get('collection_protocol', '?')}")
    md.append("\n## Design and provenance\n\n" + "\n".join(design_lines) + "\n")
    if manifest:
        out["provenance"] = {
            "engine_banner": manifest.get("engine_banner"),
            "build_command": manifest.get("build_command"),
            "corpus": manifest.get("corpus", {}),
            "depths": manifest.get("depths"),
            "seed": manifest.get("seed"),
            "max_records": manifest.get("max_records"),
            "policy_version": manifest.get("policy_version"),
            "excluded_cells": manifest.get("excluded_cells"),
            "problems": manifest.get("problems"),
            "timestamp_utc": manifest.get("timestamp_utc"),
        }

    # -- node-cost wording
    md.append("\n### Note on node counts\n\n"
              "`nodes_consumed` is the *local* subtree count spent by one "
              "attempt (nested: parent attempts re-count work later counted "
              "again by sampled descendants). The attempt-cost sum is "
              "therefore an overlapping measure, not unique engine work. "
              f"The non-overlapping engine root-search totals from the run "
              f"summaries are {engine_nodes:,} nodes across the included "
              f"runs. Attempt-level quantiles below are unaffected by the "
              f"overlap semantics.\n")

    # -- status table
    tab = _status_table(df)
    md.append("\n## Status distribution (attempt rows)\n\n"
              + _md_table(tab) + "\n")
    out["status_distribution"] = _records(tab)
    censored = int((df["outcome"] == 3).sum())
    out["censored_aborted"] = censored

    # -- cutoff rate by quiet ordinal (pooled + macro)
    ord_tab = _ordinal_table(df)
    md.append("\n## Cutoff rate by quiet ordinal (searched quiet attempts)\n\n"
              "Conditional hazard: P(cutoff | the engine searched this move "
              "as its k-th quiet attempt). Row counts are not independent; "
              "`macro_*` columns are the between-root spread.\n\n"
              + _md_table(ord_tab) + "\n")
    out["ordinal_cutoff"] = _records(ord_tab)

    # -- cutoff rate by context (pooled + macro)
    cut_cols = [
        ("cutoff_by_remaining_depth", "depth", [1, 3, 5, 7, 9, 11, 13, np.inf]),
        ("cutoff_by_ply", "ply", [0, 5, 10, 15, 20, 25, 30, 40, np.inf]),
        ("cutoff_by_margin_beta", "margin_beta",
         list(range(-1200, 1201, 300)) + [np.inf]),
    ]
    for key, col, edges in cut_cols:
        t = _macro_rate_table(completed, _bucket(completed[col], edges),
                              completed["outcome"] == 1)
        md.append(f"\n## Cutoff rate by {col}\n\n" + _md_table(t) + "\n")
        out[key] = _records(t)

    # -- fail-low cost tails by context (pooled quantiles + macro)
    cost_cols = [
        ("cost_by_remaining_depth", "depth", [1, 3, 5, 7, 9, 11, 13, np.inf]),
        ("cost_by_ply", "ply", [0, 5, 10, 15, 20, 25, 30, 40, np.inf]),
        ("cost_by_margin_beta", "margin_beta",
         list(range(-1200, 1201, 300)) + [np.inf]),
    ]
    for key, col, edges in cost_cols:
        t = _macro_quant_table(fl, _bucket(fl[col], edges),
                               fl["nodes_consumed"])
        md.append("\n## Nodes consumed by a failed quiet attempt (outcome 2) "
                  f"by {col}\n\n"
                  "Costs are heavy-tailed; median/p90/p99 are reported. "
                  "`macro_mean` is the mean over roots of the per-root mean.\n\n"
                  + _md_table(t) + "\n")
        out[key] = _records(t)

    # -- node-type flags (pooled + macro)
    flags = ["improving", "tt_hit", "tt_move_present", "gives_check",
             "is_tt_move"]
    flag_rows = []
    for f in flags:
        for v, label in [(True, f), (False, f"not_{f}")]:
            g = completed[completed[f] == v]
            if len(g) == 0:
                continue
            per = pd.DataFrame({"g": g["position_id"],
                                "e": (g["outcome"] == 1).astype(int)}
                               ).groupby("g")["e"].agg(["sum", "count"])
            rate = (per["sum"] / per["count"]).to_numpy(dtype=np.float64)
            flg = g[g["outcome"] == 2]
            rec = {"flag": label, "rows": int(len(g)),
                   "cutoff_rate": float((g["outcome"] == 1).mean()),
                   **_macro_fields(rate),
                   "fl_mean_nodes": float(flg["nodes_consumed"].mean())
                   if len(flg) else float("nan"),
                   "fl_median_nodes": float(np.median(flg["nodes_consumed"]))
                   if len(flg) else float("nan")}
            flag_rows.append(rec)
    flag_tab = pd.DataFrame(flag_rows)
    md.append("\n## Cutoff rate / fail-low cost by node-type flags\n\n"
              + _md_table(flag_tab) + "\n")
    out["flag_cutoff"] = flag_rows

    # -- re-search and TT-move success (pooled + per-root span)
    tt = completed[completed["is_tt_move"]]
    ntt = completed[~completed["is_tt_move"]]
    rs = completed[completed["child_search_count"] == 2]
    rsfl = df[(df["child_search_count"] == 2) & (df["outcome"] == 2)]

    def _span(per_rates) -> dict:
        fields = _macro_fields(per_rates)
        return {"n_roots": fields["n_roots"],
                "per_root_min": fields["macro_min"],
                "per_root_max": fields["macro_max"]}

    tt_per = pd.DataFrame({"g": tt["position_id"],
                           "e": (tt["outcome"] == 1).astype(int)}
                          ).groupby("g")["e"].mean()
    ntt_per = pd.DataFrame({"g": ntt["position_id"],
                            "e": (ntt["outcome"] == 1).astype(int)}
                           ).groupby("g")["e"].mean()
    extra = {
        "re_search_rate": float((df["child_search_count"] == 2).mean()),
        "re_search_rows": int((df["child_search_count"] == 2).sum()),
        "tt_quiet_rows": int(len(tt)),
        "tt_quiet_cutoff_rate": float((tt["outcome"] == 1).mean())
        if len(tt) else float("nan"),
        "tt_quiet_cutoff_span": _span(tt_per.to_numpy(dtype=np.float64)),
        "non_tt_quiet_cutoff_rate": float((ntt["outcome"] == 1).mean())
        if len(ntt) else float("nan"),
        "non_tt_quiet_cutoff_span": _span(ntt_per.to_numpy(dtype=np.float64)),
        "re_search_fl_mean_nodes": float(rsfl["nodes_consumed"].mean())
        if len(rsfl) else float("nan"),
        "re_search_fl_median_nodes": float(np.median(rsfl["nodes_consumed"]))
        if len(rsfl) else float("nan"),
        "re_search_fl_rows": int(len(rsfl)),
        "note": ("TT rows are *searched quiet TT moves* at sampled nodes "
                 "(single-thread, cold-process, 16 MB hash); rates are "
                 "behavior-policy-conditioned, not a causal effect of the TT "
                 "flag."),
    }
    md.append("\n## Re-search and quiet-TT-move\n\n```\n"
              + json.dumps(extra, indent=2) + "\n```\n")
    out["research_tt"] = extra

    # -- node-level (decision-joined) quiet-loop structure
    node = _node_frame(df, dec)
    n_node = len(node)
    with_q = node[node["n_quiet"] > 0]
    cut_nodes = node[node["n_fh"] > 0]
    no_cut = node[(node["n_fh"] == 0) & (node["n_quiet"] > 0)]
    cut_share_all = len(cut_nodes) / n_node if n_node else float("nan")
    cut_share_withq = (len(cut_nodes) / len(with_q)) if len(with_q) else float("nan")

    per_all = pd.DataFrame({"g": node["position_id"],
                            "c": (node["n_fh"] > 0).astype(int)}
                           ).groupby("g")["c"].mean()
    per_wq = pd.DataFrame({"g": with_q["position_id"],
                           "c": (with_q["n_fh"] > 0).astype(int)}
                          ).groupby("g")["c"].mean()

    node_summary = {
        "sampled_nodes": int(n_node),
        "nodes_with_quiet_attempt": int(len(with_q)),
        "nodes_quiet_cutoff": int(len(cut_nodes)),
        "nodes_no_quiet_cutoff": int(len(no_cut)),
        "quiet_cut_share_all_nodes": float(cut_share_all),
        "quiet_cut_share_all_span": _span(
            per_all.to_numpy(dtype=np.float64)),
        "quiet_cut_share_of_nodes_with_quiet_attempt": float(cut_share_withq),
        "quiet_cut_share_withq_span": _span(
            per_wq.to_numpy(dtype=np.float64)),
        "mean_quiet_attempts_per_node": float(with_q["n_quiet"].mean())
        if len(with_q) else float("nan"),
        "note": ("A node with no quiet cutoff either cut off on a capture / "
                 "TT move path not recorded as a quiet attempt, exhausted its "
                 "move list, or was aborted; the share *of all sampled nodes* "
                 "therefore includes capture/TT cutoffs and is a lower bound "
                 "on the true node cutoff share."),
    }
    md.append("\n## Node-level quiet-loop structure (decision-joined)\n\n"
              "Quiet attempts are grouped by their decision node; a quiet "
              "cutoff ends the node's moves loop, so each node has at most "
              "one. `wasted_before_cut` = fail-low quiet-attempt costs "
              "preceding the node's quiet cutoff.\n\n```\n"
              + json.dumps(node_summary, indent=2) + "\n```\n")
    out["node_level_summary"] = node_summary

    nd_cols = [
        ("node_cut_by_remaining_depth", "depth",
         [1, 3, 5, 7, 9, 11, 13, np.inf]),
        ("node_cut_by_margin_beta", "margin_beta",
         list(range(-1200, 1201, 300)) + [np.inf]),
    ]
    for key, col, edges in nd_cols:
        t = _macro_rate_table(with_q, _bucket(with_q[col], edges),
                              with_q["n_fh"] > 0)
        md.append(f"\n### Node quiet-cutoff share by {col}\n"
                  "(nodes with >=1 searched quiet move)\n\n"
                  + _md_table(t) + "\n")
        out[key] = _records(t)

    # cutoff ordinal distribution among cutting nodes
    cut = cut_nodes[["fh_ord", "position_id"]]
    if len(cut):
        ord_dist = (cut.groupby("fh_ord", observed=True)["position_id"]
                    .size().reset_index(name="nodes"))
        ord_dist["share"] = ord_dist["nodes"] / len(cut)
        ord_dist["cum_share"] = ord_dist["share"].cumsum()
        per_ord = pd.DataFrame({"g": cut["position_id"],
                                "o": cut["fh_ord"]}).groupby("g")["o"].mean()
        md.append("\n### Quiet-cutoff ordinal among cutting nodes\n\n"
                  "Distribution of the quiet ordinal at which the node's "
                  "quiet loop cut off (nodes with >=1 quiet attempt that "
                  "produced a quiet cutoff).\n\n"
                  + _md_table(ord_dist) + "\n"
                  + "macro (mean ordinal over roots): "
                  + json.dumps(_macro_fields(
                      per_ord.to_numpy(dtype=np.float64))) + "\n")
        out["node_cutoff_ordinal"] = _records(ord_dist)
        out["node_cutoff_ordinal_macro"] = _macro_fields(
            per_ord.to_numpy(dtype=np.float64))

    # wasted cost before cutoff by remaining depth
    for key, col, edges, lab in (
            ("node_wasted_by_remaining_depth", "depth",
             [1, 3, 5, 7, 9, 11, 13, np.inf], "cutting nodes"),
            ("node_nocut_cost_by_remaining_depth", "depth",
             [1, 3, 5, 7, 9, 11, 13, np.inf],
             "non-cutting nodes with quiet attempts")):
        if key.startswith("node_wasted"):
            sub = cut_nodes
            val = cut_nodes["wasted_before_cut"]
            title = "Nodes wasted on failed quiet attempts before the node's quiet cutoff"
        else:
            sub = no_cut
            val = no_cut["no_cut_quiet_cost"]
            title = "Total quiet-attempt cost at nodes where no quiet move cut off"
        t = _macro_quant_table(sub, _bucket(sub[col], edges), val)
        md.append(f"\n### {title} by {col}\n({lab})\n\n" + _md_table(t) + "\n")
        out[key] = _records(t)

    # -- grouped calibration
    feature_groups = _FEATURE_GROUPS
    feature_order = _FEATURE_ORDER
    md.append("\n## Behavior-policy-conditioned cutoff calibration "
              "(root-position-held-out)\n\n"
              "Completed attempts (outcome 1 vs 2). Logistic (ridge on "
              "standardized features, unpenalized intercept) is fit on the "
              "development-set roots; PAV-isotonic calibration is fit on the "
              "validation-set roots; every metric is evaluated only on the "
              "test-set roots. `macro_*` columns report the between-root "
              "spread across the test roots (roots are the sample unit; rows "
              "inside one root are correlated).\n\n"
              "Features: `" + ", ".join(feature_order) + "` groups =\n\n"
              + "```\n" + json.dumps(feature_groups, indent=2) + "\n```\n")
    cal = _grouped_calibration(df)
    out["calibration"] = cal

    def _model_row(name: str, m: dict) -> dict:
        pooled = m["test"]["pooled"]
        mac = m["test"]["macro"]
        iso = m.get("isotonic_test", {}).get("pooled")
        return {
            "model": name, "rows_test": pooled["n"],
            "base_rate": pooled["base_rate"],
            "auc": pooled["auc"], "auc_macro": mac["auc"]["macro_mean"],
            "auc_macro_sd": mac["auc"]["macro_sd"],
            "brier": pooled["brier"], "brier_macro": mac["brier"]["macro_mean"],
            "logloss": pooled["logloss"],
            "logloss_macro": mac["logloss"]["macro_mean"],
            "ece10": pooled["ece10"], "ece10_macro": mac["ece10"]["macro_mean"],
            "converged": m.get("converged"),
            "isotonic_brier": iso["brier"] if iso else None,
            "isotonic_logloss": iso["logloss"] if iso else None,
            "isotonic_ece10": iso["ece10"] if iso else None,
        }

    rows_cal = pd.DataFrame([_model_row(nm, m)
                             for nm, m in cal["models"].items()])
    md.append("### Test-root evaluation by model\n\n" + _md_table(rows_cal)
              + "\n")
    out["calibration_table"] = _records(rows_cal)

    full = cal["models"]["full"]
    md.append("\n### Full model: logistic vs validation-fitted isotonic "
              "(test roots)\n\n")
    md.append("```\n" + json.dumps({
        "logistic": full["test"]["pooled"],
        "isotonic": full["isotonic_test"]["pooled"],
        "isotonic_fit": full["isotonic_fit"],
    }, indent=2) + "\n```\n")
    md.append("\nPer-test-root breakdown:\n\n")
    per_rows = []
    for d in full["test"]["per_root"]:
        r = {"model": "logistic", **d}
        per_rows.append(r)
    for d in full["isotonic_test"]["per_root"]:
        r = {"model": "isotonic", **d}
        per_rows.append(r)
    md.append(_md_table(pd.DataFrame(per_rows)) + "\n")
    out["calibration_per_test_root"] = per_rows

    md.append("\n### Full-model standardized coefficients\n\n"
              + _md_table(pd.DataFrame(cal["coefficients_full"])) + "\n")
    md.append("\n### Reliability tables (test roots, pooled)\n\n"
              "Logistic:\n\n"
              + _md_table(pd.DataFrame(full["logistic_reliability_test"]))
              + "\n\nIsotonic:\n\n"
              + _md_table(pd.DataFrame(full["isotonic_reliability_test"]))
              + "\n")

    out["censoring_note"] = ("ABORTED_STOP rows carry no value semantics; they "
                             "are excluded from cutoff-rate and calibration "
                             "denominators (kept in cost/total tables).")
    out["limitations"] = {
        "n_roots": int(dec["position_id"].nunique()),
        "n_test_roots": int(len(cal["design"]["test_roots"])),
        "note": ("12 corpus roots (3 test roots) is a small cluster count; "
                 "macro columns quantify the between-root spread but cannot "
                 "substitute for a larger root sample. Labels are "
                 "behavior-policy-conditioned (the move was searched under "
                 "the recorded ordering/reduction schedule); they are not "
                 "candidate-quality labels, and reordering gains need "
                 "counterfactual phases 4/5."),
    }
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
                        "c1-d-004@20 when a root's volume exceeds the "
                        "engine's per-run hard cap; prefer lowering --rate so "
                        "sample rates stay uniform across cells)")
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
