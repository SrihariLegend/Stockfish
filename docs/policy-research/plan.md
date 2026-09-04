# Implementation Plan: Incremental Alpha-Beta Proof Policy for Stockfish

This plan is an implementation specification for a coding model that must **not be expected to infer missing design decisions**. Work must be completed in phases. Do not skip phases, combine unrelated phases, or modify production search behavior before the required measurements exist.

The project begins as a research and instrumentation project. A production policy network is not implemented until the oracle gap, interaction gap, and Jacobian signal have been measured.

---

# 1. Project objective

Build and evaluate a fast move-ordering policy for alpha-beta search that predicts which candidate move is most likely to produce the required bound at the lowest search cost.

The initial deployed policy will only reorder moves. It will not modify:

- Search depth.
- LMR reductions.
- Pruning conditions.
- Search windows.
- Extensions.
- Quiescence search.
- TT replacement.
- Time management.

The long-term formulation is:

```
priority(m)
  ~  P(required bound is proved within budget | m)
     /  E[compute consumed within budget | m]
```

The first production student will directly predict a ranking score approximating:

```
S(m) ~ log q(m) - log e(m)
```

where:

- `q(m)` is cutoff/useful-bound probability.
- `e(m)` is expected search cost.
- Both are conditional on position, window, depth, search context, and policy version.

Auxiliary teacher heads will predict cutoff outcome and proof-time distributions. The production engine does not initially need to calculate calibrated probabilities.

---

# 2. Non-negotiable engineering rules

## 2.1 Do not guess the current Stockfish architecture

Before editing search code, inspect and document:

- Exact search entry points.
- Search templates and node-type representation.
- MovePicker stages.
- Where baseline move scores are assigned.
- TT API and ownership.
- Worker-local mutable histories.
- Search-stack layout.
- Position make/unmake flow.
- NNUE accumulator representation.
- NNUE feature indexing and king-bucket behavior.
- Build and testing commands.

Do not assume symbol names from another Stockfish version.

## 2.2 Research code must not affect production builds

All expensive instrumentation and counterfactual machinery must be behind a compile-time research flag, e.g.:

```cpp
#ifdef POLICY_RESEARCH
```

Use the project's actual build conventions if a more appropriate mechanism exists.

When research support is disabled:

- No research branches may execute.
- No logging state may be allocated.
- Search results and node counts must remain unchanged.
- Production performance must not regress measurably.
- Research-only UCI options should not appear unless intentionally retained.

## 2.3 Never label unsearched moves as failures

If a cutoff occurs before a move is searched:

- Its outcome is unknown.
- Its cost is unknown.
- It must not receive a negative cutoff label.
- It must not receive a zero-cost label.
- It must be marked `UNOBSERVED`.

## 2.4 Do not silently use a cold TT as a realistic counterfactual

Distinguish:

1. Cold-TT cost.
2. Common-prestate force-next cost.
3. Sequential shared-TT cost.

Every dataset record must identify which protocol generated it.

## 2.5 Do not optimize top-1 move accuracy as the primary metric

Primary metrics are:

- Search-cost regret.
- Cycles or wall time at fixed search quality.
- Nodes at fixed depth.
- Root-result agreement.
- Elo at fixed time, eventually.

Best-move and cutoff top-1 accuracy are diagnostic metrics only.

## 2.6 Do not modify LMR until ordering-only evaluation passes

LMR data may be logged early. LMR behavior must remain unchanged until the ordering model:

- Reduces fixed-depth wall time.
- Does not introduce unacceptable result disagreement.
- Passes controlled engine testing.

## 2.7 All data must be versioned

Every record must identify:

- Engine commit.
- Research schema version.
- Value-network checksum.
- Policy version.
- Compiler/build configuration.
- Root corpus version.
- Search configuration.
- TT protocol.
- Random seed.

---

# 3. Precise terminology

## 3.1 Root ply versus remaining depth

Store both:

- `ply_from_root`: distance from root.
- `remaining_depth`: current search depth in the engine's native depth units.
- `root_iteration_depth`: current iterative-deepening target.

Do not call all three "depth".

## 3.2 Node versus decision point

The policy acts at a **move-selection decision point**, not merely once per node.

A decision point is the moment when search must choose the next move among a remaining candidate set.

Examples:

- Initial move selection.
- Selection after a TT move failed.
- Selection after a good capture failed.
- Selection after alpha was raised.
- Entry into the quiet-move stage.

A single search node may contain several relevant decision points because the state changes after each attempted move.

Every policy training record must therefore include:

- Node identity.
- Decision index within the node.
- Moves already attempted.
- Current alpha and beta.
- Current move count.
- Current MovePicker stage.
- Remaining candidates.

## 3.3 Required bound

For a null-window/non-PV decision, a useful result is normally:

```
returned score >= beta
```

Encode all scores from a documented perspective, preferably the current node's side-to-move/negamax perspective.

Never mix child and parent score signs.

## 3.4 Cost

Record at least:

- Recursive nodes searched.
- Whether the search was budget-censored.
- Re-search count.
- Returned bound.
- Optional elapsed cycles/time for rich samples.

Use node count as the initial stable label. Use wall time for final deployment evaluation.

## 3.5 Policy-relative cost

Record the policy used inside every counterfactual subtree:

```
C^pi_k(s, m)
```

Do not combine costs generated under materially different policy versions without explicitly handling that difference.

---

# 4. Repository structure and required documentation

Use the repository's conventions, but create an equivalent structure to:

```text
docs/
  policy-research/
    overview.md
    architecture-inventory.md
    search-mutation-audit.md
    data-schema.md
    experiment-protocols.md
    results-template.md

src/
  policy_research/
    recorder.*
    serialization.*
    sampling.*
    counterfactual.*
    tt_overlay.*
    worker_snapshot.*
    policy_features.*
    policy_accumulator.*
    policy_model.*
    policy_loader.*

tools/
  policy_research/
    run_corpus.py
    decode_log.py
    validate_log.py
    build_dataset.py
    analyze_observational.py
    analyze_oracle_gap.py
    analyze_interaction_gap.py
    analyze_calibration.py
    analyze_jacobian.py
    train_baselines.py
    train_teacher.py
    train_student.py
    export_student.py
    benchmark_policy.py
    requirements.txt
```

Do not create all source files immediately. Create them only in the relevant phase.

Raw datasets, trained models, and large logs must not be committed. Commit:

- Tiny test fixtures.
- Schemas.
- Scripts.
- Checksums and manifests.
- Reproduction instructions.

---

# 5. Phase 0: architecture inventory and mutation audit

No search behavior changes are allowed in this phase.

## 5.1 Identify exact code locations

Create `architecture-inventory.md` with:

### Search

- Main root search function.
- Recursive PV search function.
- Recursive non-PV search function.
- Quiescence search.
- Search node-type representation.
- Where alpha and beta are modified.
- Where reductions are selected.
- Where re-searches occur.
- Where cutoffs are detected.
- Where search nodes are counted.

### MovePicker

- MovePicker class/file.
- Every move stage in exact order.
- How TT moves are handled.
- Capture scoring.
- Quiet scoring.
- Promotion handling.
- Evasion handling.
- Duplicate suppression.
- Whether all moves are generated eagerly or lazily.
- Whether constructing/draining a second MovePicker mutates global state.

### Mutable search state

List every object that recursive search may mutate:

- TT.
- History tables.
- Continuation history.
- Capture history.
- Correction histories.
- Search stack.
- Worker node counters.
- Selective depth.
- Root move data.
- Stop flags.
- Time-manager state.
- Pawn/material/evaluation caches if present.
- Tablebase state.
- Thread-local fields.
- Any static mutable fields.

For each field record:

- Owner.
- Type.
- Size.
- Whether it must be cloned, restored, or disabled during counterfactual search.

### Position state

Document:

- `do_move`.
- `undo_move`.
- Null move.
- Castling representation.
- En passant.
- Promotions.
- Position key.
- StateInfo or equivalent lifetime.
- NNUE accumulator attachment.

### TT

Document:

