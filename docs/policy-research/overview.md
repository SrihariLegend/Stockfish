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
| 4 | Root-level counterfactual experiments | **Complete (Factorial Causal Decomposition, Multi-Trial Timing & Canonical Evidence)** — 26-root immutable `corpus/v3` (quarantined test set), full 2³ factorial causal decomposition isolating aspiration-optimism coupling and PV-follow decoupling, 12-trial counterbalanced timing (73.7% kernel time reduction [3.80× speedup factor] on `c3-v-001`, 64.9% kernel reduction [2.85× speedup factor] on `c3-d-004`), 18-root strict-candidate depth ladder (D12/D14/D16), and canonical tracked evidence in `docs/policy-research/evidence/p4-canonical/`. |
| 5 | Internal counterfactual search sandbox | **Complete (Isolated Worker Sandbox, TT Overlay, Full Stack Forward Init, Whole-Table TT Immutability & Safety Hardening, Whole-Node Replay Force-Next Estimand & Versioned Internal-Counterfactual Dataset)** — compile-time templated `search<NT, TTAccess>` / `qsearch<NT, TTAccess>`, deep `StateInfo::previous` chain cloning with threefold repetition detection, private history rebinding, copy-on-first-access `ResearchTTOverlay`, whole-table 100% bit-for-bit base TT immutability verification, forward stack initialization up to `MAX_PLY + 10`, `Threads == 1` invariant assertion, automatic shadow search node budgeting & cooperative cancellation (`nodeBudget`), `ScopedShadowProbe` recorder suppression, and internal NNUE evaluation fidelity verified. M2/M3 adds the force-next estimand (whole-node replay from true search entry, candidate forced into slot 1 as a genuine reorder — the swallowed natural prefix is buffered and re-searched after the forced move fails low, never deleted), decision-point capture inside the baseline replay itself, MovePicker-exact candidate enumeration driving the real picker, and the versioned JSONL dataset `internal-counterfactual/3` (finite-default budget + robust stop handling, decision rows with marginal inclusion probabilities and per-probe censoring, measurement-A per-candidate attribution (`first`/`cutoff`/`prefix_pops`), sibling-`(ss+1)->cutoffCnt` mirroring making replay node counts equal live subtrees, live-subtree `node_exit` audit rows, complete `root_end` accounting, non-perturbation verified bit-for-bit on live search). |
| 6 | Oracle / ratio / interaction-gap studies | Preliminary GO — corrected /3 evidence on the fixed engine (canonical four-root set `11032847` + ten-root breadth corpus `2d77b835`, 15,825 decision rows across 12 independent roots). Blind slot-1 promotion costs 1.76-2.50x the natural-order node count per root (pooled ord1..3 = 2.19/2.29/2.40) while the classification-preserving local oracle saves 19.1-29.6% of baseline nodes per root (mean 23.1%, sd 3.6; 9-18% under exact-value equivalence) — the adaptive-selection headroom is real and consistent. Measurement A landed (schema /3, `5da89d37`) and shows 85-92% of natural cutoffs happen on the ordinal-0 move, with forced ordinal-1..3 candidates proving the bound on only 27-35%/19-21%/11-13% of rows. Remaining: the formal Phase-6 report — candidate-level q/e ratio study with root-clustered/Hájek- HT intervals, top-K interaction gap, learnability of the oracle choice, and the final go/no-go for Phases 7-13. |
| 7 | Proof-time survival modeling | Not started |
| 8 | Exact and prototype Jacobian experiments | Not started |
| 9 | Teacher model | Not started |
| 10 | Incremental production student | Not started |
| 11 | Conservative engine integration | Not started |
| 12 | On-policy data generation (DAgger) | Not started |
| 13 | LMR shadow modeling | Not started |

## Phase 5 — internal counterfactual search sandbox & isolation infrastructure

Engine-side Phase 5 unit (plan §10) and comprehensive safety remediation:

### P5.1 — Isolated Worker Snapshot & State Decoupling
To evaluate counterfactual actions at internal search nodes without perturbing
the live search tree, Stockfish implements `Stockfish::Research::IsolatedWorker`
(`src/policy_research/worker_snapshot.h`, `worker_snapshot.cpp`):
- **Worker Ownership & Memory Safety**: Heap-allocates an isolated `Search::Worker`
  using `make_unique_large_page<Search::Worker>` along with heap-allocated
  `Search::Stack` (pinned across `MAX_PLY + 10` frames) and `Search::PVMoves`. This
  prevents stack-overflow hazards and `-Wstack-usage=128000` compiler warnings.
