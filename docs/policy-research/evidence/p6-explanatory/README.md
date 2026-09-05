# Phase 6.2 — explanatory policy study (top-four, ex-post, in-sample)

First results of plan §11.2/§11.4/§11.5 on the existing
internal-counterfactual/3 corpora (12 root positions: ten-root breadth
corpus + root A depth-8 seed-0 + root B depth-8; 15,825 decision rows,
94,950 forced probes, zero censoring, zero baseline/live mismatches). The
root-held-out learnability probe (a real q/e-ratio study with feature-only
predictors) is reported in `p6-learnability/README.md`.

Reproduction: `tools/policy_research/p6_explanatory.py <dirs/files...> --json
out.json`; `tools/policy_research/p5b_analysis.py <dirs/files...>
--measurement-a` reproduces the ordinal cost tables and measurement-A
summaries. Pass the breadth directory plus the two canonical files for the
12-root pooled numbers. Per-root tables: `p6-policy-tables.json`. Unit
tests: `tools/policy_research/tests/test_p6_analysis_tools.py`.

## Method and honest labels

Every policy below is a **single-candidate top-1 rule**: it either keeps the
natural order (ordinal 0) or promotes exactly one candidate to slot 1. For
those rules the measured forced whole-node replay of the chosen candidate IS
the node cost under the rule (prefix-preserving, same entry state), so no
independence assumption is needed — unlike multi-move ordering simulations,
which require the plan §11.3 shared-permutation data and are NOT attempted
here.

Policies fall into three categories; only the first two are upper bounds:

1. EX-POST ORACLES (use the measured future outcomes; upper bounds for any
   rule restricted to entry-state features):
   - `BASE` natural order (ordinal 0) — the reference.
   - `CHEAP_UNC` cheapest measured whole-node cost among ordinals 0..3,
     unconstrained.
   - `ORACLE_CLS` cheapest measured cost whose whole-node fail-high status
     equals the baseline (no classification flips by construction).
   - `ORACLE_EXACT` cheapest measured cost whose whole-node value equals the
     baseline value.
2. MEASUREMENT-A-CONDITIONAL HEURISTICS (use the realized slot-1 own-cut
   label `cutoff_by_first`; NOT upper bounds — a real q model is noisy and
   even the perfect-label rules below lose to baseline whenever they
   promote a self-cutting but expensive candidate):
   - `Q1_EARLIEST` promote the earliest own-cutting candidate; baseline if
     none.
   - `Q1_CHEAPEST_FORCED` promote the cheapest own-cutting candidate;
     baseline only when none self-cuts (forced-promotion variant: promotes
     even when every own-cut candidate is costlier than baseline).
   - `Q1_CHEAPEST_ABSTAIN` cheapest of {baseline, own-cutting candidates}.
   - `Q1_SAFE_ABSTAIN` like `Q1_CHEAPEST_ABSTAIN` restricted to own-cutting
     candidates whose whole-node classification matches the baseline.
3. `p6_learnability` adds feature-only, root-held-out predictors (the only
   deployable-shape rules measured so far).

Aggregation is ratio-of-sums (plan §11.1); per-row ratios are never averaged
for cost claims. Classification flips are reported as changes — NOT as
improvements/deteriorations: the baseline and counterfactual searches are
both selective approximations, and deciding which result is better needs a
full-window or deeper reference.

## Results (pooled; savings are ratio-of-sums)

| policy | save% | later pick (% rows) | FH->FL rows | FL->FH rows |
|---|---:|---:|---:|---:|
| BASE | 0.00 | 0.0 | 0 | 0 |
| CHEAP_UNC | 27.34 | 26.8 | 88 | 184 |
| ORACLE_CLS | 24.01 | 25.4 | 0 | 0 |
| ORACLE_EXACT | 12.75 | 12.8 | 0 | 0 |
| Q1_EARLIEST | 1.59 | 38.0 | 0 | 262 |
| Q1_CHEAPEST_FORCED | 5.71 | 38.0 | 0 | 262 |
| Q1_CHEAPEST_ABSTAIN | 15.69 | 16.2 | 0 | 161 |
| Q1_SAFE_ABSTAIN | 13.66 | 15.1 | 0 | 0 |

Root-clustered savings (12 roots): CHEAP_UNC 26.35 +/- 3.42 sd (21.6-32.7);
ORACLE_CLS 23.12 +/- 3.64 (19.1-29.6); ORACLE_EXACT 12.15 +/- 2.48
(8.8-15.0); Q1_CHEAPEST_ABSTAIN 15.07 +/- 2.63 (11.4-18.7); Q1_SAFE_ABSTAIN
13.07 +/- 2.39 (10.3-17.8).

## Reading the table