- Probe API.
- Save/write API.
- Cluster/index calculation.
- Generation handling.
- Replacement algorithm.
- Prefetch calls.
- Whether search receives a concrete TT reference or accesses a global.
- Whether a research adapter can be introduced without affecting production hot paths.

### NNUE

Document:

- Input feature set.
- Accumulator dimensions.
- Perspective handling.
- King bucket behavior.
- Activation functions.
- Value-output layers.
- Existing SIMD kernels.
- Whether accumulators are materialized at every node or lazily.
- What happens on king moves.
- How feature deltas are represented.

## 5.2 Deliverables

- `architecture-inventory.md`
- `search-mutation-audit.md`
- A proposed exact integration-point diagram.
- A list of unresolved architecture questions.

## 5.3 Exit gate

Do not continue until every mutable object reachable from recursive search has been accounted for.

---

# 6. Phase 1: deterministic research harness

Purpose: create reproducible search runs before logging internal data.

## 6.1 Initial research configuration

Use:

- One thread.
- Fixed hash size.
- Fixed search depth, not time.
- MultiPV 1.
- Ponder off.
- No external stop commands.
- Fixed value network.
- Fixed executable.
- Fixed root order.
- Tablebases disabled initially unless the corpus specifically tests them.
- Fresh process per root for the strictest initial experiments.

A fresh process per root is intentionally conservative. Later optimize throughput only after reproducibility is proven.

## 6.2 Root corpus

Create a versioned root corpus manifest containing:

- Stable root ID.
- FEN/EPD.
- Source/license information.
- Optional tags:
  - opening/middlegame/endgame,
  - tactical/quiet,
  - side to move,
  - material class,
  - in check,
  - tablebase eligibility.

Use at least three logical sets:

1. Development set.
2. Validation set.
3. Permanently held-out test set.

Split by root position or source game, never by internal node.

If positions come from games, all positions from one game must remain in one split.

## 6.3 Run manifest

Each run writes a manifest with:

- Run UUID.
- Timestamp.
- Engine commit.
- Dirty-working-tree status.
- Compiler and version.
- Compile flags.
- CPU model.
- Operating system.
- Thread count.
- Hash size.
- Value-network path and checksum.
- Policy-network path/checksum, if any.
- Corpus checksum.
- Search depth.
- All relevant UCI options.
- Random seed.
- Research schema version.

## 6.4 Determinism test

Run the same corpus twice with identical settings.

The following must match exactly:

- Best move.
- Root score.
- Root bound where applicable.
- Node count.
- Principal variation if deterministic.
- Internal sampled record identities once logging exists.

Wall time and cycle count are excluded from exact matching.

## 6.5 Exit gate

Do not proceed if repeated single-thread fixed-depth runs produce unexplained node-count differences.

---

# 7. Phase 2: versioned research logging

## 7.1 Research options

Add research-only options equivalent to:

```text
PolicyResearch
PolicyResearchMode
PolicyResearchLogPath
PolicyResearchSeed
PolicyResearchSampleRate
PolicyResearchMaxRecords
PolicyResearchTopK
PolicyResearchNodeBudget
PolicyResearchPolicyVersion
```

Suggested modes:

```text
off
observational
root_counterfactual
internal_counterfactual
shared_permutation
jacobian_dump
reduction_shadow
```

Names may be adapted to repository style, but all semantics must be documented.

## 7.2 Logging architecture

Do not format JSON or write files in the hot recursive path.

Use:

1. In-memory record collection.
2. Explicit maximum memory/record count.
3. Flush after root search or at controlled synchronization points.
4. Length-prefixed binary serialization.
5. Offline decoder to JSONL or Parquet.

Suggested binary framing:

```text
File header:
  magic bytes
  schema version
  endian marker
  engine commit length + bytes
  run UUID

Record header:
  record type: uint16
  record schema: uint16
  payload length: uint32

Payload:
  explicitly serialized fields
```

Do not dump native C++ structs directly because of padding, ABI, and endianness.

Required record types:

- `RUN_START`
- `ROOT_START`
- `DECISION_POINT`
- `MOVE_ATTEMPT`
- `COUNTERFACTUAL_RESULT`
- `ROOT_END`
- `RUN_END`
- `ERROR_RECORD`

## 7.3 Deterministic sampling

Sampling must not use mutable global random state.

Compute a hash from:

- Root ID.
- Position key.
- Ply.
- Remaining depth.
- Decision index.
- Research seed.
- Optional MovePicker stage.

Example conceptual form:

```
h = mix(rootId, positionKey, ply, depth, decisionIndex, seed)
```

Sample when:

```
h mod 1,000,000 < configured threshold
```

Support stratified rates by:

- Remaining-depth bucket.
- Ply bucket.
- PV/non-PV.
- In-check status.
- Move stage.
- TT move available.
- Candidate count once known.

## 7.4 Decision-point record

Each `DECISION_POINT` must contain:

### Identity

- Run ID.
- Root ID.
- Node serial.
- Parent node serial if available.
- Decision serial.
- Decision index within node.
- Position key.
- Optional full position only for rich samples.

### Search context

- Ply from root.
- Remaining depth in native units.
- Root iteration depth.
- Node type.
- PV flag.
- In-check flag.
- Improving flag if defined.
- Cut-node expectation if represented.
- Move count already searched.
- MovePicker stage.
- Current alpha.
- Current beta.
- Original node alpha.
- Static evaluation.
- Static-evaluation-valid flag.
- Current best score.
- Window width.
- `beta - staticEval`.
- `staticEval - alpha`.
- Aspiration expansion count if available.
- Previous iteration score if available.
- TT-hit state.
- Whether TT move was legal.
- Whether TT move was already tried.
- Whether alpha has already been raised.
- Parent and grandparent move encodings.
- Rule-50 count.
- Repetition-related state needed for features.

Store raw engine score values. Normalize only offline.

### Candidate set

For every remaining candidate:

- Canonical move encoding.
- UCI move string in decoded output.
- Baseline rank.
- MovePicker stage.
- Baseline total score.
- Individual history components where accessible.
- Mover piece type.
- From square.
- To square.
- Move category:
  - quiet,
  - capture,
  - en passant,
  - castling,
  - promotion,
  - evasion.
- Captured piece type.
- Promotion type.
- SEE bucket/value if already available.
- Gives-check flag only if available cheaply and without making the move.
- Recapture relation.
- Baseline planned reduction if known at the decision point.
- Whether the move is the TT move.
- Whether it was generated but later skipped.

All feature values must be available before the candidate is searched.

## 7.5 Move-attempt record

For every attempted move at a sampled decision/node:

- Candidate move ID.
- Actual attempt rank.
- Alpha before attempt.
- Beta before attempt.
- Search depth passed to child.
- Reduction used.
- Initial window.
- Re-search count.
- Re-search depths/windows.
- Nodes before and after.
- Cost in nodes.
- Optional elapsed cycles/time.
- Returned score.
- Returned bound:
  - lower,
  - upper,
  - exact.
- Whether alpha was raised.
- Whether beta cutoff occurred.
- Whether search was stopped.
- Whether result was budget-censored.
- Whether TT hit occurred in the child if measurable.
- Policy version used inside the subtree.

## 7.6 Candidate enumeration

For sampled rich records, enumerate all legal remaining candidates without changing search behavior.

Rules:

- Use a separate MovePicker instance if this is read-only.
- Verify by audit that draining it does not mutate histories or search state.
- Preserve actual baseline ordering.
- Do not generate candidates twice in production builds.
- Record candidates that would remain unsearched after a cutoff as `UNOBSERVED`.

## 7.7 Logging validation

Add a decoder and validator that checks:

- Record lengths.
- Schema version.
- Move encoding validity.
- Candidate IDs referenced by attempts exist.
- No move has both `UNOBSERVED` and an outcome.
- Alpha/beta values are valid.
- Node costs are nonnegative.
- Decision indices are monotonic within a node.
- Policy version is present.
- Root manifests match binary logs.

## 7.8 Exit gate

With observational logging enabled but no interventions:

- Best move, score, PV, and node count must match a logging-disabled research build.
- The same deterministic samples must be selected across repeated runs.
- Logs must decode without errors.
- No unsearched move may be labeled negative.

---

# 8. Phase 3: observational dataset and calibration baseline

