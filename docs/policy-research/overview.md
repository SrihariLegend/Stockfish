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
| 4 | Root-level counterfactual experiments | **Complete (P4.2 remediation, causal decomposition & canonical evaluation)** — expanded immutable `corpus/v3` (26 roots, unburned test split), exact byte-for-byte replay verification, causal decomposition (isolated pure MovePicker order vs aspiration depth reduction), 10-trial randomized counterbalanced timing (+73.0% engine speedup on Giuoco Piano, +63.5% on CPW4), and 18-root canonical evaluation (9/18 positive opportunities, 5 baseline-optimal, 3 harmful, 1 no-valid; 18.5% macro cumulative / 34.9% target-iteration savings). |
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

### P4.1 — research-only root force-first override & review remediation

Engine-side Phase 4 unit (plan §9.1) and follow-up review remediation:

- Research UCI options (honored only with `PolicyResearch` on and mode
  `RootCounterfactual` on the main thread with `multiPV == 1`):
  - `PolicyResearchForceFirstMove` (string, default empty): named legal root
    move to force first; inert in Observational mode and when master switch
    is off.
  - `PolicyResearchForceFirstDepth` (spin, 0..256, default 0):
    - `0`: persistent schedule (Experiment A), overriding at every root depth.
    - `> 0`: isolated target depth (Experiment B), overriding ONLY when
      `rootDepth == forceFirstDepth`. Depths 1..D-1 run under standard
      baseline conditions, guaranteeing identical TT and history state at the
      decision boundary. Forcing the baseline's own best move at depth D is
      an exact bit-for-bit no-op in deterministic fields (nodes, score, bestmove,
      PV).
- Hook & semantics: `Search::Worker::iterative_deepening()` in `src/search.cpp`.
  At the root, Stockfish sets `ttData.move = rootMoves[0]` and emits it in
  `MovePicker`'s `MAIN_TT` stage. The former index-0 move drops into its
  natural MovePicker stage (captures, quiets scored by history); all other
  moves keep their relative MovePicker order. The causal estimand is therefore:
  *cost of substituting the root TT move and allowing normal MovePicker / PVS
  behavior thereafter*, not an exact remainder replay.
- Tablebase safety: overrides check `rootMoves[i].tbRank == rootMoves[0].tbRank`
  to prevent disrupting Syzygy contiguous rank grouping (`pvFirst`..`pvLast`).
- Zero regression: macro-off `bench 16 1 10 default depth` = **453 169 nodes**
  (standing gate unchanged). Full test suite: 92 tests (7 engine-gated in
  `TestForceFirstRootOrder`).

Tooling & quality validation (`tools/policy_research/p4_force_first.py`):
- Fails fast at startup if the binary is missing research options.
- Enforces `Threads 1`, `Hash 16`, `MultiPV 1`; token-based UCI parsing with
  depth-reached assertions; records wall time (ms), nodes, score (cp/bound),
  bestmove, and PV.
- Captures depth-(D-1) node baseline to report both **cumulative nodes**
  (total search to depth D) and **incremental depth-D nodes** ($\Delta N_D$).
- Deeper reference search (D16) evaluates plan §9.4 reference agreement: checks
  best-move agreement with reference and score tolerance (<= 50 cp) before
  calling a candidate quality-valid; checks mate score sign agreement;
  distinguishes mild drift (<= 100 cp) from score collapse (> 100 cp).
- Dual-baseline comparison: reports candidate score delta relative to both the
  same-depth baseline ($\Delta\text{base}$) and the deeper reference ($\Delta\text{ref}$).
- Score tolerance sensitivity analysis: evaluates the frontier across gates
  $\pm 25, \pm 50, \pm 75, \pm 100\text{ cp}$ to reveal cost-quality tradeoffs.
- Disentangled time metrics: reports wall time of the node-optimal candidate;
  reports a distinct alternative only if strictly faster; warns of single-run
  millisecond scheduling jitter.
- Candidate set framing: depth-10 MultiPV top-k is a search-informed empirical
  shortlist (upper bound for deployable cheap policy, lower bound for all-legal
  oracle).
- Root reservation: `c1-t-001` was inspected and is marked burned/exploratory.
- Durable provenance & artifacts: recorded under
  `tools/policy_research/runs/policy-research-p4-kickoff-d14-h16-20260904/`
  with schema `policy-research-p4-counterfactual/2`.

Pilot results at depth 14 (reference depth 16, candidate depth 10, k=4):