1. **Cost-aware abstention, not self-cut probability, drives the oracle.**
   The earlier write-up of this study compared a forced-promotion own-cut
   rule (`Q1_CHEAPEST_FORCED`, 5.7%) against the oracle and concluded q/e
   explains "at most ~25%" of it. That rule promotes on 38% of rows even
   when every own-cutting candidate costs MORE than baseline, so it is not
   an upper bound and the 25% conclusion was invalid. Once own-cut rules are
   allowed to abstain (as any deployable policy must), perfect own-cut
   knowledge plus cost knowledge among own-cutting candidates
   (`Q1_CHEAPEST_ABSTAIN`) saves 15.7% — 65% of the ORACLE_CLS 24.0% — and
   the classification-safe variant (`Q1_SAFE_ABSTAIN`) saves 13.7% (57% of
   the oracle). Self-cut knowledge is important but insufficient: 41.2% of
   oracle picks and 45.3% of oracle savings come from rows where the
   promoted candidate does NOT cut off by itself (continuation effects).
2. **Whole-node cost (measurement B) is the dominant oracle signal.** The
   ex-post oracles need the measured per-candidate whole-node cost; no
   purely pre-search feature rule is measured here. Whether entry-state
   features can predict that cost is the learnability question answered in
   `p6-learnability/README.md`.
3. **The classification constraint is cheap.** CHEAP_UNC exceeds ORACLE_CLS
   by 3.3 percentage points while flipping classifications on 272 rows
   (1.7%). Note the flip counts are directionally mixed (88 FH->FL, 184
   FL->FH); they are NOT evidence of improvement, and ORACLE_EXACT (12.75%)
   remains the strictest safe local bound.
4. **Per-invocation economics (local, first order).** Aggregate savings per
   eligible decision row: ORACLE_CLS 2.53 nodes, ORACLE_EXACT 1.34,
   Q1_CHEAPEST_ABSTAIN 1.65. These sums are over NESTED local subtrees
   (local subtree totals are ~2.4x the actual root-search node counts), so
   they are not a composable global budget; the real affordability test
   requires a live policy run (Phase 11-style). Directionally: a universal
   scorer must cost far less than one subtree node-equivalent per eligible
   node, and savings concentrate in nontrivial nodes (baseline >= 10 nodes:
   23.8% of rows, 89.6% of ORACLE_CLS savings; >= 25: 8.4% of rows, 71.5%),
   so gate + stratified invocation remains the economically viable shape —
   but whether pre-search features can identify those rows (and at what
   precision) is an empirical question, not yet an assumption.

## Stratified savings (pooled; baseline >= threshold)

| policy | >=1 row (15,825) | >=10 (3,759) | >=25 (1,322) |
|---|---:|---:|---:|
| CHEAP_UNC | 27.3 | 32.5 | 36.9 |
| ORACLE_CLS | 24.0 | 28.3 | 31.6 |
| ORACLE_EXACT | 12.8 | 15.5 | 18.5 |
| Q1_CHEAPEST_ABSTAIN | 15.7 | 24.2 | 29.3 |
| Q1_SAFE_ABSTAIN | 13.7 | 20.4 | 23.7 |

## Statistical treatment (plan §11.4)

- All aggregate figures are sums before division.
- Root-level spread reported over 12 manually selected opening positions
  (exploratory breadth, NOT a random population sample; do not quote the
  spreads as population inference).
- Rows within a root are correlated (shared TT/history, iterative
  deepening, repeated identities); no row-level independence is assumed.
- Censoring: zero.
- Tail ordinals (>3) are NOT used; the top-four census is exact, so all
  oracle numbers here are conservative in coverage (they may miss cheaper
  tail candidates) and optimistic in knowledge (ex-post).
- `p6-learnability` evaluates feature-only rules root-held-out with
  root-clustered summary; both docs share the same raw corpora.

## Measurement-A shape note

Baseline fail-high rows split into: move-loop cutoffs 8,774 (86.8% of them
on the natural ordinal-0 move; pooled 12-root set), and 501 in-loop
early-return fail highs (singular-extension/multi-cut probe fails high
before the slot-1 child is searched: `cutoff_seen == false`,
`first.searched == false`). These are NOT pre-loop exits — every decision
row reaches its move loop (rows whose replay ends pre-loop never fire the
decision capture and are dropped). They should be modeled as a separate
proof mechanism in q-labeling.

## Go/no-go input (as of this report)

- Local oracle target: ~24% (classification-preserving, top-four,
  conservative in coverage) / 12.75% (exact-value) of eligible subtree
  nodes; consistent across the 12 roots.
- Self-cut knowledge + abstention captures 57-65% of the local oracle;
  continuation effects ~40-45%; exact-value preservation still halves the
  headroom.
- Cycle budget: a composable number does NOT yet exist (local sums
  overlap); learnability and interaction-gap measurements (next) plus a
  live policy run are required before the plan §11.5 go/no-go.