Do this before counterfactual search.

## 8.1 Dataset construction

Build one row per candidate decision, not merely one row per node.

Statuses:

```text
OBSERVED_FAIL_HIGH
OBSERVED_FAIL_LOW
OBSERVED_ALPHA_RAISE
OBSERVED_EXACT
UNOBSERVED_AFTER_CUTOFF
SEARCH_ABORTED
BUDGET_CENSORED
INVALID_OR_SKIPPED
```

Do not collapse these prematurely.

Schema note (fixes the Phase 2 review P1): with the committed
`research-data/1` records, Phase 3 collection can build behavior-conditioned
rows only for **actually attempted quiet moves** (per sampled node: the node's
DECISION_POINT plus its MOVE_ATTEMPTs). Derivable now: `OBSERVED_FAIL_HIGH`,
`OBSERVED_FAIL_LOW` (exact negative event; NonPV null-window nodes cannot raise
alpha without a cutoff), and a single `ABORTED_STOP` status for outcome 3 —
outcome 3 records no stop reason, so `SEARCH_ABORTED` and `BUDGET_CENSORED`
cannot be separated with `research-data/1`. Candidate-set
statuses (`UNOBSERVED_AFTER_CUTOFF`, `INVALID_OR_SKIPPED`) and full-window
observations (`OBSERVED_ALPHA_RAISE`, `OBSERVED_EXACT`) need a schema extension
with candidate enumeration and MovePicker baseline features; that is deferred
to the counterfactual phases (see experiment-protocols.md P3.x). Until then,
history/baseline-score calibration (§8.3) cannot be grounded in `research-data/1`
alone.