- **Private History & Table Rebinding**: The snapshot owns a private `SharedHistories`
  instance. All stack frame history pointers (`ss->continuationHistory`) and worker
  correction history references are rebound from the parent search to the private
  worker tables using exact relative offset rebinding.
- **Deep Position & StateInfo Chain Rebinding**: `Position::clone_to(Position&, std::vector<StateInfo>&)`
  deeply duplicates all board state, piece bitboards, rule-50 counters, and NNUE
  accumulators, and reconstructs the complete linked list of `StateInfo::previous`
  pointers. This preserves historical game trajectories and enables exact threefold
  repetition detection (`st->repetition < 0`, `is_draw(0) == true`) and reversible
  `undo_move()` operations.
- **Forward Stack Sentinel Initialization**: Forward stack frames are initialized
  through `MAX_PLY + 10` rather than merely nominal depth:
  `ss->pv = nullptr; ss->continuationHistory = nullptr; ss->movePicker = nullptr; ss->currentMove = Move::none(); ss->ply = 0;`.
  This guarantees that deep selective extensions, singular search verifications,
  and quiescence searches never encounter uninitialized frame pointers or invalid
  sentinels.
- **Node Budgeting & Cooperative Cancellation**: Shadow searches support a private
  node budget (`nodeBudget`). At every node in `Worker::search()` and `Worker::qsearch()`,
  the isolated worker decrements the budget and triggers cooperative cancellation
  (`threads.stop = true`) upon exhaustion, strictly bounding counterfactual probe
  latency. The shadow search returns a structured `ProbeResult` distinguishing
  completed evaluations from budget-censored searches (`hit_budget = true`,
  `completed = false`), preventing aborted searches from being mistaken for valid
  evaluations or exact draws.
- **Single-Threaded Safety Enforcement**: `is_safe_environment()` checks both
  `liveWorker.options["Threads"] == 1` and `liveWorker.threads.num_threads() == 1`.
  In `IsolatedWorker` construction, this check is enforced at runtime via an
  assert-free condition that throws `std::runtime_error` in all builds (including
  release builds with `-DNDEBUG`), guaranteeing that internal shadow probes only run
  when the base transposition table is synchronously paused and free from concurrent
  thread mutations.
- **Inter-Candidate Isolation & CandidateProbeRunner**:
  When evaluating multiple candidate moves at a decision node, evaluating candidate A
  mutates shadow histories, stack frames, and overlay entries. `CandidateProbeRunner`
  guarantees complete inter-candidate isolation by re-synchronizing worker state via
  `sync_from()`, re-cloning the position, re-binding the stack, and providing an
  independent `ResearchTTOverlay` for each candidate probe. Evaluated candidate-order
  invariance tests verify that probing sequence $[A, B]$ produces bit-identical
  nodes and scores to sequence $[B, A]$.
- **Full Legal Candidate Denominator Enumeration (Phase 3 §8.3/§8.4)**:
  `enumerate_candidates()` and the UCI command `policy_research_enumerate_candidates`
  extract all legal moves at the decision position, computing their MovePicker stage
  (TT move, good capture, killer, quiet, bad capture), stage sort score, `mainHistory`,
  `captureHistory`, `continuationHistory`, and SEE scores. This supplies the complete
  action denominator $\mathcal{A}(s)$ required for offline policy softmax modeling
  and history calibration.
- **Active Internal-Node Search Integration**:
  In `Search::Worker::search()`, eligible NonPV null-window decision nodes invoke
  `Research::on_internal_node_counterfactual()`. When `PolicyResearchMode internal_counterfactual`
  is active, candidate moves are probed in isolated sandboxes while the live recursive
  search proceeds with 100% bit-for-bit live search node equivalence (verified via
  identical node counts and best moves between counterfactual and baseline searches).

### P5.2 — Copy-on-First-Access Transposition Table Overlay
Transposition table mutations during shadow searches are isolated by
`Stockfish::Research::ResearchTTOverlay` (`src/policy_research/tt_overlay.h`, `tt_overlay.cpp`):
- **Overlay Semantics**: Reads probe the private overlay hash map first; on miss,
  they probe the underlying base TT (`TT.probe(...)`). Writes write strictly to the
  overlay map, leaving the base TT completely untouched.
