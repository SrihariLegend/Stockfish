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
| 3 | Observational dataset and calibration baseline | **Complete (schema-derivable subset)** — prefix-free uniform-rate dataset v2 + root-held-out baseline report (protocol P3.2) landed; the P3.1 evaluation review (leaky row-random split + duplicated iterative-deepening prefixes, row-level SEs on 12 roots, non-random cell exclusion, collinear margins, in-sample isotonic fit) was fully addressed. §8.3 history / §8.4 baseline-score calibration and candidate-denominator analyses remain gated on the counterfactual candidate-enumeration schema (Phases 4/5). Strong *generalization* claims need a larger root sample (corpus/v2) |
| 4 | Root-level counterfactual experiments | **In progress (P4.1 kickoff)** — research-only root force-first override landed (commit `b5ab17f7`), determinism + parity verified, smoke-pilot oracle gap measured on 3 roots (R_norm 0.055–0.924 at fixed depth 14) |
| 5 | Internal counterfactual search sandbox | Not started |
| 6 | Oracle / ratio / interaction-gap studies | Not started |
| 7 | Proof-time survival modeling | Not started |
| 8 | Exact and prototype Jacobian experiments | Not started |
| 9 | Teacher model | Not started |
| 10 | Incremental production student | Not started |
| 11 | Conservative engine integration | Not started |
| 12 | On-policy data generation (DAgger) | Not started |
| 13 | LMR shadow modeling | Not started |

## Phase 4 — root-level counterfactual experiments

### P4.1 kickoff — research-only root force-first override (`b5ab17f7`)

First engine-side Phase 4 unit (plan §9.1). Research build only; macro-off
engine source untouched.

- New UCI option `PolicyResearchForceFirstMove` (string, default empty),
  honored only when the research master switch is on with mode
  `RootCounterfactual`:
  - the named legal root move is rotated to the front of the root move order
    at the start of every root iteration on the main thread (searched first
    at the new depth);
  - all remaining root moves keep their relative baseline order and ordinary
    root/PVS semantics stay intact — this is an explicit order override,
    never a `searchmoves`-style restriction (plan §9.1);
  - inactive in `Observational` mode (corpus labels stay baseline-policy
    conditioned) and when the master switch is off; default option is a
    strict no-op;
  - an illegal root move is ignored with a one-time `info string`
    diagnostic and the search is identical to the no-override search.
- Hook: `Search::Worker::iterative_deepening()` in `src/search.cpp`
  (research-gated), main thread only, `multiPV == 1`.
- 5 engine-gated integration tests (`TestForceFirstRootOrder`): repeat-run
  determinism of baseline and forced-first searches, node-cost change under
  a non-best forced first move, illegal-move parity + diagnostic,
  observational-mode and master-off parity with baseline.
- Standing zero-regression gate re-verified: macro-off `bench 16 1 10 default
  depth` = **453 169 nodes** (identical to prior commits); the override code
  is fully `#ifdef POLICY_RESEARCH`-gated.

Smoke pilot (research build `dev-20260904-b5ab17f7`, fresh process per
intervention, fixed depth 14, Hash 16, Threads 1; candidates = engine top-4
MultiPV at depth 10):

| root | C_base | forced-first nodes (best move unchanged vs baseline in every run) | min | R_norm |
|---|---|---|---|---|
| c1-d-001 | 43 275 | e2e4 25 317, d2d4 49 065, g1f3 38 603, c2c4 21 146 | 21 146 | 0.511 |
| c1-v-001 | 20 956 | d4c5 29 994, c4f7 2 233, e1g1 2 482, b1c3 1 600 | 1 600 | 0.924 |
| c1-t-001 | 26 292 | f1e2 24 839, f1g2 31 976, f3d1 320 781 | 24 839 | 0.055 |

Honest framing (no over-claiming): this is a **necessary-mechanism and
magnitude smoke check**, not the plan-§9.3–9.6 oracle-gap report. It compares
fixed-depth node cost only (no wall time, no §9.4 reference-quality
confirmation at depth), the candidate set is the engine's own top-k at a
shallower depth (self-referential — an upper bound on ordering opportunity),
and 3 roots cannot carry bootstrap confidence intervals. Notably, forcing the
baseline's own best move first does *not* reproduce baseline cost (e.g.
c1-d-001: 25 317 vs 43 275): the always-first semantics removes the
best-move-switch churn the baseline pays for, which is itself an ordering
cost worth quantifying later. Next increments: P4.2 reference-result study
(§9.3/§9.4 measurements incl. wall time and deeper/reduced-selectivity
reference), then a larger root sample before oracle-gap claims
(`tools/policy_research/p4_force_first.py` reproduces the pilot table).

## Phase 3 — observational dataset and calibration baseline

- New tool `tools/policy_research/p3_dataset.py` (`collect` / `report`):
  uniform-rate corpus collection with a fresh engine process and log per
  (depth, root), decode + RUN_START/ROOT_START cross-checks per run (same
  conventions as `verify-research`), and per-root JSONL rows: one **attempt
  row per searched quiet move** at sampled eligible nodes (decision features
  copied inline for self-contained modeling) plus a decisions table carrying
  the FENs. Every row records `node_weight = 1/sample_rate`.
