#!/usr/bin/env python3
"""Decode, validate, and compare policy-research binary logs.

Mirrors docs/policy-research/data-schema.md (container research-log/1,
records research-data/1). All integers little-endian. Strings are
u16 length + bytes. Never parses native structs: every width and meaning is
defined here independently of the engine ABI.

Usage:
  decode_research_log.py <file.bin> [--jsonl out.jsonl] [--validate]
  decode_research_log.py --compare a.bin b.bin [--summary-only]
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

MAGIC = b"PXRLOG\x01\x00"
ENDIAN_MARKER = 0x01020304
LOG_FORMAT_VERSION = 1
DATA_SCHEMA_VERSION = 1

# Record types (stable; see data-schema.md).
RUN_START, ROOT_START, DECISION_POINT, MOVE_ATTEMPT, COUNTERFACTUAL_RESULT, \
    ROOT_END, RUN_END, ERROR_RECORD = range(1, 9)

TYPE_NAMES = {
    RUN_START: "RUN_START",
    ROOT_START: "ROOT_START",
    DECISION_POINT: "DECISION_POINT",
    MOVE_ATTEMPT: "MOVE_ATTEMPT",
    COUNTERFACTUAL_RESULT: "COUNTERFACTUAL_RESULT",
    ROOT_END: "ROOT_END",
    RUN_END: "RUN_END",
    ERROR_RECORD: "ERROR_RECORD",
}

OUTCOME_NAMES = {1: "fail_high_cutoff", 2: "fail_low", 3: "aborted_stop"}
MODE_NAMES = {
    0: "off", 1: "observational", 2: "root_counterfactual",
    3: "internal_counterfactual", 4: "shared_permutation",
    5: "jacobian_dump", 6: "reduction_shadow",
}
MOVE_TYPE_NAMES = {0: "NORMAL", 1: "PROMOTION", 2: "EN_PASSANT", 3: "CASTLING"}

# Move raw layout (Move::raw()): to 0-5, from 6-11, promotion 12-13, type 14-15.
FILES = "abcdefgh"
RANKS = "12345678"


def _move_raw_to_uci(raw: int) -> str:
    to = raw & 0x3F
    frm = (raw >> 6) & 0x3F
    promo = ((raw >> 12) & 3) + 2  # KNIGHT..QUEEN encoded as 0..3 at bits 12-13
    mtype = (raw >> 14) & 3
    if frm > 63 or to > 63:
        return f"<invalid:{raw}>"
    sq = lambda i: FILES[i & 7] + RANKS[i >> 3]
    s = sq(frm) + sq(to)
    if mtype == 1:  # PROMOTION
        s += "nbrq"[promo]
    return s


def _read_str(buf: bytes, off: int):
    (n,) = struct.unpack_from("<H", buf, off)
    return buf[off + 2: off + 2 + n].decode("utf-8", "replace"), off + 2 + n


class ValidationError(Exception):
    pass


def decode_file(path: Path):
    """Return (header dict, list of record payload dicts)."""
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        raise ValidationError(f"{path}: bad magic")
    if struct.unpack_from("<I", data, 8)[0] != ENDIAN_MARKER:
        raise ValidationError(f"{path}: endian marker mismatch")
    (fmt,) = struct.unpack_from("<H", data, 12)
    if fmt != LOG_FORMAT_VERSION:
        raise ValidationError(f"{path}: unsupported log format {fmt}")
    (ulen,) = struct.unpack_from("<I", data, 14)
    uuid = data[18: 18 + ulen]
    header = {"uuid": uuid.hex(), "format_version": fmt}

    records = []
    off = 18 + ulen
    end = len(data)
    while off < end:
        if off + 8 > end:
            raise ValidationError(f"{path}: truncated frame header at {off}")
        rtype, schema, plen = struct.unpack_from("<HHI", data, off)
        off += 8
        if schema != DATA_SCHEMA_VERSION:
            raise ValidationError(f"{path}: unsupported record schema {schema}")
        if off + plen > end:
            raise ValidationError(f"{path}: truncated payload for type {rtype}")
        payload = data[off: off + plen]
        off += plen
        p = _decode_payload(rtype, payload, path)
        p["_type"] = rtype
        records.append(p)
    return header, records


def _decode_payload(t: int, b: bytes, path: Path) -> dict:
    if t == RUN_START:
        mode, seed, thr, cap = struct.unpack_from("<BQII", b, 0)
        o = 17
        pol, o = _read_str(b, o)
        eng, o = _read_str(b, o)
        if o != len(b):
            raise ValidationError(f"{path}: RUN_START trailing bytes")
        return {"mode": mode, "seed": seed, "sample_threshold": thr,
                "max_records": cap, "policy_version": pol, "engine_info": eng}
    if t == ROOT_START:
        key, d, = struct.unpack_from("<QI", b, 0)
        fen, o = _read_str(b, 12)
        if o != len(b):
            raise ValidationError(f"{path}: ROOT_START trailing bytes")
        return {"root_key": key, "target_depth": d, "fen": fen}
    if t == DECISION_POINT:
        (rk, ser, key, ply, depth, root_iter, alpha, beta, st_eval) = \
            struct.unpack_from("<QQQiiiiii", b, 0)
        (flags,) = struct.unpack_from("<B", b, 48)
        (rule50,) = struct.unpack_from("<H", b, 49)
        (stm,) = struct.unpack_from("<B", b, 51)
        fen, o = _read_str(b, 52)
        if o != len(b):
            raise ValidationError(f"{path}: DECISION_POINT trailing bytes")
        return {"root_key": rk, "node_serial": ser, "key": key, "ply": ply,
                "depth": depth, "root_iter_depth": root_iter, "alpha": alpha,
                "beta": beta, "static_eval": st_eval, "flags": flags,
                "improving": bool(flags & 1), "tt_hit": bool(flags & 2),
                "tt_move_present": bool(flags & 4), "rule50": rule50,
                "side_to_move": stm, "fen": fen}
    if t == MOVE_ATTEMPT:
        (rk, ser, aser, raw, qord, tot, gc, tt, cc, fd, rsd, ab, bb, vr) = \
            struct.unpack_from("<QQQHHHBBBiiiii", b, 0)
        (consumed,) = struct.unpack_from("<Q", b, 53)
        (outcome,) = struct.unpack_from("<B", b, 61)
        if len(b) != 62:
            raise ValidationError(f"{path}: MOVE_ATTEMPT bad length {len(b)}")
        return {"root_key": rk, "node_serial": ser, "attempt_serial": aser,
                "move_raw": raw, "move_uci": _move_raw_to_uci(raw),
                "quiet_ordinal": qord, "total_attempted": tot,
                "gives_check": bool(gc), "is_tt_move": bool(tt),
                "child_search_count": cc, "first_child_depth": fd,
                "research_depth": rsd, "alpha_before": ab, "beta_before": bb,
                "value_returned": vr, "nodes_consumed": consumed,
                "outcome": outcome}
    if t == COUNTERFACTUAL_RESULT:
        raise ValidationError(f"{path}: unexpected COUNTERFACTUAL_RESULT (reserved)")
    if t == ROOT_END:
        key, dc, ac = struct.unpack_from("<QQQ", b, 0)
        if len(b) != 24:
            raise ValidationError(f"{path}: ROOT_END bad length")
        return {"root_key": key, "decision_count": dc, "attempt_count": ac}
    if t == RUN_END:
        dd, aa, ovf, err = struct.unpack_from("<QQBB", b, 0)
        if len(b) != 18:
            raise ValidationError(f"{path}: RUN_END bad length")
        return {"run_decisions": dd, "run_attempts": aa, "overflow": bool(ovf),
                "error_code": err}
    if t == ERROR_RECORD:
        (code,) = struct.unpack_from("<H", b, 0)
        msg, o = _read_str(b, 2)
        if o != len(b):
            raise ValidationError(f"{path}: ERROR_RECORD trailing bytes")
        return {"code": code, "message": msg}
    raise ValidationError(f"{path}: unknown record type {t}")


def validate(records: list[dict], path: Path, fen_check: str | None = None) -> dict:
    """Structural + semantic validation. Raises ValidationError on problems.

    Returns a small stats dict (per-root counts, outcome counts, totals)."""
    if not records:
        raise ValidationError(f"{path}: no records")
    if records[0]["_type"] != RUN_START or records[-1]["_type"] != RUN_END:
        raise ValidationError(f"{path}: stream must start RUN_START and end RUN_END")

    roots = {}        # root_key -> {"fen", decisions: dict serial->decision}
    in_root = False
    cur_root = None
    run_dec = run_att = 0
    overflow_seen = False
    for i, rec in enumerate(records):
        t = rec["_type"]
        if t == RUN_START:
            if in_root:
                raise ValidationError(f"{path}: RUN_START inside root at {i}")
            if rec["mode"] not in MODE_NAMES:
                raise ValidationError(f"{path}: RUN_START bad mode {rec['mode']}")
        elif t == ROOT_START:
            if in_root:
                raise ValidationError(f"{path}: nested ROOT_START at {i}")
            rk = rec["root_key"]
            if rk in roots:
                raise ValidationError(f"{path}: duplicate root key {rk:#x}")
            roots[rk] = {"fen": rec["fen"], "decisions": {}, "target_depth": rec["target_depth"]}
            if fen_check is not None and rec["fen"] != fen_check:
                raise ValidationError(f"{path}: root fen mismatch at {i}")
            in_root = True
            cur_root = roots[rk]
        elif t == DECISION_POINT:
            if not in_root:
                raise ValidationError(f"{path}: DECISION_POINT outside root at {i}")
            rk = rec["root_key"]
            if rk not in roots:
                raise ValidationError(f"{path}: decision references unknown root at {i}")
            ns = rec["node_serial"]
            if ns in cur_root["decisions"]:
                raise ValidationError(f"{path}: duplicate decision node_serial {ns} at {i}")
            if ns == 0:
                raise ValidationError(f"{path}: zero node serial at {i}")
            cur_root["decisions"][ns] = rec
        elif t == MOVE_ATTEMPT:
            if not in_root:
                raise ValidationError(f"{path}: MOVE_ATTEMPT outside root at {i}")
            ns = rec["node_serial"]
            d = cur_root["decisions"].get(ns)
            if d is None:
                raise ValidationError(
                    f"{path}: attempt at {i} references missing decision {ns}")
            if rec["root_key"] != d["root_key"]:
                raise ValidationError(f"{path}: attempt/decision root mismatch at {i}")
            if rec["move_raw"] == 0 or rec["move_raw"] == 65:
                raise ValidationError(f"{path}: attempt carries null/none move at {i}")
            if "invalid" in rec["move_uci"]:
                raise ValidationError(f"{path}: undecodable move raw {rec['move_raw']} at {i}")
            if rec["outcome"] not in OUTCOME_NAMES:
                raise ValidationError(f"{path}: bad outcome {rec['outcome']} at {i}")
        elif t == COUNTERFACTUAL_RESULT:
            raise ValidationError(f"{path}: reserved type emitted at {i}")
        elif t == ROOT_END:
            if not in_root:
                raise ValidationError(f"{path}: ROOT_END outside root at {i}")
            rk = rec["root_key"]
            if cur_root is None or cur_root is not roots.get(rk):
                raise ValidationError(f"{path}: ROOT_END root mismatch at {i}")
            if rec["decision_count"] != len(cur_root["decisions"]):
                raise ValidationError(
                    f"{path}: ROOT_END decision count {rec['decision_count']} != "
                    f"{len(cur_root['decisions'])} at {i}")
            run_dec += rec["decision_count"]
            run_att += rec["attempt_count"]
            in_root = False
            cur_root = None
        elif t == ERROR_RECORD:
            overflow_seen = True
        elif t == RUN_END:
            if in_root:
                raise ValidationError(f"{path}: RUN_END inside open root at {i}")
            if rec["run_decisions"] != run_dec or rec["run_attempts"] != run_att:
                raise ValidationError(
                    f"{path}: RUN_END totals mismatch "
                    f"(recorded {rec['run_decisions']}/{rec['run_attempts']}, "
                    f"counted {run_dec}/{run_att})")
        if t == DECISION_POINT:
            pass
    if in_root:
        raise ValidationError(f"{path}: unterminated root (no ROOT_END)")

    # quiet-ordinal monotonicity per (root, node), plus outcome aggregates.
    outcomes = {}
    per_node = {}
    for rk, root in roots.items():
        for d in root["decisions"].values():
            per_node[(rk, d["node_serial"])] = {"last_qord": 0}
    for rec in records:
        if rec["_type"] != MOVE_ATTEMPT:
            continue
        key = (rec["root_key"], rec["node_serial"])
        slot = per_node.get(key)
        if slot is None:
            continue
        if rec["quiet_ordinal"] <= slot["last_qord"]:
            raise ValidationError(
                f"{path}: non-increasing quiet ordinal {rec['quiet_ordinal']} at node {key[1]}")
        slot["last_qord"] = rec["quiet_ordinal"]
        outcomes[OUTCOME_NAMES[rec["outcome"]]] = \
            outcomes.get(OUTCOME_NAMES[rec["outcome"]], 0) + 1

    stats = {
        "roots": {rk: {"fen": r["fen"], "decisions": len(r["decisions"]),
                       "target_depth": r["target_depth"]} for rk, r in roots.items()},
        "run_decisions": run_dec,
        "run_attempts": run_att,
        "outcomes": outcomes,
        "overflow": overflow_seen,
    }
    return stats


def records_payloads(records: list[dict]) -> list[dict]:
    """Comparable view: frame data only (no _type is fine; type kept)."""
    return [{k: v for k, v in r.items()} for r in records]


def compare(path_a: Path, path_b: Path) -> dict:
    ha, ra = decode_file(path_a)
    hb, rb = decode_file(path_b)
    sa = validate(ra, path_a)
    sb = validate(rb, path_b)
    pa, pb = records_payloads(ra), records_payloads(rb)
    return {"equal": pa == pb, "header_equal_uuid": ha["uuid"] == hb["uuid"],
            "count_a": len(ra), "count_b": len(rb),
            "stats_a": sa, "stats_b": sb}


def main() -> int:
    args = sys.argv[1:]
    if "--compare" in args:
        i = args.index("--compare")
        a = Path(args[i + 1])
        b = Path(args[i + 2])
        res = compare(a, b)
        print(f"{a} vs {b}: records_equal={res['equal']} "
              f"(a={res['count_a']} b={res['count_b']})")
        print(f"  header uuid equal: {res['header_equal_uuid']} (must be False)")
        if not res["equal"]:
            print("  stats A:", json.dumps(res["stats_a"], indent=2))
            print("  stats B:", json.dumps(res["stats_b"], indent=2))
        return 0 if res["equal"] else 1

    # Decode/validate one file.
    summary_only = "--summary-only" in args
    pos = []
    jsonl = None
    it = iter(args)
    for a in it:
        if a == "--jsonl":
            jsonl = Path(next(it))
        elif a in ("--validate", "--summary-only"):
            pass
        else:
            pos.append(Path(a))
    if len(pos) != 1:
        print("usage: decode_research_log.py <file.bin> [--jsonl out.jsonl] "
              "[--summary-only]", file=sys.stderr)
        return 2
    path = pos[0]
    header, records = decode_file(path)
    stats = validate(records, path)
    if jsonl is not None:
        with open(jsonl, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    if not summary_only:
        print(f"{path}: {len(records)} records; schema research-data/1; "
              f"run uuid {header['uuid'][:8]}…")
        rs = records[0]
        print(f"  RUN_START: mode={MODE_NAMES.get(rs['mode'])} seed={rs['seed']} "
              f"threshold={rs['sample_threshold']} cap={rs['max_records']} "
              f"policy={rs['policy_version']!r}")
        print(f"  engine: {rs['engine_info']}")
        for rk, r in stats["roots"].items():
            print(f"  root {rk:#016x}: decisions={r['decisions']} "
                  f"target_depth={r['target_depth']} fen={r['fen'][:50]}…")
    print(f"  totals: decisions={stats['run_decisions']} attempts={stats['run_attempts']} "
          f"outcomes={stats['outcomes']} overflow={stats['overflow']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