- **Shared Replacement Logic**: Both base `TranspositionTable` and `ResearchTTOverlay`
  share `probe_cluster(const Cluster&, u16, u8)` in `src/tt.h`, ensuring bit-identical
  cluster replacement rules and generation aging between base and shadow searches.
- **Bit-for-Bit Base TT Immutability Verification**: Shadow search test suites
  verify base TT immutability by calculating whole-table memory comparisons
  across 100% of base TT clusters (`cluster_data()`, `byte_size()`), verifying zero
  mutations before and after shadow exploration.

### P5.3 — Zero Production Overhead & Compile-Time Templating
To prevent runtime branches or virtual dispatch overhead in production search hot paths:
- `Search::Worker::search<NT, TTAccess>` and `qsearch<NT, TTAccess>` are fully
  parameterized over compile-time policy access tags (`LiveTT` vs `ResearchTTOverlay`).
- Explicit template instantiations in `src/search.cpp` generate specialized code
  paths for research builds while production builds (`#ifndef POLICY_RESEARCH`) retain
  monomorphic direct calls to `TT.probe()`.
- Production macro-off build benchmark retains exactly **453,169 nodes** for
  `bench 16 1 10 default depth`.

### P5.4 — Recorder Suppression & NNUE Fidelity
- `ScopedShadowProbe`: Suspends observational logging (`Research::log()`) and
  suppresses root telemetry during active shadow searches, preventing counterfactual
  tree explorations from contaminating observational datasets.
- NNUE accumulator fidelity is validated at internal nodes: evaluating positions
  via `evaluate()` and `evaluate_worker()` matches bit-identically across the original
  and isolated worker instances.

### P5.5 — Test Coverage & Canonical Evidence
- C++ Unit Tests: `policy_research_test_sandbox` and `policy_research_test_overlay`
  assert sandbox compilation, stack rebinding, repetition detection, whole-table TT
  immutability, recorder suppression, and NNUE accumulator fidelity (`SANDBOX_TEST_OK`).
- Python Test Suite: `tools/policy_research/tests/test_sandbox.py` (104 tests green
  at P5.5; the suite is now 107 tests — see P5.6).
- Canonical Evidence: All experimental artifacts are committed under
  `docs/policy-research/evidence/p4-canonical/` with clean commit provenance
  (`tool_dirty: False`).

### P5.6 — Whole-Node Replay Force-Next Estimand & Versioned Dataset (M2/M3)

M2/M3 turns the M1 isolation machinery into a measurement instrument for the
plan §10.6 measurement B: *total remaining node cost of the decision node if the
candidate is searched next*.

**Estimand semantics (force-next = whole-node replay, candidate forced to slot 1):**
At an eligible internal decision node (NonPV, null-window, not in check, no
excludedMove, positive rootDepth), the living worker state is snapshotted into an
`IsolatedWorker` and the *entire node is re-searched* on the shadow worker
(measurement B intrinsic). The candidate receives the exact slot-1 search
treatment: naturally ordered earlier `MovePicker` emissions are skipped *without*
incrementing the search's `moveCount` until the forced move is emitted (step-10.6
semantics); the forced move then flows through the ordinary move loop exactly as a
slot-1 move (including pruning paths). If it fails low, replay continues over the
remaining candidates with baseline policy. The arm is thread-local RAII
(`ForceNextScope`), matched only on `(posKey, ply)` at the main move loop;
qsearch/PV/root never consume arms; nesting is an assert-guarded programming error.
Each replay re-syncs worker state from the live worker, clones the position,
rebinds the stack window, and uses a fresh empty `ResearchTTOverlay` (base TT is
read-only for shadow searches).

**MovePicker-exact candidate enumeration:** the decision-row denominator is
produced by driving the real `MovePicker::next_move()` to exhaustion over the live
position with the live worker's histories, then filtering with `pos.legal()`.
Ordinals are the search-visible slot indices (illegal emissions are skipped
without counting). Classification of each emission re-applies the picker's own
splits to the picker's own sort value (`research_emitted_score()`, the value at
`(cur-1)` read immediately after a select-based emission): captures split at
`see_ge(m, -score/18)`, quiets at `score > -14000`; stage names follow the exact
emission order `tt → good_capture → good_quiet → bad_capture → bad_quiet`.
Quiet feature mirrors are pure reads of the live tables (main history, capture
history, pawn-entry history with the documented frame offsets, low-ply history,
and SEE/check/capture flags).

