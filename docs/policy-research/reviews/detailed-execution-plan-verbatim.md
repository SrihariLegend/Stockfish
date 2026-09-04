The immediate next step is clear:

> Freeze Phase 4 as the successful root feasibility study and begin Phase 5: an exact internal-node counterfactual sandbox.

We should not train the production network yet, because we still need unbiased internal labels. But every next step should now point directly toward the generalized Policy-NNUE.

# 0. Freeze Phase 4

Do not reopen the root experiment unless Phase 5 exposes a concrete engine bug.

## Preserve

- Current implementation commits.
- Canonical artifacts.
- Macro-off benchmark: 453,169 nodes.
- Existing 102-test baseline.
- Three especially useful fixtures:
  - `c3-v-001`: strong joint effect;
  - `c3-d-004`: strong effect surviving PV preservation;
  - `c3-d-007`: exact D−1-leader no-op distinction.

## Frozen Phase 4 conclusion

> Isolated root move-order interventions have substantial deterministic search-cost effects. This passes the original Phase 4 gate and justifies internal-node investigation.

Known caveats remain documented, but they do not block Phase 5.

# 1. Define the first internal causal experiment precisely

Do not begin with every node type and every MovePicker stage.

## Initial scope

Sample only:

- non-root nodes;
- `NonPV`;
- null-window searches;
- main search, not qsearch;
- not in check;
- `excludedMove == Move::none()`;
- tablebases disabled;
- single thread;
- fixed depth;
- first decision in the quiet-move stage;
- configurable remaining-depth range, initially perhaps 4–10;
- at least two legal quiet candidates.

Keep:

- TT move behavior unchanged;
- capture stages unchanged;
- all normal pruning;
- all normal LMR;
- null move;
- ProbCut;
- singular extensions;
- history updates;
- descendants using baseline policy $\pi_0$.

The intervention occurs only when the target node reaches its first quiet-stage selection.

## Target estimand

For internal state $h$ and quiet candidate $m$:

$$
C^{\pi_0}(h,m)
=
\text{nodes required to resolve the node if }m
\text{ is the next quiet move, then baseline policy resumes}
$$

The candidate receives the normal treatment of that next slot:

- same move number;
- same alpha and beta;
- same effective depth;
- same pruning rules;
- same LMR formula;
- same pre-quiet TT/capture searches;
- same histories at node entry.

## Correctness criterion

At a null-window node, quality is primarily **required-bound equivalence**:

- If baseline returns `value >= beta`, candidate intervention must also prove the lower bound.
- If baseline returns `value < beta`, candidate intervention must preserve the non-cutoff result.
- Mate/TB semantics must agree.
- Large returned-score differences should be recorded and deeper-checked, but exact cp equality is not required for equivalent null-window bounds.

This is more appropriate internally than applying the root $\pm 50\text{ cp}$ criterion everywhere.

# 2. Implement a compile-time TT accessor

This should be the first substantial Phase 5 code change because all isolated search depends on it.

## Refactor

Create one search implementation parameterized by TT access:

```cpp
template<NodeType NT, typename TTAccess>
Value search_impl(..., TTAccess& ttAccess);
```

and similarly for qsearch.

Normal production search uses the real `TranspositionTable` or a zero-overhead forwarding adapter.

Research shadow search uses `ResearchTTOverlay`.

All recursive calls must preserve the same accessor.

## Required accessor API

```cpp
probe(Key)
generation()
first_entry(Key)
```

The returned writer must preserve:

```cpp
write(...)
penalize(...)
```

## Production constraints

- No virtual TT calls.
- No runtime research branch in macro-off builds.
- No copied second implementation of `search()`.
- Compiler should specialize the normal path.
- Macro-off benchmark remains exactly 453,169 nodes.
- Macro-on with research disabled must also retain the existing search signature.

# 3. Implement the copy-on-write TT overlay

Create something equivalent to:

```text
src/policy_research/tt_overlay.h
src/policy_research/tt_overlay.cpp
```

## Semantics

Each shadow probe sees:

- the real TT as an immutable base;
- a private map of copied 32-byte TT clusters;
- the normal replacement algorithm;
- private writes;
- private depth penalties;
- normal within-probe TT reuse.

On first write to cluster $i$:

