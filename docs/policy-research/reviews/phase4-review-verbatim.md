# Expert Review of Phase 4 Counterfactual Research (Exact Verbatim Transcript)

Date: 2026-09-04
Source: Independent expert review of Phase 4 root-level counterfactual experiments on branch `policy-research` (commit `ca3cd4985771959add663d15e4d4ce5c3e41254d`).

---

## Overall verdict

The experiment is **not fundamentally wrong**. It provides strong evidence that changing the first root move can deterministically and substantially alter Stockfish’s selective search cost while often preserving the final result.

However, it remains an **oracle-headroom and mechanism study**, not evidence that a realizable learned policy will save 22–43% of search work. Two current descriptions are methodologically overstated:

1. No existing condition cleanly isolates “pure move ordering.”
2. The “fixed-candidate” depth ladder does not actually use an identical candidate set at every depth.

Those issues do not erase the observed phenomenon, but they change what can honestly be inferred.

---

## What the results establish well

### 1. The root-order intervention is real and deterministic

The no-op control is now convincing:

- On 17 canonical roots where the D−1 leader appears in the candidate list, forcing it reproduces nodes, score, best move, and PV exactly.
- The separate `c3-d-007` check also gives:
  - Baseline: 129,556 nodes
  - Force D−1 leader `c1e3`: 129,556 nodes
  - Force untreated final best `f2f3`: 66,704 nodes

This correctly demonstrates that the **D−1 leader**, not necessarily the eventual D-best move, is the no-op control.

The common-prefix design is also credible from code inspection: the force and ablation options are gated to the target depth. Identical D−1 node counts are consistent with that design. Calling the complete TT/history state “byte-for-byte verified” is slightly stronger than what the artifact records, but there is no obvious prefix contamination left.

### 2. Node accounting is now sound

Across the canonical D14 artifact:

- All 90 baseline/candidate searches satisfy  
  \[
  \sum_m \Delta E_D(m) \leq \Delta N_D.
  \]
- Only four have nonzero root overhead, ranging from 1 to 16 nodes.
- Per-aspiration-attempt node totals equal incremental nodes exactly in every case.

Across the depth ladder, root overhead remains tiny: 0–25 nodes. This is good telemetry and resolves the previous attribution problem.

### 3. There is genuine root-level search headroom

At the chosen ±50 cp quality gate:

- 9/18 roots have a quality-valid saving opportunity.
- Pooled cumulative saving: **22.39%**
- Pooled target-iteration saving: **42.80%**
- Macro mean cumulative saving: **18.45%**

The effect is not just timing noise: node counts are deterministic, and some effects are very large.

The strongest mechanistic example is `c3-d-004`:

- Baseline: 69,878 nodes
- Joint intervention: 24,972 nodes
- Preserve previous PV: 25,410 nodes

The savings survive preservation of the baseline PV-follow path, suggesting that moving the candidate into the first root slot is genuinely important.

### 4. Timing supports the node results locally

The current regenerated timing artifact shows:

- `c3-v-001`: engine median **18 → 5 ms**
- `c3-d-004`: engine median **62 → 22 ms**

For both roots, all 12 paired engine-time differences favor the intervention. All 12 wall-time differences also favor it, despite roughly 300 ms of process-start overhead.

This is convincing local latency evidence, though not yet a production benchmark.

---

## Important methodological problems

### 1. There is still no “pure move-order” condition

The causal matrix does not fully isolate the mechanisms.

`preserve_previous_pv` preserves `lastIterationIdxPV`, but it still uses the forced candidate’s:

- `averageScore`
- `meanSquaredScore`
- aspiration window
- derived optimism

Conversely, `preserve_aspiration` restores baseline aspiration/optimism but does **not** preserve the baseline previous PV.

`pure_order_nominal_depth` preserves aspiration and disables fail-high depth reduction, but still changes previous-PV/follow-PV state. Therefore it is not pure ordering.

This matters empirically. On `c3-d-004`:

- Joint: 24,972 nodes, quality-valid
- Preserve aspiration: 24,596 nodes, but **fails the predefined ±50 cp quality gate**
- Preserve previous PV: 25,410 nodes, quality-valid

The documentation’s claim that the 24,596-node result is “pure MovePicker ordering” is not supported. It still contains a PV-follow change, and its score is 67 cp from the reference.

A proper isolation would require at least the missing combinations:

- preserve aspiration + preserve previous PV
- preserve previous PV + disable fail-high reduction
- preserve all three controls

For the nominal-depth comparison, it also needs a **matched unforced baseline with fail-high reduction disabled**. Otherwise the difference conflates the forced ordering with the independent effect of disabling a normal baseline search mechanism.

### 2. The “fixed-candidate” ladder is not strictly fixed

`p4_depth_ladder.py` generates a fixed D10 shortlist, but then does this at every target depth:

```python
if base_temp["best"] not in depth_candidates:
    depth_candidates.insert(0, base_temp["best"])
```

Thus it uses a fixed core shortlist **plus a depth-specific, same-depth hindsight move**.

This affects 6 of the 54 root-depth cells:

- Three cells for `c3-d-007`
- Three cells for `c3-v-008`

The added move is selected as the reported optimum in five of those cells. Most are zero-saving classifications, but at `c3-d-007@D14`, the added final-best move `f2f3` supplies the reported 48.51% saving.

Fortunately, the headline is robust: using only the strict D10 shortlist, `a2a3` still gives a valid 46.56% saving on that root. A strict reanalysis produces:

- Positive roots: still **9/18**
- Pooled cumulative saving: **22.11%**, rather than 22.39%
- Pooled incremental saving: **42.27%**, rather than 42.80%

So this is a real design flaw, but it does not overturn the principal D14 conclusion.

The same issue remains in `p4_force_first.py`: it automatically adds the untreated final best, even though the experiment has now established that this is not generally a no-op. The proper added control should be the D−1 leader.

### 3. The causal decomposition is highly non-additive

The results show strong interactions rather than clean separable components.

For `c3-d-002`:

- Joint intervention: 18,176 nodes
- Preserve aspiration: 26,456
- Preserve previous PV: 18,634
- Disable fail-high reduction: 32,602
- Preserve aspiration + disable reduction: 78,030

This is not a simple decomposition where independent effects can be added. Aspiration state, PV following, and depth reduction interact strongly. The current conditions are useful diagnostics, but not a complete causal decomposition.

### 4. Much of the largest saving is selective-depth behavior

On `c3-v-001`, the 5,643-node intervention performs attempts at effective depths:

- D14
- D13
- D12
- D11

The final exact search is at D11 because repeated fail-highs trigger Stockfish’s standard depth-reduction logic. That is a legitimate measurement of practical Stockfish behavior, but it is not equivalent to reducing the cost of an otherwise identical nominal D14 proof.

The nominal-depth-like condition still saves 31.1% there, which is meaningful. But the headline 73.1% is primarily a **joint search-control effect**, not classical best-first alpha-beta efficiency alone.

---

## Population and generalization concerns

### 1. The 50% opportunity rate is descriptive, not an estimate of chess positions

The 18 roots are curated test and benchmark positions, not a random sample of positions encountered in games. Development and validation roots have also been repeatedly inspected during methodology development.

Therefore “9/18” means exactly that for this corpus. It should not be interpreted as “50% of chess positions offer this saving.”

The quarantined test split is still required before making a generalization claim.

### 2. Savings are concentrated

At D14, the top contributors to the 203,508 saved nodes are:

- `c3-d-007`: 30.9%
- `c3-d-004`: 22.1%
- `c3-v-008`: 19.3%
- `c3-d-008`: 12.8%

Those four roots account for about **85% of pooled savings**.

At D16, `c3-d-004` alone accounts for 59.1% of all saved nodes, and `c3-d-004` plus `c3-d-006` account for about 79%.