**Selection:** N < 2 ⇒ no row; 2 ≤ N ≤ 8 ⇒ probe all at probability 1; N > 8 ⇒
top-K at probability 1 (K = min(TopK or 4, N-1)) plus two deterministically
hash-sampled candidates from the rest at marginal probability 2/(N-K).

**Dataset:** rows are written as a *separate* versioned JSONL artifact, schema
`internal-counterfactual/3` (spec in `data-schema.md`; /2 was the pre-`5da89d37`
form), selected by
`PolicyResearchMode=internal_counterfactual`; mode selection arms exactly one
writer. Collection is restricted to fixed-depth/fixed-node offline `go` commands
with `Threads == 1`; live stop is honored before and between every replay (an
abort drops the row). Replays carry a finite default node budget (50 000) and a
robust stop/censor contract: `completed = !stopped`, censored probes report
`score = null` and `hit_budget` for budget exhaustion; decision rows record the
full legal denominator, per-candidate features/selection probabilities, the
baseline replay, and per-probe outcomes incl. `forced_slot1` (arm consumption via
the thread-local consumed-counter delta captured before the shadow search) and
fail-high-or-null. Rows are dropped when no forced replay consumed the arm
(degenerate node), and record caps stop sampling with the overflow flag reported
in the always-written `root_end` row (lifecycle rows bypass the decision cap).

**Non-perturbation (verified):** on `position startpos moves e2e4 c7c5 g1f3 d7d6`
`go depth 8`, the armed run (sampling active, 31 decision rows) produced the
identical final info row — score cp 41, nodes 5320, same PV — as the unarmed
run; probes are CPU-only. (Re-verified under the /2 entry-hook pipeline: the
e2e2 suite runs four roots incl. a fixed-node root; each armed root is
bit-identical in live info rows to its unarmed twin and writes a fully
accounted run file with matched decision/node_exit rows.) Non-perturbation claims are limited to fixed-depth /
fixed-node offline collection; recurrences and static-eval re-application inside
replays are documented replay-of-captured-state semantics.

**C++ sandbox suite extensions (Parts 8–12):** (8) whole-node replay
determinism and order invariance across runners plus illegal-forced rejection;
(9) differential enumeration against an independent second fresh MovePicker drive
with per-emission stage-score equality and split self-consistency (startpos, 20
candidates, tt=e2e4; the capture-rich Kiwipete enumeration path is separately
exercised by the Python `policy_research_enumerate_candidates` drive); (10) armed-hook non-mutation and row pipeline
(live key/nodes/base-TT bytes untouched, schema-valid JSONL decision row written
and re-read); (11) `force_next_step` swallow/count semantics, mismatch
non-consumption, and the natural-first-forced no-op control (forcing the natural
first candidate reproduces the baseline replay exactly); (12) prefix-resume
regression — a natural cutoff scenario plus a forced slot-1 candidate that fails
low must re-search the buffered prefix (buffer depth == forced ordinal, pops in
[1, k]) under deterministic depth/ordinal scans. Python suite:
`tools/policy_research/tests/test_sandbox.py` (4 test methods, exercised by
`python3 -m unittest`) adds the armed-vs-unarmed
bit-identical live search comparison, dataset lifecycle row checks, collection
gate diagnostics (missing log path, movetime, `Threads=2`), and the new
enumerate-command output with optional depth token and stage names.

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
  (standing gate unchanged). Full test suite: 102 tests green.

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
   - Emits per-attempt aspiration telemetry (`info string research root_telemetry ... attempts <depth>:<val>:<res>:<alpha>:<beta>:<attempt_nodes> ...`).
   - Disambiguates cumulative effort from target-iteration effort increments $\Delta E_D(m) = E_D(m) - E_{D-1}(m)$ by snapshotting and looking up effort by move identity (`Move`), resolving the vector-reordering sorting defect so move incremental efforts sum directly to target-iteration search nodes minus search root-level overhead: $\sum_m \Delta E_D(m) \le \Delta N_D$, with `root_overhead_nodes = max(0, incremental_nodes - sum(move_incremental_efforts))` explicitly recorded.