1. Copy the complete base cluster.
2. Apply the normal writer operation to the private cluster.
3. Route all subsequent reads of that cluster to the private copy.

Candidate B receives a fresh overlay and must not see candidate A’s writes.

## Important implementation rule

Do not reimplement TT replacement semantics independently. Refactor or expose a research-gated internal cluster operation so both live TT and overlay use the same logic.

## Overlay tests

Test:

- base hit equivalence;
- base miss equivalence;
- replacement-candidate equivalence;
- generation handling;
- `TTWriter::write`;
- `TTWriter::penalize`;
- multiple writes to one private cluster;
- private writes visible within the same probe;
- private writes invisible to another probe;
- base TT checksum unchanged;
- candidate A/B order does not change results.

# 4. Build an isolated research worker

Create a dedicated research-only worker or sandbox owner rather than allowing shadow probes to mutate the real worker.

Likely components:

```text
src/policy_research/worker_snapshot.h
src/policy_research/worker_snapshot.cpp
src/policy_research/counterfactual.h
src/policy_research/counterfactual.cpp
```

## State that must be copied

From the real worker:

- `mainHistory`;
- `lowPlyHistory`;
- `captureHistory`;
- `continuationCorrectionHistory`;
- `ttMoveHistory`;
- isolated `continuationHistory`;
- isolated pawn history;
- isolated correction history;
- `nodes`;
- `tbHits`;
- `selDepth`;
- `nmpMinPly`;
- `optimism`;
- `rootDepth`;
- `rootDelta`;
- relevant root/PV state;
- accumulator stack;
- accumulator refresh table;
- reduction table;
- applicable options and network references.

The absolute source node count should be copied, not reset to zero. Stockfish’s draw-value behavior can depend on node count, so a zero-based shadow count could return a different draw value. Record probe cost as:

$$
N_{\text{after}}-N_{\text{before}}
$$

## Shared histories

The shadow worker must own isolated copies of:

- continuation history block;
- pawn history;
- correction history.

The real worker’s shared/atomic histories must never be updated by a probe.

Correctness is more important than speed initially. Copying tens of megabytes per sampled decision is acceptable for the first smoke test. Optimize only after exactness is established.

## Stack cloning

Build a shadow stack with enough preceding frames for all backward reads.

Stockfish reads up to `ss - 6`, so copy at least:

- the current frame;
- six previous frames;
- sufficient future scratch frames for recursive search.

Rebind:

- `continuationHistory` pointers into the shadow shared-history storage;
- `continuationCorrectionHistory` pointers into the shadow worker’s table;
- PV pointers into shadow-owned PV storage where needed.

Do not leave pointers referencing the real worker’s history tables.

## Position strategy

For the first implementation, a full `Position` clone may not be necessary.

Because probes execute synchronously with the real search paused, the shadow worker can search using the current `Position` provided that:

- every move is undone normally;
- candidate probes are sequential;
- position and `StateInfo` checksums are verified after each probe;
- the shadow uses its own stack and accumulator state;
- no hard abort bypasses normal undo paths.

Record before and after:

- position key;
- FEN or board checksum;
- side to move;
- rule-50 count;
- repetition state;
- current `StateInfo*`;
- accumulator stack size.

If this approach proves fragile, add an explicit research-only deep position/state-chain clone.

# 5. Add a scoped shadow-probe context

Create an RAII object such as:

```cpp
ScopedShadowProbe
```

Its context should contain:

```text
active
discoveryMode
targetPositionKey
targetPly
targetDepth
targetStage
forcedMove
interventionApplied
nodeBudget
budgetExceeded
targetAttemptTrace
candidateList
```

While active:

- ordinary research sampling is disabled;
- no nested shadow probes are started;
- normal search logic remains enabled;
- normal history and TT operations target isolated state;
- UCI output is suppressed;
- time management is suppressed;
- tablebases remain disabled;
- root stop state is never modified;
- only the private node budget may stop the probe.

The target intervention must be consumed once. A descendant transposition with the same position key must not accidentally receive the intervention.

Use a target invocation identity, not only a Zobrist key.

# 6. Add research-only MovePicker discovery and force-next support

The first policy action should be within one existing stage: quiets.

## Discovery mode

During a baseline shadow search, when the target node first enters its quiet stage:

- capture the generated and sorted quiet list;
- filter illegal/excluded moves appropriately;
- record baseline rank;
- record the exact next quiet slot;
- record the node count at stage entry;
- allow the baseline shadow to finish normally.

Candidate selection:

- 2–8 legal quiets: probe all.
- More than 8:
  - baseline top four;
  - two deterministic sampled alternatives;
  - record inclusion probability.
- Fewer than two: no counterfactual record.

Use the real MovePicker’s generated list. Do not construct a new independent MovePicker at a later state.

## Force-next mode

At the target node and target quiet-stage entry:

1. Return the forced candidate first.
2. Skip its normal later occurrence.
3. Preserve all other quiet moves in baseline relative order.
4. Leave TT moves, captures, evasions, and bad-capture stages unchanged.
5. Set `interventionApplied = true`.

If the requested candidate is not present in the target stage, fail the record rather than silently doing nothing.

# 7. Establish baseline-shadow exactness before probing alternatives

This is the most important Phase 5 milestone.

At a sampled internal node:

1. Pause the real search at node entry.
2. Snapshot the worker and stack.
3. Run a baseline shadow with no forced move.
4. Discard its overlay and isolated state.
5. Resume the real node normally.
6. Measure the real node’s local result and cost.
7. Compare the two.

Required equality:

- returned bound/value;
- node cost;
- first quiet-stage candidate list;
- first quiet move;
- target-stage entry node count;
- re-search count;
- cutoff outcome;
- relevant PV where applicable;
- target attempt trace.

If baseline shadow does not reproduce the real node exactly, do not run alternative candidates yet.

This test proves that:

- worker cloning is complete;
- TT overlay semantics are faithful;
- history pointer rebinding is correct;
- accumulators are correct;
- the shadow starts from the intended state;
- the research execution path still represents Stockfish.

# 8. Run force-next candidate probes

Once baseline-shadow replay is exact:

1. Save the common node-entry snapshot.
2. Run baseline shadow.
3. Obtain the candidate list.
4. For each selected candidate:
   - restore the same worker snapshot;
   - create a fresh TT overlay;
   - reset the same stack and accumulator state;
   - force the candidate at first quiet-stage entry;
   - run the complete node to resolution;
   - discard all mutations.
5. Resume the real node.
6. Confirm real search remains identical to the no-probe baseline.

## Record both common and candidate cost

For each probe:

```text
total_node_cost
nodes_before_quiet_stage
nodes_after_quiet_stage
forced_candidate_attempt_nodes
remaining_node_cost_after_candidate
returned_value
returned_bound_class
cutoff
alpha_raise
re_search_count
intervention_applied
budget_censored
```

The pre-quiet work should normally be identical across candidates. Assert or explicitly report when it is not.

The principal label is:

$$
C_{\mathrm{decision}}(m)
=
N_{\mathrm{total}}(m)-N_{\mathrm{stage\ entry}}(m)
$$

The full node cost remains useful for validating total consequences.

# 9. Implement safe budget cancellation

Do not use exceptions or `longjmp` unless all position/accumulator mutations are protected by RAII. Existing search relies on explicit undo calls, so uncontrolled unwinding would corrupt state.

Use a research-only abort flag and invalid sentinel propagation.

## Proposed behavior

At node entry:

```cpp
if (shadowContext && shadowContext->budget_exhausted()) {
    shadowContext->aborted = true;
    return VALUE_NONE;
}
```

After every recursive call:

1. Undo the move/null move.
2. Check `shadowContext->aborted`.
3. Propagate `VALUE_NONE` upward without interpreting it as a score.

Every recursive call site must be audited:

- normal PV/non-PV search;
- LMR search;
- full-depth re-search;
- null-move search;
- ProbCut;
- singular/excluded search;
- qsearch.

A censored probe records:

```text
status = BUDGET_CENSORED
nodes_observed = ...
returned_bound = unknown
```

It must never become a fail-low label.

## Budget tests

Force cancellation:

- immediately;
- one ply below target;
- during qsearch;
- during null-move search;
- during ProbCut;
- during LMR;
- during re-search.

After every case:

- position restored;
- accumulator restored;
- stack restored;
- source histories unchanged;
- base TT unchanged.

Start the first replay test without hard budgets at shallow internal depths. Add hard cancellation before scaling collection.

