# Phase 7 game-paired breadth and direct-ranking experiment

This experiment addresses the Phase-6 corpus/generalization gap with two
disjoint seeded samples of actual engine-match games and game-grouped holdout.
It also runs the first nested direct-regret ridge/MLP policy probe.

## Frozen corpus

Source PGN: `games.pgn` from local match `m_20260827_092100_r15`, SHA-256
`4adacac9e2065ef1aa4f987c9f0f7a410d336949ab9a34c0938744b8d9cc96f4`.
The selected FENs, game indexes and headers are committed, so analysis does not
depend on the external PGN.

Two disjoint game samples:

| member | selection seed | ply seed | game groups | roots |
|---|---:|---:|---:|---:|
| `game-paired-v1.json` | 20260906 | 20260907 | 12 | 24 |
| `game-paired-v2.json` | 20260916 | 20260917 | 12 | 24 |

Each selected game contributes one randomly selected middlegame and one
randomly selected endgame position. Eligibility:

- middlegame: ply 32–70, both queens, at least 20 pieces, not in check;
- endgame: ply ≥70, no queens, at most 18 pieces, nonterminal, not in check.

Both roots from a game always share `game_group` and are held out together.
`build_game_corpus.py` records exclusions so the second sample cannot reuse a
first-sample game. This is broader and randomized relative to the opening
corpus, but all games still come from one engine match and are not a universal
chess-position population.

## Collection

Research engine `98fa4f9d`; depth 8, Threads 1, Hash 16, sample rate 1.0,
TopK 4, node budget 5,000. Raw schema `/3` logs are in sibling directories
`p7-game-paired-v1/` and `p7-game-paired-v2/`.

- 48 roots / 24 game groups;
- 35,603 decision rows;
- 401,508 baseline local replay nodes;
- zero censored probes;
- 20.7% classification-preserving top-four oracle;
- 9.2% exact-value oracle;
- blind ordinal 1/2/3 costs +26.3%/+28.8%/+30.4% pooled.

The oracle is smaller than the 24.0% opening-corpus result but remains material
and appears in both phases.

## Game-group-held-out linear replication

The corrected QE analysis holds out both positions from each game. Pooled over
24 untouched games:

| policy | full corpus | baseline ≥10 |
|---|---:|---:|
| Q | −0.00% | +0.12% |
| CHEAP | −0.38% | −6.80% |
| CHEAP_SAFE | +0.03% | +0.10% |
| **QE** | **+0.59%** | **+0.70%** |

Full QE costs 399,155 vs 401,508 baseline nodes and fires on 6.9% of rows. It
is positive on 16/24 game groups, negative on 7 and tied on one; root-group
mean 0.65%, SD 0.99%. On the hindsight ≥10-node stratum it is positive on
20/24 groups (mean 1.40%, SD 2.18%).

Diagnostics remain consistent with the opening corpus: own-cut AUC 0.857,
global log-cost Spearman 0.669, but decision-relevant later-vs-baseline cost
AUC only 0.557. Always-baseline identifies the within-row cost argmin on 74.3%
of groups' rows versus 67.9% for the cost model. Absolute-cost prediction is
not enough for choice.

## Nested direct-regret models

`p7_direct_ranking.py` uses only entry-state features. For every outer held-out
game, a three-fold inner game split produces out-of-group predictions used to
tune an abstention threshold. The final model is trained on all remaining
games. Training utility is signed-log node gain, with a one-baseline-cost
penalty for a fail-high classification change. Evaluation reports raw measured
cost plus hindsight class/exact fallbacks.

Combined-corpus results:

| model | raw saving | class fallback | exact fallback | positive groups |
|---|---:|---:|---:|---:|
| direct weighted ridge | +0.37% | +0.34% | +0.19% | 21/24 |
| two-layer MLP, seeds 7+19 | +0.18% | +0.16% | +0.08% | 15/24 |
| direct ridge, baseline ≥10 | +1.24% | +1.06% | +0.27% | 21/24 |

The nonlinear model does not outperform the linear baselines. This is still a
positive result in one narrow sense: the opening-corpus +0.60% QE finding
replicates almost exactly (+0.59%) under disjoint seeded game-group holdout.
But realized capture remains only about 3% of the local oracle.

## Reproduction

```sh
python3 tools/policy_research/build_game_corpus.py ...
python3 tools/policy_research/collect_internal_corpus.py ...
python3 tools/policy_research/p6_learnability.py \
  docs/policy-research/evidence/p7-game-paired-v1 \
  docs/policy-research/evidence/p7-game-paired-v2 \
  --group-manifest tools/policy_research/corpora/game-paired-combined.json ...
PYTHONPATH=tools/policy_research python3 \
  tools/policy_research/p7_direct_ranking.py ...
```

Exact commands and machine outputs are represented by the committed corpus
manifests, JSON results and text reports.

## Decision

The broader-data and positive-held-out prerequisites for a live research test
are satisfied, weakly. They do not satisfy a production gate: local replay
savings are small, overlapping, and do not include inference cost or on-policy
state-distribution changes. Those are measured next in `../p7-live-qe/`.
