# Phase 6.2 — explanatory policy study (top-four, ex-post, in-sample)

Answers plan §11.2/§11.4/§11.5 questions 1-3 and 5 on the existing
internal-counterfactual/3 corpora (12 root positions: ten-root breadth
corpus + root A depth-8 seed-0 + root B depth-8; 15,825 decision rows,
94,950 forced probes, zero censoring after the b8_nimzo budget-5000
re-collection, zero baseline/live mismatches). Reproduction:
`tools/policy_research/p6_explanatory.py <dirs...> --json out.json`;
per-root tables in `p6-policy-tables.json`.

## Method and honest labels

Every policy below is a **single-candidate top-1 rule**: it either keeps the
natural order (ordinal 0) or promotes exactly one candidate to slot 1. For
those rules the measured forced whole-node replay of the chosen candidate IS
the node cost under the rule (prefix-preserving, same entry state), so no
independence assumption is needed — unlike multi-move ordering simulations,
which require the plan §11.3 shared-permutation data and are NOT attempted
here. All policies are **ex-post/in-sample** (they use the measured
outcomes), so every savings figure below is an UPPER BOUND on what any
learnable entry-state rule could achieve on this corpus; the learnability
gap is unknown until a predictor is trained and root-held-out evaluated.

Policies:
- `BASE` natural order (ordinal 0) — the reference.
- `CHEAP_UNC` cheapest measured whole-node cost among ordinals 0..3, no
  classification constraint (aggressive upper bound).
- `ORACLE_CLS` cheapest measured cost whose whole-node fail-high status
  equals the baseline (conservative upper bound; 0 outcome flips by
  construction).
- `ORACLE_EXACT` cheapest measured cost whose whole-node value equals the
  baseline value.
- `Q1_EARLIEST` promote the earliest candidate whose own slot-1 search cut
  off (`cutoff_by_first`); baseline otherwise. Pure cutoff-probability (q)
  knowledge, no cost knowledge.
- `Q1_CHEAPEST` among candidates whose own slot-1 search cut off, promote
  the cheapest measured; baseline otherwise. Perfect q + perfect cost
  restricted to q = 1 candidates.

## Results (pooled; savings are ratio-of-sums, plan §11.1)

| policy | save% | later pick (% rows) | FH->FL rows | FL->FH rows |
|---|---:|---:|---:|---:|
| BASE | 0.00 | 0.0 | 0 | 0 |
| CHEAP_UNC | 27.34 | 26.8 | 88 | 184 |
| ORACLE_CLS | 24.01 | 25.4 | 0 | 0 |
| ORACLE_EXACT | 12.75 | 12.8 | 0 | 0 |
| Q1_EARLIEST | 1.59 | 38.0 | 0 | 262 |
| Q1_CHEAPEST | 5.71 | 38.0 | 0 | 262 |

Root-clustered savings (12 roots): CHEAP_UNC 26.35 +/- 3.42 sd (21.6-32.7);
ORACLE_CLS 23.12 +/- 3.64 (19.1-29.6); ORACLE_EXACT 12.15 +/- 2.48
(8.8-15.0); Q1_EARLIEST 0.94 +/- 4.62 (negative on several roots);
Q1_CHEAPEST 5.32 +/- 3.86 (-2.4-9.6).

## Reading the table

1. **The oracle is mostly a COST effect, not a cutoff-probability effect.**
   Perfect knowledge that a candidate cuts off by itself (Q1_EARLIEST)
   saves only 1.6% — it promotes on 38% of rows and mostly at a loss,
   because own-cut candidates are usually still more expensive than the
   natural order. Adding cost information restricted to q=1 candidates
   (Q1_CHEAPEST) reaches 5.7%. The full classification-preserving oracle
   (24.0%) therefore relies on rows where the promoted candidate does NOT
   cut off by itself (~41% of oracle picks) and on cost differences in the
   continuation — exactly the whole-node, prefix-reserving measurement-B
   information. A q/e-style head alone captures at most ~6/24 = 25% of the
   oracle; the direct per-candidate whole-node cost (measurement B) is the
   dominant signal and must be part of any teacher label.