# 10. Extend the data schema

Create a new internal-counterfactual schema rather than overloading the root schema.

## Decision record

Include:

- root/game ID;
- node ID and path hash;
- position key;
- ply;
- remaining depth;
- root iteration depth;
- alpha;
- beta;
- node type;
- `cutNode`;
- in-check;
- improving;
- static evaluation;
- TT hit/bound/depth/move;
- previous move;
- previous-PV state;
- move count before quiet stage;
- quiet-stage entry;
- baseline policy version;
- TT protocol;
- node budget;
- deterministic sample key;
- candidate inclusion probabilities;
- state and TT checksums.

## Candidate record

Include:

- move;
- baseline rank;
- baseline MovePicker score;
- individual history components;
- continuation history components;
- pawn history;
- SEE;
- gives check;
- recapture;
- promotion/capture type;
- planned reduction;
- whether baseline would prune it at its original slot;
- forced-slot reduction;
- whether forced-slot pruning rejected it;
- intervention applied;
- first-probe nodes;
- total post-decision nodes;
- cutoff/alpha-raise/fail-low;
- returned value and bound class;
- re-search count;
- budget-censor status;
- result equivalence to baseline.

No candidate feature may contain information generated after the action is selected.

# 11. Phase 5 smoke-test sequence

Do not immediately collect millions of counterfactuals.

## P5-A: one-node hand test

Select one deterministic internal node.

Requirements:

- baseline shadow exactly matches real node;
- no-op force-first exactly matches baseline shadow;
- one alternative candidate produces a deterministic result;
- base state unchanged.

## P5-B: small fixed fixture set

Use approximately 10–20 development roots at modest depth.

Sample at most one qualifying internal node per root.

Test:

- candidate order A/B versus B/A;
- repeated-process determinism;
- multiple remaining depths;
- successful and unsuccessful cutoffs;
- at least one censored candidate;
- at least one candidate pruned in its baseline slot but searched when promoted.

## P5-C: instrumented regression run

Enable probes during a complete fixed-depth search.

The real search must retain:

- identical best move;
- identical score;
- identical PV;
- identical real node count.

Only external wall time should increase.

## Phase 5 exit gate

Phase 5 is complete when:

- baseline shadow replay is exact;
- no-op forced ordering is exact;
- candidate probe order is irrelevant;
- all source-state checksums pass;
- base TT is unchanged;
- budget cancellation is safe;
- logs validate;
- macro-off benchmark remains 453,169 nodes;
- the full test suite passes.

# 12. Phase 6 pilot dataset

Once the sandbox passes, collect a small internal causal dataset.

## Corpus

Use development roots only initially.

Do not touch the quarantined test set.

Start with perhaps:

- 50–100 game-diverse roots;
- fixed D14 or D16 root searches;
- internal remaining depths 4–10;
- one stage: first quiet-stage decision;
- deterministic sparse sampling;
- candidate cap of 6;
- logarithmic probe budgets derived from the Phase 5 pilot.

Then scale only if the cost distribution is manageable.

## Two sampling streams

### Dense training stream

Allow many sampled decisions per root.

This is useful for model training, but nested decision costs must not be summed as independent engine savings.

### Prefix-free opportunity stream

Once a decision is selected on a path, do not sample descendants of that decision.

This gives a cleaner estimate of where non-overlapping local opportunity exists. TT interactions still mean it is not an exact whole-search oracle, but it avoids obvious nested double counting.

Group all statistical uncertainty by root/game.

# 13. Phase 6 analyses

## Internal oracle gap

For each decision:

$$
C^*(h)=
\min_{m\in Q(h)} C(h,m)
$$

where $Q(h)$ contains candidates preserving the required bound.

Then:

$$
R_{\mathrm{base}}(h)
=
C_{\mathrm{baseline}}(h)-C^*(h)
$$

Report:

- positive-opportunity rate;
- pooled local regret;
- macro regret by root;
- median and tail regret;
- regret by remaining depth;
- regret by ply;
- regret by beta-static-eval margin;
- regret by candidate count;
- regret by TT state;
- regret by baseline cutoff rank;
- regret by quiet-stage entry move count.

Do not call the sum over nested nodes total engine savings.

## Compare ranking targets

Compare:

1. Baseline MovePicker order.
2. Existing history score.
3. Cutoff probability $q$.
4. Predicted candidate cost $e$.
5. $q/e$.
6. Direct measured force-next cost.
7. Simple pairwise model.
8. Simple direct cost-ranking model.

Primary metric:

$$
\operatorname{Capture}(\pi)
=
\frac{
C_{\mathrm{base}}-C_{\pi}
}{
C_{\mathrm{base}}-C^*
}
$$

Aggregate costs before dividing.

## Interaction study

For selected high-cost nodes, test:

- baseline order;
- best single candidate first;
- $q/e$ order;
- reverse top candidates;
- adjacent swaps;
- top-$K$ permutations or small beam search.

Extend the research MovePicker context from one forced move to a forced quiet prefix:

```text
forcedOrder = [m2, m4, m1]
```

After consuming the forced prefix, resume baseline order while skipping duplicates.

This measures whether a scalar score is sufficient or whether sequential interactions dominate.

## Cycle budget

Estimate:

$$
\text{affordable cycles/call}
\approx
\frac{
\text{recoverable nodes}\times\text{cycles per baseline node}
}{
\text{number of policy invocations}
}
$$

Calculate this separately by context. The likely result may be:

- policy affordable only above a depth threshold;
- policy affordable only when candidate count is large;
- policy affordable only at high-regret quiet stages.

That becomes the initial invocation gate.

# 14. Phase 6 go/no-go decision

Proceed toward neural training if the internal study shows:

1. Meaningful oracle regret exists at internal quiet decisions.
2. Opportunity is not confined to a handful of pathological roots.
3. Opportunity occurs in contexts with enough compute to pay for inference.
4. Cheap pre-action features explain some nontrivial fraction of oracle regret.
5. A scalar ranking captures enough of the permutation opportunity.
6. Bound-changing failures can be identified or safely gated.

Possible outcomes:

## Strong positive

Large internal gap, predictable, affordable.

Proceed with the full teacher and student.

## Concentrated positive

Opportunity exists only at deep cut nodes or specific margin/depth regions.

Build a gated policy for those contexts first.

## Large oracle, weak predictability

Invest in richer teacher features, survival targets, and Jacobian experiments.

## Small oracle

Stop or restrict the project before spending time on runtime neural inference.

# 15. Phase 7: proof-time modeling

Once internal probe data exists, model search as competing risks.

For each candidate:

- completion time $T$;
- fail-high event;
- fail-low event;
- budget censoring.

Use logarithmic node bins and predict:

$$
F_+(t)=P(T\le t,\text{ fail-high})
$$

$$
F_-(t)=P(T\le t,\text{ fail-low})
$$

$$
S(t)=P(T>t)
$$

Then derive, for budget $B$:

$$
q_B(m)=F_+(B)
$$

$$
e_B(m)=\int_0^B S_m(t)\,dt
$$

$$
I_B(m)=\frac{q_B(m)}{e_B(m)}
$$

This directly implements the original proof-policy formulation.

Compare the survival-derived ordering with direct force-next cost. If direct ranking is materially better, give it greater weight in the teacher.

# 16. Phase 8: Jacobian experiment

Test whether the existing value NNUE contains useful move-order signal.

Start narrowly with:

- quiet;
- non-king;
- non-capture;
- same material bucket;
- canonical perspective.

Compute the exact or explicitly defined surrogate sensitivity:

$$
S_J(P,m)=g(P)^\top\Delta A(m)
$$

Measure:

- cutoff ranking;
- alpha-raise ranking;
- cost ranking;
- oracle-regret capture;
- incremental cost of obtaining the signal.

Then test prototype compression and a cheap router.

Outcomes:

- Strong signal: use as teacher/student input.
- Moderate but expensive: teacher-only feature.
- Weak signal: discard without affecting the primary Policy-NNUE design.

The generalized policy does not depend on the Jacobian hypothesis succeeding.

# 17. Phase 9: train the teacher

## Data splits

Split by source game/root:

- train;
- validation;
- untouched test.

Never split candidate rows from one decision across sets.

## Training sequence

1. Pretrain from the large observational dataset:
   - cutoff;
   - alpha raise;
   - cost;
   - final best;
   - existing ordering.