- Dataset v2 (protocol P3.2, engine = research build of `c9878d11`): one
  run per corpus root at depth 20, node-sample rate 0.5, hash 16, seed 101,
  policy `baseline-observational-v1`; **12 runs, 2 135 633 decisions,
  5 607 088 attempt rows**, ~2 min wall. The design is **prefix-free and
  uniform**: single deepest run per root (a deeper run replays shallower
  iterations byte-identically, so P3.1's 14/17/20 ladder triplicated ~29% of
  rows — now impossible by construction) and rate 0.5 is uniform across all
  roots, so the previously excluded `c1-d-004@20` cell fits (3 103 654
  records < the engine hard cap). **No excluded cells, zero overflow, zero
  ABORTED_STOP rows.** Artifacts under `tools/policy_research/runs/
  policy-research-corpus-v1-d20-h16-rate0.5-dataset-20260904T014612/`
  (git-ignored): `manifest.json`, `logs/`, `rows/`, and
  `baseline-report.{md,json}`.
- The superseded P3.1 dataset (depths 14/17/20 at rate 1.0, 11 GB, ~29%
  duplicate prefixes, one excluded cell) was removed after its review
  findings were fixed; its numbers are preserved in the Protocol P3.1 record
  in `experiment-protocols.md`.
- Report v2 (`research-baseline-report/2`, superseded in place by `/3` below)
  fixes, addressing the review:
  every rate/cost table reports pooled numbers **plus between-root macro
  columns** (roots are the sample unit — 12 of them — not the 5.6M rows);
  **grouped calibration** (logistic fit on development-set roots, isotonic
  fit on validation-set roots, metrics only on test-set roots); features are
  standardized, the intercept is unpenalized, `margin_alpha` is dropped as
  exactly `1 - margin_beta` at null-window nodes, and coefficients +
  convergence are reported; an **isotonic fit on validation roots** replaces
  the in-sample fit; a **node-level section** joins attempts to their
  decision nodes; node-count wording distinguishes local nested attempt
  costs from unique engine root-search totals (19 720 628 nodes across the
  runs).
- Report v3 (`research-baseline-report/3`) addresses the read-only expert
  review of v2 with tool/report/docs changes only (no engine change; the
  v3 run reproduced every pooled/macro number of v2 byte-identically): a
  **within-node reordering probe** (test roots, 16 061 late-quiet-cutoff
  nodes / 34 933 predecessor pairs: full-model pair accuracy 0.029,
  cost-weighted 0.011 — global AUC 0.936 is explicitly *not* move-ordering
  quality); **root-balanced sensitivity** fits (equal full-rate mass per
  root; ranking stable, probability map objective-sensitive with 3
  validation roots, so no canonical calibration map is claimed); reliability
  tables gain per-bin **`gap`** columns with a worst-bin headline (logistic
  +0.202 in [0.3,0.4), isotonic +0.142); a **per-root volume table** makes
  the c1-d-004 = 67.5%-of-development-attempts imbalance visible;
  **node_weight (IPW) support** is wired into pooled rate tables and both
  calibration objectives for future non-uniform datasets (uniform P3.2 rows
  take a byte-identical fast path); an **observational opportunity-
  accounting** block (wasted-before-cut = 12.3% of fail-low local cost;
  no-quiet-cut loops = 87.7%; TT-fail-low = 31.6%; re-search = 9.0% —
  sampled local costs, explicitly not causal bounds); and **provenance**
  now records the research binary's banner-embedded commit (`c9878d11`)
  plus the report tool's git commit/worktree state.
- Baseline headline numbers (behavior-policy-conditioned — the engine's own
  policy at sampled nodes, **not** unbiased for unsearched moves): cutoff
  rate 9.92% of completed attempts; first-quiet-move cutoff 29.2% pooled
  (macro 31.3 ± 7.8% across roots); quiet TT-move cutoff 69.7% vs 6.7%
  non-TT; re-search rate 2.39%; node level: 37% of nodes with ≥1 searched
  quiet move end in a quiet cutoff (root span 29–56%), 79.1% of those at the
  first quiet attempt.
- Test-root calibration (3 held-out roots, 847 222 completed attempts):
  full-model AUC 0.936 (macro 0.932 ± 0.011); validation-fitted isotonic
  yields a small honest gain over logistic — Brier 0.0605 → 0.0598,
  log-loss 0.2030 → 0.2015, ECE10 0.0195 → 0.0176 — not the in-sample
  0.0005 ECE of the v1 report. Ablations: ordinal 0.865 / tt-only 0.779 /
  context-only 0.812 AUC. Ranking quality at *move-ordering granularity* is
  far weaker: the within-node reordering probe measures 0.029 pair accuracy
  on 34 933 late-cutoff predecessor pairs (Report v3 bullet).
  History/baseline-score calibration (§8.3/§8.4) remains out of scope for
  `research-data/1` (requires candidate enumeration;
  see experiment-protocols.md).
- Documented limitation: 12 corpus roots (3 test roots) is a small cluster
  count; macro columns quantify between-root spread but cannot substitute
  for a larger root sample. Growing corpus/v2 (many game-diverse positions,
  split by source game) is a prerequisite before strong generalization
  claims.
- Tools/docs only: no engine change, macro-off production build untouched.
- Unit suite: 85 tests (1 skipped), incl. grouped macro tables, node-frame
  invariants, grouped root-held-out calibration (row-weighted +
  root-balanced + non-uniform IPW), the within-node reordering probe,
  weighted PAV/quantile/IRLS helpers, and report smoke over a three-set
  synthetic dataset (uniform and non-uniform node_weight).
- Phase 3 exit gate (§8.5): met for the `research-data/1`-derivable subset —
  `baseline-report.{md,json}` is reproducible from the manifest
  (per-executable determinism) and establishes quality/calibration of the
  recorded context heuristics without claiming policy improvement. The
  plan's full §8.2–§8.4 lists need candidate enumeration + MovePicker
  baseline features (counterfactual schema, Phases 4/5) and are tracked
  there.

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