3. **Common-Prefix Causal Decomposition (Full 2³ Factorial Design: Root Order vs PV-Follow vs Aspiration Depth Control)**:
   Added research options `PolicyResearchPreserveAspiration`, `PolicyResearchDisableFailHighReduction`, `PolicyResearchPreservePreviousPV`, and `PolicyResearchAblationDepth`, depth-gated to the intervention target depth so that depths 1..D-1 run standard Stockfish search identically. Across all conditions, depths 1..D-1 achieve common-prefix state equivalence (`prev_depth_nodes` is 100% identical: 3,671 on `c3-v-001`, 22,499 on `c3-d-004`, 16,131 on `c3-d-002`, 63,292 on `c3-d-007`).
   
   To evaluate how coupled search mechanisms interact with first-slot move ordering, the causal decomposition evaluates an orthogonal $2^3$ factorial design across 8 forced intervention conditions plus an untreated 9th baseline reference (schema version 4 in `causal-decomposition.json`). Root-order forcing is active across all 8 factorial conditions; the three experimental factors (Aspiration preservation, PV-follow decoupling, and Fail-High depth reduction disabling) assess sensitivity and mechanism interaction *conditional on the forced move treatment*:
   - Condition 1 (`baseline`): Untreated baseline search (all mechanisms operate naturally without forcing).
   - Condition 2 (`joint_intervention`): Forced candidate into `rootMoves[0]`, simultaneously updating root ordering, setting `lastIterationIdxPV = candidate`, and enabling normal aspiration window adjustments and fail-high depth reductions.
   - Condition 3 (`preserve_aspiration`): Forced move with baseline aspiration window center and width (asp=1, pv=0, fhr=0).
   - Condition 4 (`preserve_previous_pv`): Forced move with baseline previous PV reference maintained (`lastIterationIdxPV = baselinePreviousPV`; asp=0, pv=1, fhr=0).
   - Condition 5 (`disable_fail_high_reduction`): Forced move at unreduced nominal depth (disabling `failedHighCnt` reductions; asp=0, pv=0, fhr=1).
   - Condition 6 (`preserve_asp_and_pv`): Forced move with baseline aspiration AND baseline previous PV reference maintained (asp=1, pv=1, fhr=0).
   - Condition 7 (`preserve_asp_and_fhr`): Forced move with baseline aspiration AND unreduced nominal depth (asp=1, pv=0, fhr=1).
   - Condition 8 (`preserve_pv_and_fhr`): Forced move with baseline previous PV reference maintained AND unreduced nominal depth (asp=0, pv=1, fhr=1).
   - Condition 9 (`full_control_nominal_depth`): Forced move with baseline aspiration center, baseline previous PV reference maintained, AND unreduced nominal depth (full orthogonal control; asp=1, pv=1, fhr=1).

   *Architectural Mechanisms Explained*:
   - **Aspiration-Optimism Coupling**: In Stockfish (`Search::Worker::iterative_deepening`), aspiration window bounds and player optimism are coupled: Stockfish sets both initial aspiration center and optimism from `avg = rootMoves[pvIdx].averageScore`, computing `optimism[us] = 114 * avg / (abs(avg) + 85)`. Under the unconstrained joint intervention (Condition 2), forcing a move resets `avg = rootMoves[0].averageScore`, altering both the window center and the optimism evaluation scaling. Preserving baseline aspiration (Conditions 3, 6, 7, 9) locks both the window center and the player optimism to the baseline value.
   - **PV-Follow Decoupling**: Downstream PV-following in Stockfish requires `(ss - 1)->currentMove == lastIterationIdxPV[0]` at ply 1 to maintain `ss->followPV = true`. Setting `lastIterationIdxPV = baselinePreviousPV` (Conditions 4, 6, 8, 9) ensures that when the candidate move differs from the baseline previous PV leader, `ss->followPV` immediately evaluates to `false` at ply 1. This decouples candidate search from the previous PV line through intentional mismatch, suppressing candidate-biased PV tracking without mutating root move ordering (it does *not* set `rootMoves[0].pv` to baseline PV).

   *Empirical Factorial Findings (Canonical Schema v4)*:
   | Root | Condition | Nodes | Incr D14 | Cumul % | Qual vs Ref | Score |
   |---|---|---:|---:|---:|:---:|---:|
   | `c3-v-001` (b1c3, ref d4c5: 642 cp) | 1_baseline | 20,956 | 17,285 | 0.00% | PASS | 654 |
   | | 2_joint_intervention | 5,643 | 1,972 | +73.07% | PASS | 642 |
   | | 3_preserve_aspiration | 14,450 | 10,779 | +31.05% | PASS | 667 |
   | | 4_preserve_previous_pv | 5,232 | 1,561 | +75.03% | PASS | 688 |
   | | 5_disable_fail_high_reduction | 9,293 | 5,622 | +55.65% | PASS | 637 |
   | | 6_preserve_asp_and_pv | 13,263 | 9,592 | +36.71% | PASS | 651 |
   | | 7_preserve_asp_and_fhr | 14,450 | 10,779 | +31.05% | PASS | 667 |
   | | 8_preserve_pv_and_fhr | 11,667 | 7,996 | +44.33% | PASS | 679 |
   | | 9_full_control_nominal_depth | 13,263 | 9,592 | +36.71% | PASS | 651 |
   | `c3-d-004` (g1h1, ref c4c5: -692 cp) | 1_baseline | 69,878 | 47,379 | 0.00% | PASS | -647 |
   | | 2_joint_intervention | 24,972 | 2,473 | +64.26% | PASS | -654 |
   | | 3_preserve_aspiration | 24,596 | 2,097 | +64.80% | FAIL_GATE | -628 |
   | | 4_preserve_previous_pv | 25,410 | 2,911 | +63.64% | PASS | -654 |
   | | 5_disable_fail_high_reduction | 24,972 | 2,473 | +64.26% | PASS | -654 |
   | | 6_preserve_asp_and_pv | 24,186 | 1,687 | +65.39% | FAIL_GATE | -628 |
   | | 7_preserve_asp_and_fhr | 24,596 | 2,097 | +64.80% | FAIL_GATE | -628 |
   | | 8_preserve_pv_and_fhr | 25,410 | 2,911 | +63.64% | PASS | -654 |
   | | 9_full_control_nominal_depth | 24,186 | 1,687 | +65.39% | FAIL_GATE | -628 |
   | `c3-d-002` (e1g1, ref e2a6: -89 cp) | 1_baseline | 25,490 | 9,359 | 0.00% | PASS | -43 |
   | | 2_joint_intervention | 18,176 | 2,045 | +28.69% | PASS | -87 |
   | | 3_preserve_aspiration | 26,456 | 10,325 | -3.79% | PASS | -54 |
   | | 4_preserve_previous_pv | 18,634 | 2,503 | +26.90% | PASS | -60 |
   | | 5_disable_fail_high_reduction | 32,602 | 16,471 | -27.90% | FAIL_GATE | -21 |
   | | 6_preserve_asp_and_pv | 37,666 | 21,535 | -47.77% | PASS | -56 |
   | | 7_preserve_asp_and_fhr | 78,030 | 61,899 | -206.12% | PASS | -56 |
   | | 8_preserve_pv_and_fhr | 25,659 | 9,528 | -0.66% | PASS | -56 |
   | | 9_full_control_nominal_depth | 74,594 | 58,463 | -192.64% | PASS | -63 |
   | `c3-d-007` (a2a3, ref c1e3: 33 cp) | 1_baseline | 129,556 | 66,264 | 0.00% | FAIL_GATE | 21 |
   | | 2_joint_intervention | 69,237 | 5,945 | +46.56% | PASS | 37 |
   | | 3_preserve_aspiration | 105,409 | 42,117 | +18.64% | PASS | 37 |
   | | 4_preserve_previous_pv | 70,346 | 7,054 | +45.70% | PASS | 52 |
   | | 5_disable_fail_high_reduction | 84,044 | 20,752 | +35.13% | PASS | 50 |
   | | 6_preserve_asp_and_pv | 104,537 | 41,245 | +19.31% | FAIL_GATE | 38 |
   | | 7_preserve_asp_and_fhr | 105,409 | 42,117 | +18.64% | PASS | 37 |
   | | 8_preserve_pv_and_fhr | 93,111 | 29,819 | +28.13% | FAIL_GATE | 38 |
   | | 9_full_control_nominal_depth | 85,508 | 22,216 | +34.00% | PASS | 38 |

   *Causal Insights & Quality Gate Clarifications*:
   - On `c3-d-004`: While all forced conditions reduce nodes substantially (~64–65% cumulative savings), conditions that preserve baseline aspiration (Conditions 3, 6, 7, 9) finish with score -628 cp. Relative to the D18 reference score of -692 cp, $|\Delta| = 64\text{ cp} > 50\text{ cp}$, which fails the strict quality gate (`quality_valid_ref: false`). Conversely, conditions without baseline aspiration preservation (Conditions 2, 4, 5, 8) finish with score -654 cp ($|\Delta| = 38\text{ cp} \le 50\text{ cp}$), passing the quality gate. Condition 4 (`preserve_previous_pv`) saves **63.64% cumulative / 93.86% incremental nodes** (25,410 vs 69,878 nodes) while passing all quality criteria, demonstrating that significant node reductions persist when candidate PV-following is decoupled.
   - On `c3-v-001`: Under full orthogonal controls at nominal depth 14 with candidate PV-follow decoupled and fail-high reduction disabled (Condition 9), the intervention saves 36.71% cumulative / 44.51% incremental nodes (13,263 vs 20,956 nodes) while strictly passing quality validation. The unconstrained joint intervention (Condition 2) saves 73.07% cumulative (5,643 nodes at D11) by compounding with fail-high depth reductions. This demonstrates that substantial gross savings (~36.7%) survive without fail-high depth reductions or candidate PV-follow, while interacting with fail-high depth control to produce the headline 73% joint saving.
   - On `c3-d-002` (Kiwipete): Disabling fail-high reductions in this tactically volatile root causes severe search inflation (Condition 7 inflates nodes to 78,030 vs 25,490 baseline; Condition 9 inflates to 74,594), confirming that fail-high depth reductions are essential for controlling alpha-beta branching factor in complex tactics, and showing that node impacts are strongly root-dependent.
   - *Baseline vs Factorial Comparison*: Baseline (Condition 1) is an untreated reference search where all three mechanisms operate naturally without forcing. Comparing Condition 9 to baseline bundles first-slot move ordering with whatever residual search-path differences exist, rather than isolating an orthogonal move-ordering main effect independent of the forcing context.
   - Forcing the **D-1 lead move** (`d_minus_1_lead_move`) is the exact byte-for-byte no-op control (e.g. on `c3-d-007`: baseline 129,556 nodes == forced `c1e3` 129,556 nodes). Forcing the untreated final best move (`f2f3`) is an active intervention (66,704 nodes) because `f2f3` was not the leader at the start of iteration D14.