2. Fine-tune on internal counterfactual groups.
3. Use pairwise and listwise ranking losses.
4. Train auxiliary survival and bound heads.

## Teacher inputs

Use the full planned feature set:

- board representation;
- depth and ply;
- alpha/beta/window;
- static-eval margins;
- PV/cut/all context;
- TT information;
- previous moves;
- MovePicker stage;
- baseline rank;
- all history components;
- SEE/check/capture information;
- planned reduction;
- optional Jacobian/prototype score.

## Teacher metrics

Do not select by AUC alone.

Use:

- oracle-regret capture;
- cost-weighted pair accuracy;
- simulated decision cost;
- cutoff calibration;
- tail-cost errors;
- performance by depth/node type;
- held-out root performance;
- catastrophic-regret rate.

The teacher should also support abstention: retaining baseline order when uncertain.

# 18. Phase 10: build the incremental student

Only after the teacher demonstrates learnable internal regret.

## Student v1

Implement the original design:

$$
Z(P)=\sum_{f\in P}E_f
$$

with two perspective accumulators and dimensions:

- 16;
- 32;
- 64.

Update incrementally on:

- ordinary moves;
- captures;
- en passant;
- promotions;
- castling;
- null moves through side-to-move selection.

## Candidate score

Construct:

$$
Z_c =
Z(P)
+C_{\text{depth}}
+C_{\text{ply}}
+C_{\text{node}}
+C_{\text{window}}
+C_{\text{margin}}
+C_{\text{stage}}
+C_{\text{move count}}
$$

and:

$$
U(m)=
U_{\text{from/to}}
+U_{\text{capture}}
+U_{\text{promotion}}
+U_{\text{move kind}}
$$

Then:

$$
S_{\text{policy}}(m)
=
Z_c^\top U(m)
+
w^\top\phi(m,h)
$$

The runtime model emits one scalar priority.

Teacher-only heads for survival, $q$, and cost need not be deployed.

## Distillation target

Train the student to preserve:

- teacher ordering;
- teacher oracle-regret capture;
- confidence/abstention behavior;
- hard high-cost negatives.

Use quantization-aware training and export golden vectors.

# 19. Phase 11: conservative engine integration

Initial deployment:

- non-PV null-window nodes;
- quiet stage only;
- minimum remaining depth gate;
- TT move unchanged;
- captures unchanged;
- no qsearch;
- no in-check nodes;
- LMR unchanged;
- policy blended with existing history.

For example:

$$
S_{\text{final}}
=
S_{\text{history}}
+
\lambda(h)S_{\text{policy}}
$$

Evaluate:

1. Accumulator-only NPS overhead.
2. Context construction cost.
3. Per-candidate scoring cost.
4. Fixed-depth nodes.
5. Fixed-depth wall time.
6. Root-result agreement.
7. Tactical suites.
8. Fixed-time games.
9. Elo/SPRT.

Only after ordering succeeds should policy confidence influence LMR.

# Concrete commit sequence

I would implement the next work in these reviewable units:

1. Freeze Phase 4 handoff and define Phase 5 v1 protocol.
2. Refactor search/qsearch to compile-time TT access; live behavior unchanged.
3. Add `ResearchTTOverlay` and low-level tests.
4. Add isolated shared-history and worker-state cloning.
5. Add stack-pointer rebinding and accumulator snapshot tests.
6. Add `ScopedShadowProbe` and recursive instrumentation suppression.
7. Add first-quiet-stage MovePicker discovery.
8. Add baseline internal shadow replay.
9. Prove baseline shadow equals real node.
10. Add force-next quiet intervention.
11. Prove no-op and candidate-order independence.
12. Add safe budget cancellation and censoring.
13. Add internal-counterfactual schema and decoder validation.
14. Run the Phase 5 smoke suite.
15. Collect the Phase 6 pilot dataset.
16. Produce oracle/ratio/context/interaction reports.
17. Make the actual generalized-policy go/no-go decision.
18. Then proceed to survival/Jacobian/teacher/student work.

The next decisive milestone is not another root statistic. It is:

> A baseline internal shadow search starts from an arbitrary real node, uses an isolated worker and TT overlay, and reproduces the real node’s value and node count exactly.

Once that works, the project crosses from root feasibility into the generalized alpha-beta policy research envisioned in the original plan.