The macro statistics partly address this, but the pooled headline is strongly outlier-driven.

### 3. Aggregate depth scaling is real, but root-level stability is weak

The positive counts rise:

- D12: 5/18
- D14: 9/18
- D16: 12/18

But only three roots are positive at all three depths:

- `c3-d-002`
- `c3-d-004`
- `c3-v-005`

Jaccard overlap of positive sets is low:

- D12 vs D14: 0.27
- D14 vs D16: 0.40
- D12 vs D16: 0.42

The best intervention move also changes frequently with depth. This suggests that a useful policy will need to be strongly conditioned on depth, aspiration state, previous PV, and search context. A static move prior is unlikely to capture the observed behavior.

### 4. Results depend materially on the score tolerance

Recomputing the D14 population with the artifact’s tolerance bands:

| Gate | Positive roots | Pooled cumulative saving |
|---|---:|---:|
| ±25 cp | 7/18 | 16.89% |
| ±50 cp | 9/18 | 22.39% |
| ±75 cp | 10/18 | 36.63% |
| ±100 cp | 10/18 | 36.63% |

Thus the exact headline depends significantly on the chosen ±50 cp threshold.

The D+2 reference is also not a ground truth. Several positions show substantial score movement between D14 and D16, and some best moves change. The ladder shows better best-move stability from D16 to D18, but score instability remains on some roots. Stronger quality validation should eventually use deeper/stable references and game-level testing.

---

## Timing review

The timing result is directionally strong but limited:

- Only two selected roots were measured.
- Engine time is integer-millisecond resolution; 5 ms measurements are heavily quantized.
- CPU affinity, frequency control, and system isolation are not documented.
- The cyclic schedule is position-balanced, but it is not fully carryover-balanced: each treatment tends to follow the same predecessor.
- Wall time is dominated by process startup and NNUE loading.

Still, because the node savings are deterministic and all 12 paired timing observations favor the intervention, the timing result is credible for these two cases. It should be described as corroboration, not a broad performance estimate.

---

## My main inference for the policy design

The useful target is probably **not “predict Stockfish’s eventual best move.”**

Several of the strongest interventions search a move first that does not become the final best move. Their value comes from altering:

- alpha establishment,
- aspiration behavior,
- TT contents,
- fail-high counts,
- adjusted depth,
- PV-follow behavior,
- and later move costs.

So the learned target should be closer to:

> “Which move is most useful to search next under the current search state?”

That target must include search-state features such as depth, window, previous PV, expected fail-high behavior, node type, and possibly history/TT context. A static board-only move policy will probably underperform because the observed opportunity is highly search-state dependent.

An NNUE-style board accumulator can still be useful, but it should be combined with explicit search-state inputs and candidate-specific move features.

---

## Documentation consistency

The canonical JSON is cleaner than the current documentation. Some stale statements remain:

- `overview.md` still has an old top-level status row mentioning 10 trials, old speedups, and the old 5-baseline-optimal/3-harmful taxonomy.
- Some timing means and wall medians no longer match the final `ca3cd498` artifacts.
- Some exact D12/D16 node counts for `c3-d-004` are stale.
- `plan.md` and the README still contain an overbroad statement that forcing “baseline best” is an exact no-op.
- The documents call the ladder fixed-candidate even though the runner adds depth-specific final-best moves.

These do not affect the JSON results, but they mean the documentation is not yet fully authoritative.

---

## Bottom line

Phase 4 successfully demonstrates a **large, deterministic, search-state-sensitive root-ordering opportunity**. It is strong enough to justify building a Phase 5 internal sandbox.

It does **not** yet establish:

- a pure move-ordering effect separated from all other search controls,
- a realizable policy saving 22.4%,
- formal proof-cost reduction,
- stable per-root behavior across depths,
- or expected Elo improvement.

I would proceed to Phase 5 as a causal exploration, while treating the current numbers as an **oracle upper bound** and correcting the experimental matrix before making stronger mechanistic or production claims. No files were edited.
