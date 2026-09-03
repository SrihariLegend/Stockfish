# Policy Research — Overview

Status board for the incremental alpha-beta proof-policy project. See
[`plan.md`](plan.md) for the full specification (objectives, engineering rules, phases,
tests, exit gates, definition of done).

## Current state

| Phase | Description | Status |
|---|---|---|
| 0 | Architecture inventory and mutation audit | **Complete** — reviewed at commit `d2e8a7dc`; decisions in `architecture-inventory.md` §10 |
| 1 | Deterministic research harness | **Complete** — corpus-v1 runner + run manifest; determinism gate passes (12 roots × depth 11). Hardened per review (see below) and gate regenerated with a fully identified executable |
| 2 | Versioned research logging | **In progress** — recorder/serializer, decoder/validator, and `verify-research` gate (protocol P2.1) landed; Phase 2 review fixes in `3850e64e` and round-2 fixes in `c9878d11`; full depth-11 gate **PASSED** on the committed tree (artifacts `tools/policy_research/runs/policy-research-corpus-v1-d11-h16-research-20260904T004843/`) |
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
- Gate evidence (research build of the committed tree `c9878d11`, clean banner):
  full corpus, depth 11, hash 16, 5% sample — search results
  (bestmove/normalized rows/nodes) identical across off/on/on for all 12 roots;
  decoded records identical between the on passes; all logs decode clean, match
  corpus root FENs, and cross-check RUN_START mode/seed/threshold/cap/policy and
  engine identity against the pass manifest. Artifacts
  `tools/policy_research/runs/policy-research-corpus-v1-d11-h16-
  research-20260904T004843/` (git-ignored). Production macro-off build of the
  same commit is node-identical (`bench 16 1 10 default` = 453 169 nodes).

## Phase 2 review fix round 2 (`c9878d11`)

Fixes for the second read-only review round (engine edits `#ifdef POLICY_RESEARCH`
only):

- **Decoder never crashes on malformed logs**: the fixed-width header, every u16
  string length prefix, and each declared string span are bounds-checked before
  any unpack/slice, so truncated/oversized payloads raise the decoder's
  `ValidationError` instead of a native `struct.error` traceback in the gate.
- **Recorder is thread-safe**: the deferred-`go` hand-off is serialized by a
  `std::recursive_mutex` around `on_go`/`on_root_search_start`/`on_root_search_end`/
  `on_run_end`, so a `go` arriving on the UCI thread while the searching thread
  closes the previous root can no longer tear or lose the deferred request;
  `active_` is now `std::atomic` for the lock-free hot-path reads. Verified with a
  ThreadSanitizer build under repeated overlapping-`go` stress: 0 race reports.
- **Runner preflight covers the log path**: `verify-research` now requires
  `PolicyResearchLogPath` in the engine's declared options (a binary without it
  used to run all three passes before failing on missing logs).
- **Schema docs**: RUN_END `overflow`/`error_code` carry only 0/1 wire values
  (anything else is rejected, not coerced); outcome 3 is a single generic
  `ABORTED_STOP`/right-censored class (the schema records no stop reason, so
  `SEARCH_ABORTED` vs `BUDGET_CENSORED` are not separable); one-run-per-`go`
  requires rotating `PolicyResearchLogPath` (the file is opened with truncation).
- Regression tests added for all of the above (55 unit tests; gate/TSAN evidence
  as listed above).

## Phase 2 review repair pass (`3850e64e`)

Fixes for the Phase 2 read-only review findings, all macro-gated on the engine
side (tools/docs outside the macro):

- **Attempt-cost boundary**: `nodes_consumed` is measured from just before
  Step 16 (Singular Extensions), so the excluded-search verification subtree is
  charged to the TT move's MOVE_ATTEMPT, not to the sibling bookkeeping.
- **One `go` = one run file**: `on_go()` finalizes any still-open run
  (RUN_END + close) before arming under the current options; disabling research
  or quitting also closes the run. An overlapping `go` (arriving while a search
  is active) is deferred and re-evaluated when the active root closes, so
  mid-search option/path/switch changes never leak into a stale arming.
- **Gate provenance**: `verify-research` fails fast when CLI research spin
  values fall outside the engine's declared UCI ranges, and every decoded log is
  cross-checked against the manifest (RUN_START mode/seed/threshold/cap/policy
  version + engine banner identity); decode/validate now also enforces root
  containment, totals, overflow/error-code/ERROR_RECORD consistency, and
  outcome/value agreement (55 unit tests).
- **IO failures surface**: fopen/fwrite/fflush failures print an
  `info string` diagnostic and disable logging for that run (never silent loss).
- **No hot-path FEN cost**: the DECISION_POINT FEN is formatted lazily, only for
  sampled nodes; un-sampled eligible nodes pay only cheap key reads. Sample
  identities and record streams are unchanged (verified by count-identical
  gate passes before/after the refactor).
- **Docs**: `data-schema.md`, `experiment-protocols.md` (P3.x), and `plan.md`
  (§8.1) now scope Phase 3 rows to actually attempted searched quiet moves:
  with `research-data/1`, completed fail-lows are exact negative events at
  NonPV null-window nodes, and `OBSERVED_ALPHA_RAISE`/`OBSERVED_EXACT`/
  `UNOBSERVED_AFTER_CUTOFF`/`INVALID_OR_SKIPPED` require candidate enumeration
  (deferred to the counterfactual schema).

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
