# Phase 7 live linear-QE affordability test

This is the first on-policy fixed-depth test after a positive game-group-held-out
offline result. It deliberately measures the complete naive implementation:
entry-state candidate enumeration, frozen linear QE inference, actual
force-first move scheduling, changed TT/history trajectories and wall time.

## Frozen policy

`linear-qe-model.json` and generated `src/policy_research/qe_model.h` are fit to
all 35,603 rows of the two paired game corpora. The policy uses the exact
training winsorization/standardization transform and fixed lambda 2.0 (selected
on 22/24 outer folds in the full-corpus group-held-out study). It considers the
static top four and keeps natural order when ordinal 0 has the best QE score.

The intervention is research-only and disabled by default:

```text
PolicyResearchLiveQE
PolicyResearchLiveQEMinDepth
```

When a later candidate wins, the live search reserves the actual MovePicker
prefix, serves `[chosen,0..chosen-1]`, and resumes from the correct suffix
cursor—the same semantics validated by schema `/5`. Counters are exposed by
`policy_research_live_qe_stats`.

## Benchmark protocol

- held-out corpus: 26 frozen `corpus-v3` roots, not used to fit the final model;
- Threads 1, Hash 16, fixed depth;
- fresh process per root/trial/mode;
- untimed depth-6 warmup, `ucinewgame`, clear hash, then timed search;
- randomized schedule with declared seed;
- baseline and three exploratory minimum-decision-depth gates;
- same research binary/configuration except for the live policy option;
- ratio-of-sums wall time and nodes; paired wall-ratio median also reported.

All node counts, best moves and scores were deterministic across repeated trials.
Wall time is noisier, especially at depth 8, but the effects are far larger than
that noise.

## Depth 8, seven trials per root

182 runs per mode; schedule seed 20260919.

| mode | node ratio | wall ratio | paired median wall ratio | calls | promotions | distinct roots with result change |
|---|---:|---:|---:|---:|---:|---:|
| off | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 |
| all eligible depths | **1.055** | **1.522** | **1.323** | 104,559 | 6,790 | 18/26 |
| decision depth ≥4 | 1.107 | 1.334 | 1.185 | 46,634 | 2,387 | 17/26 |
| decision depth ≥6 | 1.100 | 1.197 | 1.056 | 17,850 | 1,008 | 9/26 |

The universal policy increases searched nodes by 5.5% and wall time by 52.2%.
Its wall/node ratio is 1.44, exposing large enumeration/inference overhead.
Depth gating lowers overhead but does not recover node value.

At root level the universal policy improves node count on 12 roots, ties on 3
and worsens 11; the pooled loss comes from asymmetric harmful tails (root ratios
range 0.51–2.02). This is exactly the risk hidden by local replay averages.

## Depth 12, three trials per root

78 runs per mode; schedule seed 20260918. Root depths 9–12 are outside the
training corpus's root-depth range, so this is also an extrapolation stress test.

| mode | node ratio | wall ratio | paired median wall ratio | calls | promotions | distinct roots with result change |
|---|---:|---:|---:|---:|---:|---:|
| off | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 |
| all eligible depths | **1.219** | **1.741** | **1.518** | 369,579 | 27,363 | 23/26 |
| decision depth ≥4 | 1.363 | 1.594 | 1.254 | 194,571 | 12,522 | 24/26 |
| decision depth ≥6 | 1.581 | 1.631 | 1.084 | 132,336 | 7,302 | 18/26 |

The node tail is severe: individual root ratios reach 13.9, 18.1 and 27.0 for
the three policy modes. A near-neutral median therefore does not rescue pooled
performance.

“Result change” means exact fixed-depth bestmove/score differs from baseline;
it is not labeled good or bad without deeper references. Its frequency is an
additional warning against interpreting null-window classification preservation
as root-search equivalence.

## Conclusion

The live gate decisively fails:

1. Offline local QE savings (+0.59%) do not compose on-policy; the universal
   policy increases live nodes.
2. Candidate enumeration and linear inference are far above the affordable
   global per-call budget.
3. Simple depth gates reduce calls but worsen pooled nodes and remain slower.
4. On-policy state/history/TT distribution shift and correlated error tails are
   first-order effects, not a small correction to the replay oracle.

No production integration, Elo test or neural runtime work is justified for
this universal action. Further research, if any, must learn an extremely sparse
high-precision gate and validate it directly on-policy; it cannot rely on the
hindsight `baseline >= N` strata.