4. **Balanced Latin-Square Multi-Trial Timing Benchmark**:
   - Implemented a cyclic shift Latin-square design across trials in `p4_force_first.py` (`--trials 12`). It achieves exact positional balance across trial slots when `trials` is a multiple of the candidate count (e.g. 4 candidates with 12 trials gives exactly 3 trials per slot; for 5 candidates like `c3-v-008`, 12 trials yields `[3, 2, 2, 2, 3]`). This design mitigates first-runner cache warming and positional bias, though cyclic schedules do not balance pairwise carryover effects.
   - Distinct Timing Definitions & Reconciled Results (Canonical 12-Trial Evidence in `timing-benchmark-trials12.json`):
     * **Search-Kernel Time Reduction Percentage**: $(T_{\text{base}} - T_{\text{cand}}) / T_{\text{base}}$, representing the isolated search-kernel speed gain inside the alpha-beta search loop.
     * **Search-Kernel Multiplicative Speedup Factor**: $T_{\text{base}} / T_{\text{cand}}$.
     * **Wall Time Reduction Percentage**: $(W_{\text{base}} - W_{\text{cand}}) / W_{\text{base}}$, reflecting total end-to-end execution including process startup, UCI handshake, and ~300 ms fixed memory allocation.
     * **Scope Boundary**: Search-kernel timings measure Stockfish's internal search loop (including Stockfish's native NNUE evaluation and MovePicker generation/scoring), but exclude any *future policy network inference and policy move scoring/sorting overhead*.
   - On `c3-v-001` (12 counterbalanced runs):
     * Baseline search time: median 19.0 ms (IQR 0.0 ms, mean 18.9±0.5 ms), wall time median 325.4 ms.
     * Candidate `b1c3` search time: median 5.0 ms (IQR 1.0 ms, mean 5.4±0.5 ms), wall time median 311.7 ms.
     * **Search-kernel time reduction: 73.7%** (a **3.80× speedup factor**). Wall time reduction: 4.2%.
   - On `c3-d-004` (12 counterbalanced runs):
     * Baseline search time: median 65.5 ms (IQR 3.0 ms, mean 65.6±2.0 ms), wall time median 371.3 ms.
     * Candidate `g1h1` search time: median 23.0 ms (IQR 1.0 ms, mean 23.3±1.1 ms), wall time median 329.1 ms.
     * **Search-kernel time reduction: 64.9%** (a **2.85× speedup factor**). Wall time reduction: 11.4%.