2. **The classification constraint costs little.** CHEAP_UNC exceeds
   ORACLE_CLS by 3.3 percentage points (12% of its gain) while flipping
   outcomes on 272 rows (1.7%: 88 FH->FL deteriorations, 184 FL->FH
   improvements). Under exact-value equivalence the headroom is 12.75% —
   the strictest safe bound.
3. **Per-invocation economics.** Aggregate savings per eligible decision
   row: ORACLE_CLS 2.53 nodes, ORACLE_EXACT 1.34, Q1_CHEAPEST 0.60,
   CHEAP_UNC 2.88 (baseline mean 10.5 nodes). Eligible decision rows are a
   census of ~20-27% of all searched nodes at these depths (see root_end
   searched_nodes), so a globally invoked scorer must clear a very low
   bar: at the measured ~1.4M nps of this research binary (~700 ns/node),
   the perfect-oracle budget is ~1.8 us per eligible node, and a realistic
   learnable share (say a third to a quarter of ORACLE_CLS) leaves ~0.4-0.6
   us. Table/linear/tree scorers fit; NNUE-scale forwards do not at full
   invocation — but they do fit the high-opportunity stratum: on rows with
   baseline >= 25 nodes (8.4% of eligible rows, ~2% of all nodes at these
   depths) ORACLE_CLS saves 31.6% (~15 nodes per invocation), and on
   baseline >= 50 rows ~34-37% (39 nodes per invocation, tens of us of
   budget). A universal gate (cheap) plus deeper scoring only where entry
   features indicate an expensive node is the economically viable shape.

## Stratified savings (pooled; baseline >= threshold)

| policy | >=1 row (15,825) | >=10 (3,759) | >=25 (1,322) |
|---|---:|---:|---:|
| CHEAP_UNC | 27.3 | 32.5 | 36.9 |
| ORACLE_CLS | 24.0 | 28.3 | 31.6 |
| ORACLE_EXACT | 12.8 | 15.5 | 18.5 |
| Q1_EARLIEST | 1.6 | 12.3 | 16.4 |
| Q1_CHEAPEST | 5.7 | 14.5 | 17.4 |

Savings concentrate: rows with baseline >= 10 nodes are 23.8% of eligible
rows but hold 89.6% of ORACLE_CLS savings; >= 25 rows hold 71.5%; >= 50 hold
55.4%.

## Statistical treatment (plan §11.4)

- All aggregate figures are sums before division; per-row ratios are never
  averaged for cost claims.
- Root-level spread is reported (12 manually selected root positions —
  exploratory breadth, not a random population sample; treat intervals as
  descriptive). The 12-root t-interval for ORACLE_CLS is ~[20.8, 25.4]% and
  for ORACLE_EXACT ~[10.6, 13.7]% but should not be quoted as population
  inference.
- Rows within a root are correlated (shared TT/history, iterative
  deepening, repeated identities); no row-level independence is assumed.
- Censoring: zero (b8_nimzo re-collected at budget 5000).
- Tail ordinals (>3) are NOT used here; the top-four census is exact.

## Go/no-go input

- Total oracle saving available (top-four, conservative): ~24% of eligible
  subtree nodes (~12.75% under value-equivalence), consistent across 12
  roots — a real but bounded target.
- Contexts: opportunity concentrates in nontrivial nodes (baseline >= 10);
  entry-depth and history/TT features plausibly identify them.
- Oracle explained by q/e: <= ~25% (Q1_CHEAPEST / ORACLE_CLS). The direct
  whole-node cost label must be the primary teacher signal.
- Interaction gap: NOT yet measured (requires shared-permutation data,
  plan §11.3); local savings do not yet have a global additivity claim.
- Cycle budget: universal invocation requires a sub-microsecond scorer;
  stratified invocation (gate + deep scorer on predicted-expensive nodes)
  can afford NNUE-scale scoring on a small fraction of nodes.
- Next: implement the §11.3 shared-permutation mode (design in
  `docs/policy-research/plan.md` §11.3 and the review-actions file), then
  train/evaluate a root-held-out predictor before any neural work.
