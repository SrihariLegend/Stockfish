# Phase 6.2 — learnability probe (root-held-out, abstaining, feature-only)

The decisive measurement the Phase-6.2 diagnostics could not provide:
can a policy that only sees pre-search entry features (a deployable shape)
realize any of the ex-post local-oracle headroom? Answer on this corpus:
**no — every tested feature-only rule loses to (or exactly ties) the
natural order when evaluated root-held-out; tuned gates abstain entirely.**

Reproduction: `tools/policy_research/p6_learnability.py
docs/policy-research/evidence/p5-phase6-breadth
docs/policy-research/evidence/p5-m2m3-reorder-v3/seed_a1.jsonl.gz
docs/policy-research/evidence/p5-m2m3-reorder-v3/root_b_d8.jsonl.gz
--json p6-learnability-results.json`

## Method

- Rows: the same 12-root internal-counterfactual/3 corpus (15,825 decision
  rows; top-four census). Labels per candidate ordinal 0..3: measured
  whole-node forced cost C_o (ordinal 0 = baseline) and measured slot-1
  own-cut indicator q_o (`cutoff_by_first`).
- Features: row context (ply, entry depth, remaining depth, root depth,
  improving, tt_hit, cut_node, rule50, n_candidates, static eval) plus
  per-candidate pre-search features from the row's candidate enumeration
  (MovePicker stage one-hot, stage score, main/capture/pawn/continuation/
  low-ply history, SEE bucket, check/capture/tt_move flags, natural
  ordinal). All are available before any candidate is searched; no future
  information enters the predictors. Features are winsorized at training
  1st/99th percentiles and standardized on training statistics per fold.
- Models: ridge logistic (IRLS) q-hat; ridge log-cost c-hat.
- Evaluation: leave-one-root-out over the 12 roots. Thresholds
  (theta/tau/lambda) are chosen by TOTAL measured cost on the 11 training
  roots over a grid that includes full abstention; the tuned policy then
  runs on every row of the held-out root and is charged the MEASURED cost
  of its chosen ordinal (abstain = ordinal 0). Aggregates are sums
  (plan 11.1).
- Policies: Q (promote the most probable own-cut candidate above a tuned
  threshold), CHEAP (promote the predicted cheapest whole-node cost among
  0..3), CHEAP_SAFE (CHEAP restricted to candidates whose predicted own-cut
  probability exceeds a tuned threshold), QE (promote argmax
  log q-hat - lambda * log c-hat over 0..3, lambda tuned).

## Results (pooled over the 12 held-out roots; baseline 166,807 nodes)

| policy | save% | later pick (% rows) | FH->FL | FL->FH |
|---|---:|---:|---:|---:|
| BASE | 0.00 | 0.0 | 0 | 0 |
| Q (theta tuned, abstain allowed) | 0.00 | 0.0 | 0 | 0 |
| CHEAP | -16.36 | 67.3 | 151 | 110 |
| CHEAP_SAFE (tau tuned) | 0.00 | 0.0 | 0 | 0 |
| QE (lambda tuned) | -4.27 | 19.4 | 75 | 35 |

Ex-post references (not learnable): ORACLE_CLS 24.01% saved; ORACLE_EXACT
12.75%; own-cut ex-post heuristics 13.7-15.7% (p6-explanatory report).

The tuned q-gates and the cost-gated rule chose FULL ABSTENTION on every
fold: even in-sample (training-root evaluation of the same tuned rules),
their best achievable pooled saving is 0.00% (Q, CHEAP_SAFE) and -4.4%
(QE). This is not a generalization failure: the rules' own training
criterion cannot find any promotion worth doing.

Diagnostics of feature information content (held-out): q-hat ranks
own-cut outcomes well (mean AUC 0.874, per-root 0.81-0.93) and c-hat ranks
log costs well (mean Spearman 0.703). The features DO carry ranking
signal; the ranking signal does not translate into choice value: choosing
argmin c-hat promotes on 67% of rows and loses 16%, because the model
cannot resolve the decision-relevant quantity — whether a later candidate
costs LESS than this row's natural order — at the precision the asymmetric
promotion penalty requires (promoting a non-cheaper candidate costs
~+20-30% of the row, and ~73% of rows have no cheaper top-four candidate).

Stratum check (rows with baseline >= 10 nodes, where 89.6% of the oracle
savings sit; oracle there: 28.33%): CHEAP -1.00%, CHEAP_SAFE -0.68%, QE
-0.96%, Q abstains (0.0%). Even in the high-opportunity stratum the
feature-only rules cannot clear zero.

## Reading

1. **The local oracle headroom is not capturable by cheap linear predictors
   on this corpus.** The ex-post gap (24%) is not evidence of a learnable
   policy; the measured learnable-share upper bound is currently 0%.
2. **The binding constraint is choice-value precision, not ranking
   quality.** AUC/Spearman are respectable; argmin/gated rules still lose
   because the payoff depends on predicting small per-row cost differences
   against a strong natural-order prior, where promotion errors are
   systematically expensive.
3. **Model-class caveat (honest limits of this probe):** only ridge
   linear/logistic models on the recorded tabular features were tested;
   12 roots are heavily correlated (opening lines, shared TT/history); the
   corpus has no game-random positions. A stronger model class (or
   features such as deeper NNUE-derived context) could in principle do
   better — nothing here rules that out — but the burden of proof now sits
   with such a model, and the plan §21.1 fallback (restrict to near-root/
   high-regret contexts, caching, no neural policy) is the live option if
   none materializes.
4. **Implication for the teacher target:** whole-node cost differences that
   an entry-state model cannot see are real (the oracle) but causally
   opaque from the entry; the interaction-gap corpus (shared permutations)
   will show how much of the oracle is intra-node order interaction that
   even a perfect scalar-cost model could not predict.