5. **Full 18-Root Population Depth Ladder (D12, D14, D16 with Fixed D10 Candidates)**:
   Evaluated the full 18-root development and validation corpus across depths 12, 14, 16 using a fixed candidate shortlist generated at D10 (`depth-ladder-dev-val.json` via dedicated runner `p4_depth_ladder.py`).
   *Methodology note*: In early exploratory runs, candidate lists at depth $D$ included the depth-$D$ untreated final best move. Purging same-depth hindsight injection and using strictly fixed D10 candidate sets (with the $D-1$ leader as the sole exact no-op control) confirms that headline opportunities are robust (9/18 roots positive, 22.1% pooled cumulative savings vs 22.4% with hindsight injection; e.g. on `c3-d-007` strict candidate `a2a3` yields +46.6% savings vs +48.5% with `f2f3`).
   - Positive opportunity rate expands across the ladder: **5/18 (27.8%) at D12**, **9/18 (50.0%) at D14**, and **12/18 (66.7%) at D16**.
   - Pooled cumulative savings increase across the ladder: **12.1% at D12** (254,354 $\to$ 223,530 nodes), **22.1% at D14** (908,892 $\to$ 707,917 nodes), and **36.9% at D16** (5,060,198 $\to$ 3,194,088 nodes).
   - *Savings Concentration & Heterogeneity*: At D16, two roots (`c3-d-004` saving 1.10M nodes and `c3-d-006` saving 0.38M nodes) account for 79.2% of all pooled savings across the 18 roots; the macro median saving is 13.6% (compared to 36.9% pooled). This demonstrates that depth scaling is heterogeneous and position-dependent across tactical/endgame structures, rather than uniform monotonic scaling across the entire population.
   - Key roots show persistent positive scaling: `c3-d-004` (+50.6% D12 $\to$ +64.3% D14 $\to$ +80.6% D16), `c3-d-002` (+25.8% D12 $\to$ +28.7% D14 $\to$ +44.9% D16), `c3-v-005` (+49.4% D12 $\to$ +3.6% D14 $\to$ +34.3% D16).
   - Reveals depth-dependent phase transitions: `c3-v-001` (0% D12 $\to$ +73.1% D14 $\to$ +44.4% D16), `c3-d-006` (0% D12 $\to$ +18.7% D14 $\to$ +82.7% D16).
   - Demonstrates stable baseline-optimality on mate-in-2 tacticals: `c3-d-009` (0.0% at all plies).
