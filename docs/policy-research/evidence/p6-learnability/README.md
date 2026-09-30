# Phase 6.2 — corrected root-held-out learnability probe

Can a policy that sees only pre-search entry features realize any of the
ex-post local-oracle headroom? On the current 12-root opening corpus the
answer is **a small preliminary yes for the linear QE policy, not yet a
production result**.

This report supersedes the original negative result. The original scorer fit
models on fold-winsorized/standardized features but accidentally applied the
coefficients to raw features at policy-selection time. Diagnostics used the
correct transform, which hid the mismatch. The corrected implementation routes
both fitting and every policy prediction through the same training-fold
transform and has a regression test comparing single-candidate and batch
prediction paths.

Reproduction (full corpus):

```sh
python3 tools/policy_research/p6_learnability.py \
  docs/policy-research/evidence/p5-phase6-breadth \
  docs/policy-research/evidence/p5-m2m3-reorder-v3/seed_a1.jsonl.gz \
  docs/policy-research/evidence/p5-m2m3-reorder-v3/root_b_d8.jsonl.gz \
  --json docs/policy-research/evidence/p6-learnability/p6-learnability-results.json
```

Add `--min-baseline-nodes 10` and write
`p6-learnability-ge10-results.json` for the declared high-cost stratum.

## Method

- 15,825 `internal-counterfactual/3` rows from 12 separately searched opening
  roots; candidates are the static pre-search MovePicker top four.
- Labels: exact measured whole-node force-first cost and slot-1 own-cut outcome.
- Features: entry context plus candidate stage, score, history, SEE, check,
  capture, TT-move and ordinal features. No post-search feature enters a model.
- Models: ridge logistic own-cut predictor and ridge log-cost predictor.
- Leave-one-root-out evaluation. Each fold fits winsorization and standardization
  on its 11 training roots; the held-out root uses those frozen statistics.
- Policy thresholds/lambda are selected by total measured training-fold cost.
  Evaluation charges the exact measured cost of the selected ordinal.
- Headline percentages are ratios of sums. Roots are correlated exploratory
  clusters, not a random population sample.

## Corrected full-corpus results

Pooled baseline: 166,807 local replay nodes.

| policy | save % | later pick (% rows) | FH→FL | FL→FH |
|---|---:|---:|---:|---:|
| BASE | 0.00 | 0.0 | 0 | 0 |
| Q | 0.00 | 0.0 | 0 | 0 |
| CHEAP | -0.40 | 11.9 | 18 | 21 |
| CHEAP_SAFE | -0.01 | 0.7 | 0 | 1 |
| **QE** | **+0.60** | **5.9** | **9** | **15** |

QE costs 165,809 nodes, saves 998 local replay nodes and captures 2.5% of the
classification-preserving oracle. It is positive on 10/12 held-out roots;
the root-average saving is 0.42% with SD 1.18%. That spread is descriptive;
12 selected roots do not support a population-confidence claim.

Hindsight safety diagnostics (not deployable gates): replacing QE choices that
change fail-high classification with baseline leaves about +0.47%; replacing
choices whose returned value differs from baseline leaves about +0.33%. Thus
the sign is not solely produced by outcome-changing choices, but search quality
still requires deeper/live evaluation.

## Declared baseline ≥10-node stratum

3,759 rows; pooled baseline 126,645 nodes.

| policy | save % | later pick (% rows) | FH→FL | FL→FH |
|---|---:|---:|---:|---:|
| Q | -2.11 | 24.9 | 21 | 28 |
| CHEAP | -1.34 | 67.0 | 67 | 78 |
| CHEAP_SAFE | +0.21 | 29.0 | 28 | 35 |
| **QE** | **+2.06** | **24.6** | **9** | **27** |

QE is positive on 8/12 roots and captures 7.3% of the stratum's
classification-preserving oracle. Hindsight classification fallback leaves
about +1.79%; exact-value fallback about +0.35%. Baseline cost is future
information, so this stratum demonstrates concentration, not a deployable gate.

## Diagnostics

Full-corpus held-out fold means:

- own-cut AUC: 0.874;
- global log-cost Spearman: 0.707;
- later-vs-baseline cheaper-cost AUC: about 0.54;
- within-row predicted cost argmin accuracy: reported in the machine result
  alongside the always-baseline comparator.

Global cost rank correlation is dominated by between-row node size and must not
be read as candidate-choice accuracy. The weak cheaper-than-baseline AUC
explains why CHEAP remains negative. Future models should directly predict
relative regret, pairwise preference or expected choice value rather than only
absolute whole-node cost.

## Interpretation and gate

1. The previous statement that every firing feature-only policy loses is
   withdrawn. Correctly transformed QE has a small positive root-held-out sign.
2. The magnitude is not yet economically sufficient for universal invocation:
   998 saved local nodes over 15,825 eligible calls is only 0.063 local nodes per
   call before inference overhead, and local subtree costs overlap.
3. The ≥10-node concentration supports a cheap learned gate or near-root/depth
   restriction, but measured baseline cost itself cannot be that gate.
4. This justified cheap nonlinear/direct-regret and broader game-held-out
   experiments. Those later results replicate the small offline sign, but the
   frozen live QE policy increases both nodes and wall time. The production
   gate therefore remains a NO-GO; see `../p7-game-paired/` and
   `../p7-live-qe/`.