**Experiment B: Isolated Target-Depth Override (Pure Counterfactual at Depth 14)**
| root | set | C_base @ D14 (best) | Ref @ D16 (best) | Quality-valid candidates | R_norm (cumul) | R_norm (incr D14) | R_norm (time) |
|---|---|---|---|---|---:|---:|---:|
| c1-d-001 | development | 43 275 / 2 680 incr (e2e4, cp 27) | 75 655 (e2e4, cp 39) | e2e4 (43 275, cp 27) | **+0.000** | **+0.000** | +0.000 |
| c1-v-001 | validation | 20 956 / 17 285 incr (d4c5, cp 654) | 123 243 (d4c5, cp 645) | b1c3 (5 643 / 1 972 incr, cp 642) | **+0.731** | **+0.886** | +0.722 |
| c1-t-001 | test (burned) | 26 292 / 7 742 incr (f1e2, cp 133) | 128 139 (f1e2, cp 168) | f1e2 (26 292, cp 133) | **+0.000** | **+0.000** | −0.056 |

*Notes on isolated mode:*
- On `c1-d-001` (startpos): baseline best `e2e4` is an exact no-op (43 275 nodes, 2 680 incremental D14 nodes) because the PV was stable from depth 13. Other candidates cost more (+78% to +95% incremental regret). Baseline was already optimal across all tolerance gates ($\pm 25$ to $\pm 100$ cp).
- On `c1-v-001` (tactical root): forcing `b1c3` first takes 5 643 cumulative nodes and only 1 972 incremental D14 nodes (score cp 642, within 3 cp of D16 reference). All candidates agree with D16 reference best `d4c5`. Oracle headroom on this tactical root: **73.1% in cumulative nodes, 88.6% in incremental depth-14 nodes**. Wall time at node-optimal dropped to 5 ms (tied for fastest). The gain is invariant across all tolerance gates ($\pm 25$ to $\pm 100$ cp).
- On `c1-t-001`: sensitivity analysis reveals a cost-quality frontier rather than simple zero opportunity: at $\pm 25$ cp no candidate is valid; at $\pm 50$ cp only baseline best `f1e2` passes ($R_{\text{norm}} = 0.000$); at $\pm 75$ cp candidate `f1g2` enters ($R_{\text{norm}} = +0.171$ cumulative, $+0.580$ incremental, score $-18$ cp vs base and $-53$ cp vs ref); candidate `f3d1` suffered evaluation collapse ($-111$ cp vs ref) and is rejected at all gates.

**Experiment A: Persistent Schedule (Overriding at Depths 1..14)**
- Demonstrates massive trajectory churn: on `c1-d-001`, forcing `e2e4` drops nodes to 25 317 (−41.5%) because always-first prevents best-move switches. On `c1-v-001`, `b1c3` appears to take 1 600 nodes, but its score drifted to cp 719 (+74 cp vs reference) and fails quality. On `c1-t-001`, forcing `f3d1` causes a 12.2x explosion to 320 781 nodes and collapses score to 0 cp.
- Incremental node counts in persistent mode reflect each candidate's own divergent trajectory step, not a common-prefix causal delta.

### P4.2 — expanded corpus (v3), causal decomposition, depth ladders, and timing

Building on P4.1's isolated counterfactual override, P4.2 addresses all
methodological findings and recommendations from the expert review:

1. **Immutable Expanded Corpus (`corpus/v3`)**:
   - 26 positions (10 development: `c3-d-001`..`c3-d-010`, 8 validation: `c3-v-001`..`c3-v-008`, 8 test: `c3-t-001`..`c3-t-008`).
   - Re-indexed with unique IDs (`c3-*`) to eliminate version collision. SHA256: `c7c7d47c558f8d23b6d3a726ebda886a98633f39d5d9749b153e052259a5b944`.
   - Audited every single root: every root with `source_line_moves` is verified byte-for-byte against Stockfish's internal board parser across all 6 fields (including halfmove clock and fullmove count).
   - Corrected King's Indian (`c3-d-008`) halfmove clock to 3 (exact replay match), and replaced the non-standard `c2-v-007` FEN with the authentic classic Philidor 6th-rank defense tabiya (`4k3/R7/4r3/4P3/8/8/8/4K3 w - - 0 1`, Philidor 1777).
   - Test set is strictly isolated and quarantined for final model evaluation.
2. **Move-Identity Telemetry Attribution & Target-Iteration Effort**:
   - Emits per-attempt aspiration telemetry (`info string research root_telemetry ... attempts <depth>:<val>:<res>:<alpha>:<beta> ...`).
   - Disambiguates cumulative effort from target-iteration effort increments $\Delta E_D(m) = E_D(m) - E_{D-1}(m)$ by snapshotting and looking up effort by move identity (`Move`), resolving the vector-reordering sorting defect so move incremental efforts sum directly to target-iteration search nodes.