6. **18-Root Canonical Population Distribution (`corpus/v3` Dev + Val, Schema v4)**:
   *Framing & Generalization Notice*: These statistics represent oracle headroom on the 18 curated test and benchmark roots of `corpus-v3` under an empirical search-informed candidate shortlist. They demonstrate mechanistic feasibility and existence of causal ordering leverage in alpha-beta search, not a deployable policy or population-wide Elo gain. Test roots `c3-t-001`..`c3-t-008` remain strictly quarantined.
   - **Abstaining Oracle Definition**: The 22.1% pooled cumulative (42.3% target-iteration) savings metric represents an *abstaining oracle* that intervenes only when an evaluated candidate passes quality gates and reduces search cost below baseline, and otherwise abstains (reverting to baseline cost and regret 0.0), even when the untreated baseline itself misses the reference quality gate (as in `c3-v-003` and `c3-v-004`). It does not represent the cost of an oracle constrained to achieve quality-validity on every root.
   - **7 Result-preserving savings** (38.9%): preserves baseline best move, beats baseline cost.
   - **2 Convergence corrections** (11.1%): corrects baseline suboptimal move to reference best move, beats baseline cost (`c3-d-007`, `c3-v-006`).
   - **6 Baseline optimal** (33.3%): baseline is already the best/cheapest move (regret 0.0; including `c3-d-009` mate in 2).
   - **2 Harmful interventions** (11.1%): candidate matches reference, but costs strictly more nodes than baseline (`c3-v-003`, `c3-v-004`).
   - **1 No valid candidate** (5.6%): no candidate matches reference within score tolerance (`c3-d-005`).
   - Total positive opportunities: **9 / 18 (50.0%)**.
   - Macro mean savings: **18.3% cumulative, 34.6% target-iteration**.
   - Macro median savings: **1.8% cumulative, 7.8% target-iteration**.
   - Pooled savings across 18 roots: **22.1% cumulative** (908,892 $\to$ 707,917 nodes), **42.3% target-iteration** (475,490 $\to$ 274,515 nodes).

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