Status — **Protocol P3.2 executed (2026-09-04)**: the P3.1 dataset v1 was
reviewed and found methodologically flawed for its claims (row-random split
across shared trees, ~28.9% exact iterative-deepening prefix duplicates, row-
level SEs over only 12 roots, non-random cell exclusion, collinear margins,
in-sample isotonic fit). Dataset v2 collects with
`tools/policy_research/p3_dataset.py collect` one fresh-process run per corpus
root at fixed depth 20 and `PolicyResearchSampleRate 0.5` (uniform across
roots, seed 101, policy `baseline-observational-v1`) of the research build of
`c9878d11`: **12 runs, 2 135 633 DECISION_POINT rows, 5 607 088 MOVE_ATTEMPT
rows** (~2 min wall). Prefix-free by construction (a deeper run replays
shallower iterations byte-identically, so only the deepest run per root is
collected) and no excluded cell — `c1-d-004@20` fits at rate 0.5 (3 103 654
records < the engine's hard cap). No ABORTED_STOP rows occurred. Artifacts and
the baseline report live under `tools/policy_research/runs/
policy-research-corpus-v1-d20-h16-rate0.5-dataset-20260904T014612/`
(manifest schema `research-dataset/1`, git-ignored). The superseded 11 GB v1
dataset directory was removed (its numbers are preserved in
experiment-protocols.md Protocol P3.1).

## 8.2 Initial observational analyses

Produce reports for:

- Cutoff rank distribution.
- Nodes spent before cutoff.
- Cost by move class.
- Cost by remaining depth.
- Cost by ply.
- Cost by node type.
- Cost by static-eval/beta margin.
- Cost by window width.
- Re-search rates.
- Reduction rates.
- TT-move success rate.
- Existing history-score calibration.
- Existing baseline-score calibration.

## 8.3 History calibration

Using only legitimately observed/interventional labels:

- Fit logistic or isotonic mappings from history score to cutoff probability.
- Fit separately by:
  - remaining-depth bucket,
  - node type,
  - move class,
  - MovePicker stage,
  - beta-margin bucket.

Report:

- Reliability diagrams.
- Brier score.
- Negative log-likelihood.
- Expected calibration error.
- Cost-weighted ranking accuracy.

Do not treat the observational calibration result as unbiased for unsearched moves. Label it explicitly as behavior-policy-conditioned.

Status — the P3.2 baseline report covers every §8.2 item derivable from
`research-data/1`: status distribution (cutoff/fail-low/aborted), cutoff rate
and fail-low cost by quiet ordinal, remaining depth, ply, margin, and node-
type flags, re-search rate, and TT-move success — every table with pooled
numbers plus between-root macro columns (roots, not rows, are the sample
unit). A node-level section joins attempts to their decision nodes (share of
nodes ending in a quiet cutoff, cutoff-ordinal distribution, wasted-before-cut
costs). `window` is 1 by construction at sampled nodes. Reduction rates are not
derivable (no LMR fields in `research-data/1`; LMR shadow modeling is Phase
13). Cutoff calibration is grouped by corpus set: logistic (ridge on
standardized features, unpenalized intercept) fit on development-set roots,
PAV-isotonic fit on validation-set roots, all metrics evaluated on test-set
roots only (n = 3, 847 222 completed attempts): full-model AUC 0.936 (macro
0.932 ± 0.011); isotonic gives a small honest out-of-sample gain (Brier 0.0605
→ 0.0598, ECE10 0.0195 → 0.0176). All numbers are explicitly
behavior-policy-conditioned. A follow-up review of the v2 report was
addressed in report schema `research-baseline-report/3` (tool/docs only,
no engine change): a within-node reordering probe shows the 0.936 test AUC
does not imply move-ordering quality (pair accuracy 0.029 over 34 933
late-cutoff predecessor pairs on the test roots), root-balanced fits are
reported alongside the row-weighted fits because one development root
supplies 67.5% of the fitting rows (no canonical probability map is claimed
with 3 validation roots), reliability tables expose the worst-bin |gap| that
pooled ECE10 0.0176 hides, pooled rate tables and both calibration
objectives honor per-row node_weight (IPW) when datasets are non-uniform,
an observational opportunity-accounting block sizes the (modest) reordering
headroom (wasted-before-cut 12.3% vs no-quiet-cut loop cost 87.7% of
fail-low local cost), and provenance records the research binary's
banner-embedded commit plus the report tool commit. §8.3 history
calibration and baseline-score calibration require MovePicker baseline
features and candidate enumeration (counterfactual schema). A larger root
sample (corpus/v2) is the prerequisite before strong generalization claims.

## 8.4 Cheap baselines

Implement offline baselines:

1. Existing MovePicker score.
2. Calibrated history score.
3. Logistic regression on cheap features.
4. Additive generalized model.
5. Cutoff-only classifier.
6. Cost-only regressor.
7. Direct pairwise ranking model on observed cutoff-versus-predecessor pairs.

For a cutoff at rank `k`, create preferences:

```
m_k > m_i,  i < k
```

Weight each pair by a capped function of wasted cost:

```
w_i = min(C_i, B_pair)
```

Do not infer preferences against unsearched moves.

## 8.5 Exit gate

Produce a reproducible report establishing the quality and calibration of existing heuristics. Do not claim policy improvement from observational data alone.

---

# 9. Phase 4: root-level counterfactual experiments

Implement root-level interventions before arbitrary internal-node interventions because root state is easier to reproduce safely.

## 9.1 Do not misuse `searchmoves`

Restricting the root to one move is not equivalent to placing that move first and then allowing the normal remaining moves.

Implement an explicit research-only root-order override:

- Candidate `m` is searched first.
- All remaining root moves remain available.
- Their relative baseline order is preserved.
- All ordinary root/PVS semantics remain intact.

## 9.2 Common initial state

For every candidate intervention:

- Start a fresh engine process, or perform a proven-complete research reset.
- Use the same root FEN.
- Use the same TT initialization protocol.
- Use the same history initialization protocol.
- Use the same network.
- Use the same fixed depth.
- Use the same candidate-independent options.

Initially prefer a fresh process per intervention.

## 9.3 Measurements

For each root candidate:

- Candidate forced first.
- Candidate first-probe cost.
- Whether it raised alpha.
- Whether it became best.
- Whether a re-search occurred.
- Total root search nodes.
- Total wall time.
- Final root score.
- Final best move.
- Reference agreement.

## 9.4 Reference result

Because Stockfish search is selective, ordering can change the returned value.

Establish a reference using at least one of:

- Greater search depth.
- Reduced selectivity research mode.
- Repeated stable deeper search.
- Tablebase truth where available.

Do not call the cheapest run "oracle" if it returns a materially incorrect result relative to the reference.

## 9.5 Root oracle metrics

For root `r`:

```
C_base(r)      = baseline total cost
C_oracle(r)    = min over eligible m of C(r | m first)
R(r)           = C_base(r) - C_oracle(r)
R_norm(r)      = R(r) / C_base(r)
```

Report aggregate savings using root-level bootstrap confidence intervals.

## 9.6 Exit gate

Produce an oracle-gap report. If force-first ordering offers no meaningful wall-time opportunity, stop before implementing a neural policy.

Do not choose a universal pass threshold in code. Report:

- Total weighted node opportunity.
- Total weighted wall-time opportunity.
- Implied maximum affordable policy cycles per invocation.

---

**Status — P4.1 review remediation landed (engine work + enhanced tooling).**
§9.1's override exists as a research-only unit with two distinct experimental
modes addressing the review findings:
1. `PolicyResearchForceFirstMove` (UCI string, default empty): named legal root
   move to search first; inert when empty or in Observational mode.
2. `PolicyResearchForceFirstDepth` (UCI spin, 0..256, default 0):
   - `0`: persistent schedule (Experiment A), overriding at every root iteration.
   - `> 0`: isolated target depth (Experiment B), overriding ONLY when
     `rootDepth == PolicyResearchForceFirstDepth`. Depths 1..D-1 run under
     standard baseline conditions, giving every candidate intervention identical
     TT and history state at the decision boundary. Forcing the baseline's own
     best move at depth D is an exact no-op in deterministic fields (nodes,
     score, bestmove, PV).
3. MovePicker root semantics: Stockfish sets `ttData.move = rootMoves[0]` and
   passes it to `MovePicker`, which emits it first in `MAIN_TT`. All subsequent
   root moves are generated and ordered by `MovePicker`'s normal stage progression
   (captures, quiets scored by history).
4. Tablebase safety: overrides check `rootMoves[i].tbRank == rootMoves[0].tbRank`
   to preserve Syzygy contiguous rank grouping (`pvFirst`..`pvLast`).

Tooling & quality evaluation (`tools/policy_research/p4_force_first.py`):
- Verifies engine banner and research option presence at startup (fails fast
  if run against macro-off binaries).
- Explicitly sets `Threads 1`, `Hash 16`, `MultiPV 1`.
- Token-based UCI parsing with depth-reached assertion; records wall time (ms),
  nodes, score (cp/mate and bound), bestmove, and PV.
- Captures depth-(D-1) node baseline to report both cumulative nodes and
  incremental depth-D nodes ($\Delta N_D$).
- Reference search at D16 (plan §9.4): evaluates best-move agreement and score
  tolerance (<= 50 cp) against deeper reference; verifies mate score signs;
  distinguishes mild drift (<= 100 cp) from score collapse (> 100 cp).
- Dual-baseline score comparison: records candidate deltas against both the
  same-depth baseline ($\Delta\text{base}$) and the deeper reference ($\Delta\text{ref}$).
- Multi-band tolerance sensitivity analysis: tests gates $\pm 25, \pm 50, \pm 75, \pm 100\text{ cp}$
  to reveal continuous cost-quality tradeoffs.
- Disentangles node-optimal candidate from time-optimal candidate; notes that
  single-run millisecond times are subject to scheduling jitter.
- Canonical artifacts recorded under
  `tools/policy_research/runs/policy-research-p4-kickoff-d14-h16-20260904/`
  with schema `policy-research-p4-counterfactual/2`.

Pilot comparison (depth 14, candidate shortlist = depth 10 MultiPV top 4):
- **Isolated Mode (Experiment B)**:
  - `c1-d-001` (startpos): baseline best `e2e4` is an exact no-op (43,275 nodes,
    2,680 incremental D14 nodes) under stable PV iterations. Other candidates
    cost more (+78% to +95% incremental regret). `R_norm (valid) = +0.000`
    across all tolerance gates ($\pm 25$ to $\pm 100$ cp).
  - `c1-v-001` (validation): baseline `d4c5` = 20,956 nodes (17,285 incremental).
    Forcing `b1c3` first = 5,643 cumulative nodes (1,972 incremental), score cp 642
    (agrees within 3 cp of D16 reference 645 cp). All candidates agree with
    reference best `d4c5`. Quality-valid oracle headroom on this root:
    **73.1% cumulative nodes (`R_norm = +0.731`), 88.6% incremental D14 nodes
    (`R_norm = +0.886`)**, invariant across all gates $\pm 25$ to $\pm 100$ cp.
  - `c1-t-001` (burned test root): baseline `f1e2` = 26,292 nodes (7,742 incremental).
    Sensitivity analysis shows a frontier: at $\pm 50$ cp only baseline best is valid
    ($R_{\text{norm}} = 0.000$); at $\pm 75$ cp candidate `f1g2` passes
    ($R_{\text{norm}} = +0.171$ cumul, $+0.580$ incr, $-18$ cp vs base, $-53$ cp vs ref);
    `f3d1` experienced severe collapse ($-111$ cp vs ref) and fails all gates.
- **Persistent Mode (Experiment A)**:
  - `c1-d-001`: `c2c4` = 21,146 nodes (`R_norm = +0.511`) due to iterative
    trajectory churn.
  - `c1-v-001`: `b1c3` = 1,600 nodes, but score drifted by +74 cp (fails quality).
  - `c1-t-001`: `f3d1` exploded to 320,781 nodes (+1120%) with score 0 cp.
  - Incremental counts in persistent mode reflect divergent trajectory steps.

**P4.2 Status — expanded corpus v3, causal decomposition, and canonical evaluation complete.**
- Delivered audited and immutable `tools/policy_research/corpora/corpus-v3.json` (26 positions:
  10 development `c3-d-001`..`c3-d-010`, 8 validation `c3-v-001`..`c3-v-008`, 8 unburned test roots `c3-t-001`..`c3-t-008`, with 100% byte-for-byte FEN replay verification).
- Move-identity telemetry attribution: snapshotting and lookup of `RootMove::effort` by move identity (`Move`) ensures move incremental efforts sum directly to target-iteration search nodes minus root-level search overhead: $\sum_m \Delta E_D(m) \le \Delta N_D$, with `root_overhead_nodes = max(0, incremental_nodes - sum(move_incremental_efforts))` explicitly recorded.
- Depth-gated causal decomposition across 7 experimental conditions with verified common prefix (`prev_depth_nodes` 100% byte-for-byte identical across all conditions):
  - Added Condition 4 (`PolicyResearchPreservePreviousPV`) which keeps the baseline leader's previous PV intact down the tree, decoupling pure root first-slot move order from PV-following state.
  - Added Condition 7 (`full_control_nominal_depth`) which simultaneously enforces baseline aspiration center, retains previous PV, and disables fail-high depth reduction (full orthogonal control).
  - On `c3-d-004` (CPW4, tactical middlegame): Condition 4 saves **63.64% cumulative / 93.86% incremental nodes** (25,410 vs 69,878 nodes), and Condition 7 saves **65.39% cumulative / 96.44% incremental nodes** (24,186 vs 69,878 nodes), proving that root first-slot move order dominates over PV-following and aspiration depth control down the tree.
  - On `c3-v-001` (Giuoco Piano blunder): pure move ordering at nominal depth 14 saves 31.05% cumulative / 37.64% target-iteration nodes (14,450 vs 20,956); preserving baseline previous PV saves 75.03% cumulative / 90.97% incremental nodes (5,232 nodes); under full control (Condition 7), cumulative savings are 36.71% (13,263 vs 20,956 nodes); joint intervention with fail-high depth reduction saves 73.07% cumulative (5,643 nodes at D11).
  - On `c3-d-002` (Kiwipete): fail-high depth reductions control search explosion in high-branching tactical positions (forcing nominal D14 inflates nodes from 18k to 32k or 78k).
  - On `c3-d-007` (strict candidate `a2a3`): joint intervention saves 46.56% cumulative (69,237 vs 129,556 nodes); under full control (Condition 7), savings are 34.00% cumulative (85,508 vs 129,556 nodes).
  - Disproved that forcing baseline final best is an exact no-op: forcing the **D-1 lead move** (`d_minus_1_lead_move`) is the exact byte-for-byte no-op control (e.g. `c3-d-007`: baseline 129,556 nodes == forced `c1e3` 129,556 nodes). Forcing untreated final best `f2f3` is an active intervention (66,704 nodes) because `f2f3` was not the leader at the start of iteration D14.
- Executed balanced Latin-square 12-trial counterbalanced timing benchmark:
  - On `c3-v-001`: engine median search time dropped from 18.0 ms (IQR 1.0 ms, mean 18.33±0.49 ms) to 5.0 ms (IQR 1.0 ms, mean 5.33±0.49 ms), representing a **72.2% median engine search time reduction** (a **3.60× speedup factor**).
  - On `c3-d-004`: engine median search time dropped from 62.0 ms (IQR 1.0 ms, mean 62.33±0.98 ms) to 22.0 ms (IQR 1.0 ms, mean 22.50±1.17 ms), representing a **64.5% median engine search time reduction** (a **2.82× speedup factor**).
- Full 18-root population depth ladder evaluated across D12, D14, D16 with fixed D10 candidates (`depth-ladder-dev-val.json`):
  - Positive opportunity expands with depth: **5/18 (27.8%) at D12**, **9/18 (50.0%) at D14**, and **12/18 (66.7%) at D16**.
  - Pooled cumulative savings increase monotonically: **12.1% at D12** (254,354 $\to$ 223,530 nodes), **22.1% at D14** (908,892 $\to$ 707,917 nodes), and **36.9% at D16** (5,060,198 $\to$ 3,194,088 nodes).
  - Confirms persistent positive scaling on key roots: `c3-d-004` (+50.6% D12 $\to$ +64.3% D14 $\to$ +80.6% D16), `c3-d-002` (+25.8% D12 $\to$ +28.7% D14 $\to$ +44.9% D16), `c3-v-005` (+49.4% D12 $\to$ +3.6% D14 $\to$ +34.3% D16).
  - Depth-dependent phase transitions: `c3-v-001` (0% D12 $\to$ +73.1% D14 $\to$ +44.4% D16), `c3-d-006` (0% D12 $\to$ +18.7% D14 $\to$ +82.7% D16).
- Canonical 18-root evaluation on `corpus-v3.json` (test roots quarantined, Schema v4):
  - 7 Result-preserving savings (38.9%)
  - 2 Convergence corrections (11.1%) (`c3-d-007`, `c3-v-006`)
  - 6 Baseline optimal (33.3%) (including `c3-d-009` mate in 2)
  - 2 Harmful interventions (11.1%) (`c3-v-003`, `c3-v-004`)
  - 1 No valid candidate (5.6%) (`c3-d-005`)
  - Macro mean savings: 18.3% cumulative, 34.6% target-iteration.
  - Pooled savings across 18 roots: 22.1% cumulative (908,892 $\to$ 707,917 nodes), 42.3% target-iteration (475,490 $\to$ 274,515 nodes).
- Standing zero-regression gate re-verified: macro-off `bench 16 1 10 default depth`
  = **453,169** nodes. Full test suite: 102 tests green.

---

# 10. Phase 5: internal counterfactual search sandbox

Most difficult engineering phase. Implement only after the mutation audit.

## 10.1 Target causal question

At an internal decision state `h_t`, estimate:

```
C(m | h_t, choose m next, pi_k)
```

The candidate must receive the exact search treatment associated with the next available slot:

- Current move count.
- Current window.
- Current alpha.
- Current reduction rules.
- Current MovePicker stage.
- Current policy `pi_k` in descendants.

## 10.2 Disable recursive instrumentation

Use a scoped guard such as:

```cpp
ScopedShadowProbe
```

While a shadow probe is active:

- Do not recursively sample.
- Do not recursively launch counterfactual probes.
- Do not write ordinary telemetry unless specifically requested.
- Do not modify the real search's stop/time state.

## 10.3 Worker-state isolation

Create an explicit research snapshot or cloned worker.

The snapshot must cover every mutable object identified in Phase 0, including:

- All history tables.
- Correction histories.
- Continuation structures.
- Node counters.
- Selective depth.
- Search-stack future frames.
- Root-local state reachable from the recursive search.
- Any search-local caches.
- Any random/exploration state.

Do not `memcpy` non-trivial C++ objects without proving it safe.

Preferred approach:

- Construct a dedicated research worker.
- Copy explicitly supported state into it.
- Let the shadow subtree mutate the research worker.
- Discard it after the intervention.

After every probe, assert in research builds that the real state still matches its pre-probe checksum:

- Position key.
- Side to move.
- Rule-50 state.
- Relevant search-stack frames.
- History checksum.
- Real TT generation.
- Root stop state.

## 10.4 Copy-on-write TT overlay

Implement a research-only TT view with:

- Immutable base TT representing the pre-decision state.
- Private copy-on-write clusters.

Correct semantics:

1. On probe:
   - If cluster exists in overlay, probe overlay cluster.
   - Otherwise probe immutable base cluster.

2. On first write to a cluster:
   - Copy the entire corresponding base cluster into the overlay.
   - Apply the normal TT replacement/write algorithm to the copied cluster.

3. Subsequent reads/writes use the overlay cluster.

4. Discard overlay after the intervention.

This preserves:

- Base TT hits.
- Existing bounds.
- Existing generation information.
- Within-subtree TT writes.
- Cluster replacement semantics.

It prevents:

- Candidate `m_1` from contaminating candidate `m_2`.

The overlay may use a sparse map indexed by TT cluster for research correctness. Its cycle cost must not be treated as production TT cost. Use it primarily for node-count labels.

Do not introduce a virtual TT call into production hot paths. Use:

- Research-only templating,
- compile-time dispatch,
- or a separately compiled research search path.

## 10.5 Candidate selection

Counterfactual probing all moves at all nodes is too expensive.

Initial rule:

- If remaining candidate count is 2-8: probe all.
- If greater than 8:
  - Include baseline top four.
  - Include two deterministic uniformly sampled candidates.
  - Allow up to configured `TopK`.
- Skip candidate sets smaller than two.
- Store the candidate-selection probability.

Initially focus on:

- Single-thread.
- Non-PV/null-window decision points.
- Fixed-depth searches.
- No tablebase nodes.
- No externally stopped searches.
- A documented remaining-depth range.
- Quiet-move stage or another single stage selected in advance.

Do not silently mix MovePicker stages in the first dataset.

## 10.6 Two measurements per candidate

Where feasible, record both:

### A. Candidate probe result

Search candidate as the next move and record:

- Fail-high/fail-low.
- Nodes spent in the candidate.
- Re-searches.
- Bound.
- Reduction.
- Budget censoring.

This supports `q/e` modeling.

### B. Total remaining node cost if chosen next

After a candidate fails, continue the decision/node with baseline policy over remaining candidates and record total cost until node completion.

This directly estimates action regret and captures some downstream effects.

The second measurement is the stronger target for a direct priority head.

## 10.7 Budgeting

Every shadow probe must have a configurable node budget.

When budget expires:

- Abort only the shadow probe.
- Mark it `BUDGET_CENSORED`.
- Preserve the real search.
- Store nodes consumed before censoring.
- Do not label the move fail-low.

Use logarithmic budget experiments rather than one arbitrary permanent cap.

## 10.8 Exit gate

Before collecting a large dataset, verify on a small suite:

- Real search results and node counts are unchanged by shadow probes.
- Candidate evaluation order does not affect independent probe node counts.
- Base TT hits are preserved.
- Overlay writes remain private.
- Worker state restoration passes checksums.
- Position state is unchanged.
- Censored probes are labeled correctly.

---

# 11. Phase 6: oracle, ratio, and interaction-gap studies

Do not train a policy network before these reports exist.

## 11.1 Force-next oracle

For each fully counterfactually evaluated decision:

```
C*(s) = min over m of C(s | m next)
```

subject to the candidate producing an acceptable result relative to the reference protocol.

Baseline regret:

```
R_base(s) = C_base(s) - C*(s)
```

Policy regret:

```
R_pi(s) = C_pi(s) - C*(s)
```

Oracle-regret capture:

```
Capture(pi) = (C_base - C_pi) / (C_base - C*)
```

Compute this by summing costs before division, not by naively averaging per-node percentages.

## 11.2 Ratio explanatory study

Compare candidate orderings based on:

1. Baseline MovePicker.
2. Best-move probability.
3. Cutoff probability `q`.
4. Expected cost `e`.
5. `q/e`.
6. Direct empirical force-next cost.
7. Budgeted proof index.
8. Jacobian score later.

For complete candidate sets, simulate sequential search cost under each order using measured outcomes, while clearly noting where independence is assumed.

## 11.3 Interaction gap

For selected top-`K` candidate sets, run:

- Baseline order.
- `q/e` order.
- Reverse baseline top candidates.
- Adjacent swaps.
- Deterministic Gumbel perturbations.
- A small beam search over permutations if affordable.

Each permutation must:

- Start from the same common pre-decision state.
- Retain TT writes between candidates within the permutation.
- Use a separate overlay/state clone from other permutations.

Measure:

```
interaction gap = C_shared(scalar-index order) - C_shared(best tested permutation)
```

If this gap is large, the final direct ranking target must receive greater weight than separate `q/e` heads.

## 11.4 Statistical treatment

Search-node observations from the same root are correlated.

Use:

- Root-level or game-level bootstrap.
- Confidence intervals grouped by root.
- Separate reports by depth, ply, stage, and node type.
- Both mean and tail statistics.
- Capped and uncapped costs.

Do not report millions of internal nodes as millions of independent samples.

## 11.5 Go/no-go decision

Before neural work, answer:

1. How much total cost can an oracle save?
2. Which contexts contain the opportunity?
3. How much of the oracle is explained by `q/e`?
4. How large is the TT/order interaction gap?
5. How many cycles per policy call can be afforded?

If the affordable policy budget is below even a small table-based scorer, stop or restrict invocation to high-regret contexts.

---

# 12. Phase 7: proof-time survival modeling

## 12.1 Target representation

For each attempted candidate, define:

- `T`: nodes until the candidate returns.
- `Y = +`: fail-high/useful cutoff.
- `Y = -`: fail-low/no cutoff.
- `CENSORED`: probe exceeded budget.

Predict discrete competing-risk distributions over logarithmic node bins.

Suggested bins are configurable powers of two, e.g.:

```text
1-2
3-4
5-8
9-16
...
up to the largest useful budget
```

Do not hardcode a maximum without examining observed distributions.

Outputs:

```
F+_m(t) = P(T <= t, Y = +)
F-_m(t) = P(T <= t, Y = -)
S_m(t)  = P(T > t)
```

For budget `B`:

```
q_B(m) = F+_m(B)
e_B(m) = integral_0^B S_m(t) dt
I_B(m) = q_B(m) / e_B(m)
```

## 12.2 Censoring rules

- Completed fail-high: exact event time and positive cause.
- Completed fail-low: exact event time and negative cause.
- Budget-aborted candidate: right-censored.
- Never-attempted candidate after another cutoff: missing/unobserved, not survival-censored.
- Search globally stopped: exclude or represent with a separate stop censoring reason.

## 12.3 Cost heads

Train auxiliary heads for:

- Cutoff probability.
- Conditional fail-high cost.
- Conditional fail-low cost.
- Proof-time hazards.
- Direct force-next ranking.

Do not rely solely on MSE of log node count. It predicts the wrong quantity for arithmetic expected cost under heavy tails.

## 12.4 Budget selection

Evaluate several budget policies:

- Fixed global budget.
- Budget by remaining-depth bucket.
- Budget by ply bucket.
- Budget proportional to baseline predicted cost.
- Budget based on a percentile of historical cost in that context.

The production student may receive a budget/context bucket rather than an arbitrary continuous budget.

---

# 13. Phase 8: exact and prototype Jacobian experiments

## 13.1 Exact Jacobian baseline

Implement research-only calculation of:

```
g(P) = grad_A V(A(P))
```

Do not assume this is cheap. Measure:

- Reverse-pass operations.
- Required saved activations.
- Memory traffic.
- Cost relative to a normal value evaluation.

For each candidate move, calculate:

```
S_J(P, m) = g(P)^T delta_A(m)
```

Handle explicitly:

- Ordinary moves.
- Captures.
- En passant.
- Promotions.
- Castling.
- Perspective change.
- King-bucket change.
- Accumulator refresh.

If a move cannot be represented by a valid local delta under the current NNUE architecture, mark it unsupported. Do not silently apply an invalid approximation.

## 13.2 Jacobian evaluation

Measure correlation with:

- Child static-evaluation change.
- Cutoff outcome.
- Candidate proof cost.
- Force-next total cost.
- Oracle ordering.
- Regret capture.

Report separately for:

- Quiet moves.
- Captures.
- King moves.
- Promotions.
- Different remaining depths.
- Different beta margins.

## 13.3 Prototype compression

Cluster sampled gradients:

```
g(P) ~ g_k(P)
```

For each prototype and NNUE input feature `f`, precompute:

```
H_k(f) = g_k^T W_f
```

Then score a move using feature-table additions/subtractions.

## 13.4 Cheap router

Train and benchmark a router using only already-available node information:

- Quantized NNUE activations.
- Small accumulator projection.
- Evaluation bucket.
- Phase/material bucket.
- King buckets.
- Activation-mask signature.
- Search context if useful.

Test:

- Hard one-prototype selection.
- Top-two mixture.
- Small soft mixture.

The router must not require calculating the exact gradient.

## 13.5 Jacobian exit gate

Proceed with a production prototype only if:

- Exact Jacobian captures meaningful oracle regret.
- Prototype compression preserves most of that ranking signal.
- Router plus lookup scoring fits the measured cycle budget.

If it fails, retain Jacobian score only as an offline teacher feature.

---

# 14. Phase 9: teacher model

The teacher is not constrained by production inference cost.

## 14.1 Training splits

Split by root/game ID:

- Train.
- Validation.
- Test.

Deduplicate repeated positions or transpositions across splits using position keys plus relevant search context.

Never split candidate rows from one decision point across train and validation/test.

## 14.2 Teacher inputs

### Position inputs

The teacher may use a richer board representation than the student:

- Piece-square occupancy.
- Side to move.
- Castling rights.
- En passant.
- King locations.
- Material.
- Attack information if generated consistently.

### Search-context inputs

Include explicitly:

- Ply from root.
- Remaining depth.
- Root iteration depth.
- PV/non-PV.
- MovePicker stage.
- Move count.
- Alpha.
- Beta.
- Window width.
- Beta-static-eval margin.
- Static-eval-alpha margin.
- Static-eval-valid flag.
- In check.
- Improving.
- Previous alpha raise.
- TT state.
- Previous iteration score.
- Aspiration expansion state.
- Parent/grandparent move.
- Planned reduction.
- Policy version used to generate labels.
- TT protocol.

### Candidate inputs

- Mover type.
- From/to.
- Capture type.
- Promotion.
- SEE.
- Check.
- Recapture.
- Baseline score.
- Individual history features.
- Baseline rank.
- Move stage.
- Jacobian score if available.
- Prototype-Jacobian score if available.

No feature may depend on the candidate's future search result.

## 14.3 Teacher losses

Use:

```
L = lambda_r * L_direct_ranking
  + lambda_s * L_survival
  + lambda_q * L_cutoff
  + lambda_c * L_cost
  + lambda_d * L_distillation_consistency
```

### Direct ranking loss

For counterfactual candidates `i, j`:

- If both produce acceptable equivalent bounds, prefer lower total remaining cost.
- If one produces the required bound and the other does not, prefer the successful candidate.
- If result quality differs materially, use the reference search to avoid preferring a cheap wrong result.
- If both are censored without resolution, do not create a hard pair.

Pair weight:

```
w_ij = min(|C_i - C_j|, B_weight)
```

Normalize weights to prevent one huge subtree from dominating a batch.

### Listwise loss

When a decision has multiple fully evaluated candidates, also train a listwise target derived from measured force-next cost.

Keep all candidates from one decision in the same batch/group.

## 14.4 Teacher metrics

Report:

- Oracle-regret capture.
- Cost-weighted pair accuracy.
- Simulated total decision cost.
- Cutoff AUC and calibration.
- Survival calibration.
- Calibration by depth and beta margin.
- Performance by move stage.
- Tail-cost performance.
- Performance on held-out roots.
- Performance under newer policy versions.

---

# 15. Phase 10: incremental production student

## 15.1 Student architecture v1

Use a separate tiny policy accumulator rather than requiring the value NNUE accumulator to be materialized at every node.

Initial dimension candidates:

```text
16
32
64
```

Benchmark all three.

Maintain two perspective accumulators:

```
Z_white(P), Z_black(P)
```

Each accumulator is a sum of quantized piece-square embeddings indexed by:

- Perspective-relative piece color.
- Piece type.
- Perspective-relative square.

Version 1 must not use king buckets. This avoids full refresh on king moves and guarantees sparse updates.

At a node, select the accumulator corresponding to side to move.

## 15.2 Incremental updates

On `do_move`, update both perspective accumulators for:

- Mover removed from source.
- Mover added to destination.
- Captured piece removed.
- En passant capture square.
- Promotion: remove pawn, add promoted piece.
- Castling: move king and rook.
- Null move: no piece embedding changes; side-to-move selection changes.

Store the resulting accumulator with the position state/ply so undo restores the previous accumulator naturally.

Add a research assertion mode:

- Recompute accumulator from the board.
- Compare to incremental accumulator.
- Run on random positions and every move type.

## 15.3 Candidate move embedding

Use a canonical side-to-move orientation.

Candidate embedding components may include:

- Mover type/from/to.
- Promotion type.
- Captured type.
- Move category.
- Recapture relation.

Avoid an uncontrolled Cartesian product that creates a huge random-access table.

A practical v1:

```
U(m) = U_fromto[mover, from, to]
     + U_promotion[type]
     + U_capture[captured type]
     + U_movekind[kind]
```

Construct the small vector locally, then perform one dot product.

## 15.4 Context modulation

Construct one context-adjusted position vector per decision:

```
Z_c = Z(P)
    + C_depth
    + C_ply
    + C_node type
    + C_window
    + C_beta-margin
    + C_stage
    + C_move-count
```

Then:

```
S_neural(m) = Z_c^T U(m) + b(m, c) + w^T phi(m, c)
```

This gives context x move interactions without separate large networks.

Use bucketed context features with documented boundaries. Derive bucket boundaries from data, then freeze them in the model schema.

## 15.5 Cheap scalar candidate features

Include quantized/clipped versions of:

- Existing total history score.
- Continuation history.
- Capture history.
- SEE bucket.
- Gives check.
- Recapture.
- Baseline rank.
- Planned reduction.
- Promotion type.
- TT-state context.
- Jacobian prototype score if retained.

Every value must be available before candidate search.

## 15.6 Runtime output

The initial runtime model emits one direct priority score only.

Auxiliary `q`, cost, and survival heads remain in the teacher unless benchmarks show they are affordable and useful.

Combine with classical score using fixed-point arithmetic:

```
S_final = S_classical + ((S_policy * lambda_context) >> shift)
```

Document all score ranges and tie behavior.

If policy scores tie, preserve baseline ordering deterministically.

## 15.7 Quantization

Train float first, then use quantization-aware fine-tuning.

Specify in model file:

- Model format version.
- Feature-schema checksum.
- Dimension.
- Tensor shapes.
- Per-tensor scales.
- Zero points if used.
- Context bucket definitions.
- Score scale.
- Checksum.

Use:

- Int8 weights where safe.
- Int16 accumulators if proven safe.
- Int32 dot products and biases.
- Int64 reference implementation in tests.

Prove overflow bounds mathematically and with tests.

## 15.8 Model export tests

Export at least 1,000 golden examples containing:

- Position.
- Context.
- Candidates.
- Float score.
- Quantized reference score.
- Engine score.

Require:

- Exact match between offline quantized reference and scalar C++ implementation.
- Stable ranking within documented quantization tolerance.
- Checksum rejection for incompatible model/schema versions.

---

# 16. Phase 11: conservative engine integration

## 16.1 Initial deployment scope

Do not reorder every stage immediately.

Initial scope:

- Single-thread experiments first.
- Non-PV/null-window nodes.
- One selected MovePicker stage, preferably quiets.
- TT move behavior unchanged.
- Existing hard legality/SEE classifications unchanged.
- No quiescence.
- No in-check nodes unless separately validated.
- Configurable minimum remaining depth.
- Candidate count at least two.

The policy may reorder only within the selected existing stage.

## 16.2 Invocation gate

Add a cheap configurable gate based on:

- Remaining depth.
- Ply.
- Candidate count.
- Stage.
- Baseline score margin.
- TT move state.
- Beta margin.
- Node type.

Initially use hand-configured gates from the oracle-regret study.

Later train a value-of-computation gate predicting:

```
E[cycles saved] - policy cycles
```

## 16.3 Move scoring

Score each candidate once when the existing stage normally assigns scores.

Do not:

- Recompute the accumulator per candidate.
- Use a softmax.
- Allocate heap memory.
- Call external ML libraries.
- Perform a full sort if the existing picker uses partial selection.
- Change stage boundaries.

## 16.4 Scalar implementation first

Implement a scalar reference scorer before SIMD.

Tests must cover:

- Every move category.
- Every context bucket.
- Accumulator update/undo.
- Quantized model loading.
- Invalid model handling.
- Deterministic tie breaking.

Only add SIMD after scalar correctness and engine-level validation.

## 16.5 Benchmarking

Measure separately:

1. Accumulator update cycles per made move.
2. Context-vector construction cycles per decision.
3. Per-candidate score cycles.
4. Total MovePicker overhead.
5. NPS with policy disabled.
6. NPS with accumulator only.
7. NPS with scoring enabled.
8. Fixed-depth nodes.
9. Fixed-depth wall time.

Use both microbenchmarks and complete searches. Do not infer engine speed from microbenchmarks alone.

## 16.6 Engine-level acceptance

A model is not accepted based solely on fewer nodes.

Require:

- Reduced fixed-depth wall time on held-out corpus.
- Stable or improved root-result agreement.
- No crashes or illegal moves.
- No unacceptable tactical regression.
- No production slowdown when disabled.
- Positive Elo/SPRT result before default enablement.

---

# 17. Phase 12: on-policy data generation and DAgger

## 17.1 Policy versioning

Every generated dataset must specify:

- Behavior policy version.
- Value network version.
- Student checksum.
- Teacher checksum.
- Exploration configuration.

Do not train cost heads on a mixture of policy versions without including policy version or applying an explicit recency/importance strategy.

## 17.2 Iteration loop

For iteration `k`:

1. Freeze student `pi_k`.
2. Search the development corpus using `pi_k`.
3. Collect states actually visited by `pi_k`.
4. Run counterfactual teacher queries at sampled states.
5. Add results to the aggregated dataset.
6. Train teacher `T_{k+1}`.
7. Distill student candidate `pi_{k+1}`.
8. Evaluate `pi_{k+1}` on fixed held-out roots.
9. Deploy conservatively only if it passes.
10. Repeat.

## 17.3 Exploration

In offline research runs, use controlled exploration such as:

- Adjacent top-`K` swaps.
- Deterministic Gumbel perturbations.
- Force-next sampling.
- Teacher/student disagreement sampling.

Store the behavior probability or deterministic selection rule.

Never enable random policy exploration in production games.

## 17.4 Replay policy

Maintain:

- Recent on-policy data with highest weight.
- Older data with decaying weight.
- Permanent baseline anchors.
- Permanent held-out test set never used for training.
- Hard cases where student and teacher disagree.
- Rare move classes.

Do not let a huge quantity of easy shallow nodes dominate training.

## 17.5 Distribution-shift metrics

At each generation report:

- Feature-distribution drift.
- Node-type and depth distribution drift.
- Candidate-count drift.
- Calibration drift.
- Cost-prediction drift.
- Teacher/student disagreement.
- Performance on old and new policy distributions.

---

# 18. Phase 13: LMR shadow modeling

Only begin after ordering-only success.

## 18.1 No behavioral changes initially

Log or shadow-evaluate reduction alternatives without changing the real search.

For sampled candidate `m`, evaluate reductions:

```
r in {r_baseline - 1, r_baseline, r_baseline + 1}
```

Clamp to legal search-depth limits.

Do not start with a wide reduction range.

## 18.2 Reduction labels

Record for each `r`:

- Nodes.
- Returned bound.
- Cutoff.
- Re-search.
- Agreement with unreduced search.
- Agreement with deeper reference.
- Whether the parent result changes.
- Magnitude of score disagreement.

## 18.3 LMR objective

Estimate:

```
J(m, r) = E[C(m, r)] + lambda * E[L_search(m, r)]
```

where `L_search` penalizes missing an important move or changing the reference result.

Do not optimize node reduction without the quality penalty.

## 18.4 First live LMR deployment

If eventually enabled:

- Limit changes to at most one ply of reduction.
- Exclude PV nodes initially.
- Exclude checks, evasions, promotions, and other tactical safeguards initially.
- Require high model confidence.
- Preserve all existing hard LMR exceptions.
- Add immediate fallback option.
- Run dedicated Elo testing.

---

# 19. Required metrics and reports

Every experiment report must include the following.

## 19.1 Data description

- Number of roots.
- Number of decisions.
- Number of candidates.
- Number of fully counterfactually evaluated decisions.
- Depth/ply distribution.
- Move-stage distribution.
- Policy versions.
- Censoring rate.
- TT protocol.

## 19.2 Ranking metrics

- Cost-weighted pairwise accuracy.
- Top-1 cutoff accuracy.
- Top-3 cutoff recall.
- Oracle-regret capture.
- Simulated sequential cost.
- Direct force-next cost.
- Performance by context.

## 19.3 Calibration

- Reliability plots.
- Brier score.
- NLL.
- Calibration by remaining depth.
- Calibration by beta margin.
- Calibration by move stage.
- Survival calibration.

## 19.4 Search metrics

- Nodes.
- Wall time.
- NPS.
- Policy update cycles.
- Policy score cycles.
- Root score agreement.
- Best-move agreement.
- Tactical suite performance.
- Elo/SPRT when appropriate.

## 19.5 Statistical method

- Root-level bootstrap confidence intervals.
- Separate development and held-out results.
- No internal-node independence assumption.
- Explicit treatment of censored records.
- Both capped and uncapped cost summaries.

---

# 20. Required tests

## 20.1 Logging tests

- Binary encode/decode round trip.
- Truncated-file detection.
- Unknown-schema rejection.
- Move encoding round trip.
- Candidate/attempt referential integrity.
- No unobserved candidate receives outcome label.
- Deterministic sample selection.

## 20.2 Search-preservation tests

- Research disabled: identical node count and result.
- Observational logging: identical node count and result.
- Shadow probes: real search result unchanged.
- Probe candidate iteration order does not alter isolated probe results.
- Position key unchanged after probes.
- Worker-state checksum unchanged after probes.

## 20.3 TT overlay tests

For synthetic clusters:

- Base hit visible through overlay.
- Overlay write does not modify base.
- First write copies correct cluster.
- Replacement matches normal TT behavior.
- Overlay hit supersedes base.
- Discard restores exact base behavior.
- Generation handling matches base semantics.

## 20.4 Accumulator tests

For random legal positions and move sequences:

- Incremental accumulator equals recomputed accumulator.
- Undo restores prior accumulator.
- Null move correct.
- Castling correct.
- En passant correct.
- Promotions correct.
- Captures correct.
- Both perspectives correct.

## 20.5 Model tests

- Invalid checksum rejected.
- Invalid schema rejected.
- Scalar C++ equals offline quantized reference.
- SIMD equals scalar.
- No overflow on adversarial legal positions.
- Deterministic ties preserve baseline order.
- Missing policy file disables policy safely.

## 20.6 End-to-end tests

- Generate a tiny research log.
- Decode it.
- Build a tiny dataset.
- Train/export a toy model.
- Load it in engine.
- Produce expected golden scores.
- Run fixed-depth search with deterministic output.

---

# 21. Explicit failure and fallback rules

## 21.1 Low oracle gap

If oracle ordering cannot save enough cycles to pay for the smallest scorer:

- Stop all-node policy work.
- Restrict research to near-root or high-regret contexts.
- Consider TT/top-`K` caching rather than neural policy.

## 21.2 Large interaction gap

If shared-TT permutation results differ strongly from scalar `q/e`:

- Keep `q/e` as auxiliary supervision.
- Train direct ranking from permutation/force-next cost.
- Consider dynamic re-scoring after failed probes.
- Do not add pairwise runtime interactions until a cheap approximation is justified.

## 21.3 Weak Jacobian signal

If exact Jacobian does not capture proof-ordering regret:

- Do not implement a production prototype router.
- Retain Jacobian only as an offline feature or discard it.

## 21.4 Excessive policy overhead

If nodes decrease but wall time increases:

- Reduce accumulator dimension.
- Restrict invocation.
- Remove expensive candidate features.
- Use a table/GAM baseline.
- Introduce a value-of-computation gate.
- Do not claim success based on nodes alone.

## 21.5 Search-quality regression

If ordering changes cause reference disagreement or Elo loss:

- Restrict policy to safer stages.
- Lower blending strength.
- Exclude unstable contexts.
- Increase direct quality penalties in teacher labels.
- Keep LMR untouched.

## 21.6 Cost-target staleness

If a newer policy materially changes child cost distributions:

- Reduce old-data weight.
- Regenerate on-policy data.
- Retrain teacher before student.
- Do not use stale calibrated costs as current absolute quantities.

---

# 22. Recommended implementation sequence by commit

Each item should be a separate reviewable commit or small group of commits.

1. Add architecture inventory and mutation audit documents.
2. Add deterministic corpus runner and run manifest.
3. Add research compile flag and options with no behavior.
4. Add binary serializer and decoder with tests.
5. Add deterministic sampler.
6. Add observational decision-point logging.
7. Add move-attempt logging.
8. Add log validator and observational analysis.
9. Add history calibration baselines.
10. Add root-order override.
11. Add root oracle experiment runner.
12. Produce root oracle-gap report.
13. Add research worker snapshot/clone.
14. Add copy-on-write TT overlay.
15. Add internal shadow-probe guard.
16. Add force-next internal interventions.
17. Add interaction permutation protocol.
18. Produce oracle/ratio/interaction reports.
19. Add exact Jacobian research scorer.
20. Add Jacobian analysis and prototype clustering.
21. Add cheap prototype router experiment.
22. Add survival/direct-ranking teacher dataset.
23. Add teacher training and evaluation.
24. Add incremental student training/export.
25. Add scalar policy accumulator and scorer.
26. Add golden-vector and accumulator tests.
27. Integrate policy within one MovePicker stage.
28. Add microbenchmarks and fixed-depth evaluation.
29. Add SIMD only if required.
30. Add DAgger/on-policy iteration tooling.
31. Run Elo/SPRT.
32. Only then begin LMR shadow experiments.

---

# 23. Overall definition of done

The ordering-policy project is successful only when all of the following hold:

1. The oracle-gap study proves enough opportunity exists.
2. The interaction gap is measured and incorporated into the target design.
3. The Jacobian hypothesis has been tested rather than assumed.
4. Counterfactual data preserves a common pre-decision state.
5. Unsearched moves are handled as unknown.
6. Policy-relative cost data is regenerated on-policy.
7. The student uses sparse incremental updates.
8. Runtime scoring fits the measured cycle budget.
9. Fixed-depth wall time improves on held-out roots.
10. Search quality does not regress unacceptably.
11. The result survives engine-level Elo/SPRT testing.
12. LMR remains unchanged until ordering-only deployment succeeds.

The immediate task is **Phase 0 only**: inventory the actual Stockfish search, MovePicker, TT, worker state, and NNUE implementation, then produce the two architecture documents. No logging, model, or search modification should begin until that inventory has been reviewed.
