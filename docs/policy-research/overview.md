# Policy Research — Overview

Status board for the incremental alpha-beta proof-policy project. See
[`plan.md`](plan.md) for the full specification (objectives, engineering rules, phases,
tests, exit gates, definition of done).

## Current state

| Phase | Description | Status |
|---|---|---|
| 0 | Architecture inventory and mutation audit | **Complete** — reviewed at commit `d2e8a7dc`; decisions in `architecture-inventory.md` §10 |
| 1 | Deterministic research harness | **Complete** — corpus-v1 runner + run manifest; determinism gate passes (12 roots × depth 11). Hardened per review (see below) and gate regenerated with a fully identified executable |
| 2 | Versioned research logging | **In progress** — recorder/serializer, decoder/validator, and `verify-research` gate (protocol P2.1) landed; full depth-11 gate **PASSED** on the committed tree (artifacts `tools/policy_research/runs/policy-research-corpus-v1-d11-h16-research-20260903T234538/`) |
| 3 | Observational dataset and calibration baseline | Not started |
| 4 | Root-level counterfactual experiments | Not started |
| 5 | Internal counterfactual search sandbox | Not started |
| 6 | Oracle / ratio / interaction-gap studies | Not started |
| 7 | Proof-time survival modeling | Not started |
| 8 | Exact and prototype Jacobian experiments | Not started |
| 9 | Teacher model | Not started |
| 10 | Incremental production student | Not started |
| 11 | Conservative engine integration | Not started |
| 12 | On-policy data generation (DAgger) | Not started |
| 13 | LMR shadow modeling | Not started |

## Phase 2 — engine-side scaffold progress

- Research compile flag `POLICY_RESEARCH` and zero-behavior UCI options
  (`PolicyResearch*`, see `src/policy_research/research_options.h`): **committed and
  verified** — research vs production builds of the same commit are node-identical
  (`bench 16 1 10 default depth` = 453 169 nodes for both).
- Options are registered through the canonical `make -C src research-build` target
  (objclean-first; plain `EXTRACXXFLAGS` reuse of stale objects is unsafe). Invalid
  option values are rejected in research builds: an `info string` diagnostic is
  emitted **and** the stored option value is rolled back (macro-gated change in
  `ucioption.cpp`), so the option map and `Research::config()` never disagree.
  `Research::enabled()` is the single canonical activation predicate; recorder
  phases must enforce a finite internal cap even when `PolicyResearchMaxRecords`
  is 0.

## Phase 2 — observational recorder (versioned logging)

- `src/policy_research/research_log.{h,cpp}` (both macro-gated): in-memory
  recorder + length-prefixed binary serializer flushed per root. Deterministic
  sampling from search state only (no mutable RNG); sample identity =
  mix(rootKey, key, ply, depth, rootIterationDepth, nodeSerial, seed). Records:
  `RUN_START/ROOT_START/DECISION_POINT/MOVE_ATTEMPT/ROOT_END/RUN_END` (+ reserved
  `COUNTERFACTUAL_RESULT` id). Layout: `docs/policy-research/data-schema.md`
  (`research-log/1` container, `research-data/1` records).
- Hooks (all `#ifdef POLICY_RESEARCH`, strictly write-only): `uci.cpp` arms the
  recorder for fixed-depth single-thread `go` and finalizes at loop exit
  (`wait_for_search_finished` guards the writer); `search.cpp` samples at the
  NonPV `moves_loop` entry (non-root, not in check, no excluded move) and emits a
  `MOVE_ATTEMPT` per searched quiet move (fail-high-cutoff / fail-low /
  aborted-stop; unsearched moves are never labeled).
- `off` mode and macro-off builds are strict no-ops; a finite hard cap
  (`HARD_MAX_RECORDS`/`HARD_MAX_BYTES`) always bounds collection, and a cap hit
  writes an `ERROR_RECORD` (never silent).
- Tooling: `tools/policy_research/decode_research_log.py` (decode/validate/
  compare/JSONL export) and `run_corpus.py verify-research` (protocol P2.1:
  off vs on#1 vs on#2 passes, search-result equality across all three, decoded
  record-stream equality between on passes, per-root FEN cross-check).
  Recordings land under `tools/policy_research/runs/*research-*/` (git-ignored).
- Gate evidence (research build of the committed tree, clean banner): full
  corpus, depth 11, hash 16, 5% sample — search results (bestmove/normalized
  rows/nodes) identical across off/on/on for all 12 roots; decoded records
  identical between the on passes; all logs decode clean and match corpus root
  FENs. Artifacts `tools/policy_research/runs/policy-research-corpus-v1-d11-h16-
  research-20260903T234538/` (git-ignored).

## Phase 1 hardening (review repair pass)

- Run manifests now record `engine_executable_sha256`, `engine_banner` (embedded
  source commit), and `build_command`, because repository HEAD alone does not
  identify the binary that ran (a PGO build of an older commit previously produced
  the gate evidence).
- Every root result must demonstrate a completed fixed-depth search (final depth,
  positive nodes, score, non-empty PV, or a mate announcement) before the
  determinism comparison runs; aborted searches cannot pass the gate.
- `SyzygyPath` is explicitly cleared and `UCI_Chess960` pinned false in every run;
  the value network must resolve or the run refuses to start.
- `compare_results` is symmetric (extra positions in run B are also failures).
- Corpus tag semantics are documented (`tag_semantics`) and the two incorrect
  `castling` tags corrected (metadata-only, recorded in `history`).
- Determinism gate re-verified from committed code with a fully identified
  executable; artifacts under `tools/policy_research/runs/` (git-ignored).

## Phase 0 deliverables

- `architecture-inventory.md` — exact code locations, flows, and integration points for
  Search, MovePicker, TT, worker state, Position, and NNUE.
- `search-mutation-audit.md` — every mutable object reachable from recursive search, with
  ownership, update sites, and counterfactual-isolation requirements.
- Open questions list (bottom of `architecture-inventory.md`).

## Phase 0 exit gate

> Do not continue until every mutable object reachable from recursive search has been
> accounted for.

Branch: `policy-research`. Base commit: `06675f70`.
