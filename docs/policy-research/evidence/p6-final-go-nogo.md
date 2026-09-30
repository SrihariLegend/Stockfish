# Revised Phase 6/7 go/no-go

This revision supersedes the report at `490a44c7`. Two review findings changed
the evidence: the original learnability scorer applied standardized-model
coefficients to raw features, and schema `/4` did not always execute its
requested permutation. Both are fixed and independently regression-tested.
Schema `/5`, two seeded game-paired corpora, nested direct-ranking models and a
live fixed-depth intervention now answer all five plan-11.5 questions.

All offline savings are ratios of sums over local eligible-node replays. They
are not additive root speedups because subtrees overlap and a live intervention
changes its future call distribution.

## Q1 — How much local cost can an oracle save?

Opening corpus (12 roots, 15,825 rows):

- 24.0% classification-preserving top-four force-first oracle;
- 12.75% exact-value oracle.

Two seeded game-paired corpora (24 games, 48 paired middlegame/endgame roots,
35,603 rows):

- 20.7% classification-preserving oracle;
- 9.2% exact-value oracle.

The opportunity is real and survives a broader phase-balanced corpus, though it
is smaller than in the selected opening roots.

## Q2 — Where is the opportunity?

Savings remain concentrated in nontrivial/high-cost rows. Natural MovePicker is
a strong prior: most rows have no cheaper later top-four candidate and most
natural move-loop cutoffs already occur on ordinal 0. Hindsight baseline-cost
strata have substantially more headroom, but baseline cost is unavailable at
entry and is not a deployable gate.

## Q3 — Is any of it learnable from entry features?

Yes, weakly. The original all-negative claim was caused by a feature-transform
bug and is withdrawn.

Corrected opening-root LOO:

- QE +0.60% pooled, positive on 10/12 roots;
- QE +2.06% in the hindsight baseline ≥10 stratum;
- CHEAP −0.40%, demonstrating that absolute cost prediction is insufficient.

Game-group-held-out replication over two disjoint seeded samples:

- QE +0.59% pooled, positive on 16/24 groups;
- QE +0.70% in the ≥10 stratum;
- direct weighted ridge +0.37% raw / +0.34% class-fallback;
- two-layer direct-regret MLP (seeds 7+19) +0.18% raw / +0.16% class-fallback.

Own-cut AUC remains strong (~0.86), but later-vs-baseline cheaper-cost AUC is
only ~0.56. The nonlinear model does not improve on linear QE. Realized offline
capture remains about 3% of the classification-preserving oracle.

## Q4 — How large is shared-order interaction?

Schema `/4` is rejected: 3,822 entries did not execute the requested prefix,
full-K controls were not semantically equivalent to force-next, and the
analyzer mislabeled swap(2,3) as cheapest-first.

Correct schema `/5` reserves the static prefix before search, serves it in the
requested order and resumes from the correct picker suffix. Validation:

- 5,282/5,282 valid rows;
- zero invalid prefixes;
- 15,846/15,846 exact force-next controls;
- canonical `/3` baseline/probe byte parity.

Aligned pooled references:

- scalar unconstrained/class/exact: 26.56% / 23.26% / 12.02%;
- best tested full schedule: 27.37% / 24.47% / 12.67%.

Additional full-schedule oracle value over the aligned scalar oracle is only
0.81pp raw, 1.21pp classification-preserving and 0.65pp exact. The plan-defined
cheapest-first-to-best gap is ~4.5–4.8pp, but mostly reflects imperfect
scalar-to-full-order conversion rather than extra deployable headroom.

Committed identity costs 2.03% more than natural baseline because natural
search may dynamically skip later quiets. Blind full-prefix scheduling remains
harmful; selecting the first candidate is the dominant opportunity.

## Q5 — Is a live policy affordable and composable?

No for the tested universally invoked policy.

A frozen linear QE model trained on all paired-game data was installed behind a
research-only switch and tested on 26 held-out `corpus-v3` roots. The live path
includes candidate enumeration, inference, actual force-first scheduling and
all resulting TT/history distribution changes.

Depth 8, seven trials/root:

| gate | node ratio | wall ratio | paired median wall ratio |
|---|---:|---:|---:|
| universal | 1.055 | 1.522 | 1.323 |
| decision depth ≥4 | 1.107 | 1.334 | 1.185 |
| decision depth ≥6 | 1.100 | 1.197 | 1.056 |

Depth 12, three trials/root:

| gate | node ratio | wall ratio | paired median wall ratio |
|---|---:|---:|---:|
| universal | 1.219 | 1.741 | 1.518 |
| decision depth ≥4 | 1.363 | 1.594 | 1.254 |
| decision depth ≥6 | 1.581 | 1.631 | 1.084 |

All modes fail both node and wall-time gates. Depth gating reduces inference
calls but does not fix harmful on-policy tails. Exact fixed-depth bestmove/score
changes are frequent; they are not labeled good or bad without deeper
references, but they independently block a strength claim.

## Revised decision

### NO-GO: universal or broadly invoked proof scheduler

The central universal-policy hypothesis is rejected for the measured feature
set/model family. Positive offline local capture does not compose on-policy,
and inference/candidate-enumeration overhead is far above the affordable global
budget. No Phase-10+ production integration, NNUE coupling, Elo claim or SPRT is
justified.

### Narrow research GO only

Further work is justified only if it changes the architecture materially:

1. an extremely sparse, cheap, high-precision gate learned and evaluated
   directly on-policy;
2. no full candidate enumeration at the majority of nodes;
3. a prespecified live fixed-depth node-and-wall gate passed on game-held-out
   roots before any Elo test;
4. deeper-reference checks for every root-result change.

The local oracle remains scientifically useful and may guide targeted search
heuristics. It no longer supports a universally invoked learned scheduler as a
credible path to substantially stronger Stockfish.