3. **Common-Prefix Causal Decomposition (Pure Move Order vs Aspiration Depth Control)**:
   Added research options `PolicyResearchPreserveAspiration` and `PolicyResearchDisableFailHighReduction`, depth-gated to the intervention target depth so that depths 1..D-1 run standard Stockfish search identically (`prev_depth_nodes` is 100% byte-for-byte identical across all 5 conditions):
   - On `c3-d-004` (CPW4, tactical middlegame): savings are **100% pure MovePicker alpha-beta ordering efficiency** with zero aspiration failures in any condition (64.8% cumulative / 95.6% target-iteration node reduction at unreduced nominal depth 14).
   - On `c3-v-001` (Giuoco Piano blunder): pure move ordering at nominal depth 14 saves 31.0% cumulative / 37.6% target-iteration nodes (14,450 vs 20,956); joint intervention forced to nominal depth 14 saves 55.7% (9,293 nodes); joint intervention with fail-high depth reduction saves 73.1% (5,643 nodes at D11).
   - On `c3-d-002` (Kiwipete): fail-high depth reductions control search explosion in high-branching tactical positions (forcing nominal D14 inflates nodes from 18k to 32k or 78k).
4. **Balanced Latin-Square Multi-Trial Timing Benchmark**:
   - Implemented a balanced Latin square cyclic design across trials in `p4_force_first.py` (`--trials N`) where each treatment appears in each ordinal position an equal number of times across trials, eliminating thermal and first-runner cache warming biases.
   - On `c3-v-001` (12 counterbalanced runs): engine median search time dropped from 18.0 ms (IQR 0.0 ms, mean 18.2±0.4 ms) to 5.0 ms (IQR 1.0 ms, mean 5.2±0.5 ms), an exact **+72.2% engine search speedup**.
   - On `c3-d-004` (12 counterbalanced runs): engine median search time dropped from 62.0 ms (IQR 1.0 ms, mean 62.2±0.5 ms) to 23.0 ms (IQR 0.0 ms, mean 22.8±0.5 ms), an exact **+62.9% engine search speedup**.
5. **Full 18-Root Population Depth Ladder (D12, D14, D16)**:
   Evaluated the full 18-root development and validation corpus across depths 12, 14, 16 (`depth-ladder-dev-val.json`):
   - Positive opportunity rate expands with depth: **5/18 (27.8%) at D12**, **9/18 (50.0%) at D14**, and **12/18 (66.7%) at D16**.
   - Confirms persistent positive scaling on key roots: `c3-d-004` (+50.6% D12 $\to$ +64.3% D14 $\to$ +80.6% D16), `c3-d-002` (+25.8% D12 $\to$ +28.7% D14 $\to$ +44.7% D16), `c3-v-005` (+49.4% D12 $\to$ +3.6% D14 $\to$ +15.9% D16).
   - Reveals depth-dependent phase transitions: `c3-v-001` (0% D12 $\to$ +73.1% D14 $\to$ +44.4% D16), `c3-d-006` (0% D12 $\to$ +18.7% D14 $\to$ +82.7% D16).
   - Demonstrates stable baseline-optimality on mate-in-2 tacticals: `c3-d-009` (0.0% at all plies).
6. **18-Root Canonical Population Distribution (`corpus/v3` Dev + Val, Schema v4)**:
   - **7 Result-preserving savings** (38.9%): preserves baseline best move, beats baseline cost.
   - **2 Convergence corrections** (11.1%): corrects baseline suboptimal move to reference best move, beats baseline cost.
   - **6 Baseline optimal** (33.3%): baseline is already the best/cheapest move (regret 0.0; including `c3-d-009` mate in 2).
   - **2 Harmful interventions** (11.1%): candidate matches reference, but costs strictly more nodes than baseline (`c3-v-003`, `c3-v-004`).
   - **1 No valid candidate** (5.6%): no candidate matches reference within score tolerance (`c3-d-005`).
   - Total positive opportunities: **9 / 18 (50.0%)**.
   - Macro mean savings: **18.5% cumulative, 34.9% target-iteration**.
   - Macro median savings: **1.8% cumulative, 7.8% target-iteration**.
   - Pooled savings across 18 roots: **22.4% cumulative** (908,892 $\to$ 705,384 nodes), **42.8% target-iteration** (475,490 $\to$ 271,982 nodes).

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
