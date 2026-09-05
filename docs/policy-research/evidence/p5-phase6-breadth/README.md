# Phase-6 breadth corpus — 10 independent roots (schema internal-counterfactual/3)

Breadth evidence for the Phase-6 oracle/ratio studies: ten independent
root searches across openings, collected with the same fixed-engine protocol
as the p5-m2m3-reorder-v3 set (research build at `5da89d37`, `Threads 1`,
`Hash 16`, `PolicyResearchMode internal_counterfactual`, sample rate 1.0,
`TopK 4`, `NodeBudget 800`, `go depth 8`, seed 0, one process per root).

## Roots

| file | opening line |
|---|---|
| b1_ruy | 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 |
| b2_najdorf | 1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6 5.Nc3 a6 |
| b3_qgd | 1.d4 d5 2.c4 e6 |
| b4_kid | 1.d4 Nf6 2.c4 g6 3.Nc3 Bg7 |
| b5_caro | 1.e4 c6 2.d4 d5 |
| b6_qga | 1.d4 d5 2.c4 dxc4 |
| b7_french | 1.e4 e6 2.d4 d5 |
| b8_nimzo | 1.d4 Nf6 2.c4 e6 3.Nc3 Bb4 |
| b9_slav | 1.d4 d5 2.c4 c6 |
| b10_sicil-nf3 | 1.e4 c5 2.Nf3 e6 3.d4 cxd4 4.Nxd4 Nf6 |

12,999 decision rows + one node_exit each; every file: schema /3 rows,
`root_end` present with `completed == true`, `overflow == false`, unique
per-visit `sample_id`, decision/node_exit joins exact, and
`baseline.nodes == live_subtree_nodes` on every row (0 mismatches).

## Headline numbers (12 roots incl. the p5-m2m3-reorder-v3 canonicals; 15,825 rows)

Pooled forced/baseline whole-node ratios at ordinals 1-3 (census):
**2.19 / 2.29 / 2.40**; root-level ordinal-1 ratio mean 2.16, sd 0.20,
range 1.76-2.50 (all 12 roots between ~1.8x and ~2.5x — blind slot-1
promotion is harmful everywhere, consistently ~2x costlier than the natural
order). Whole-node fail-high rates stay flat (55-63% per root at every
ordinal; FH->FL 0.8-2.5% at ordinal 1) because the natural lead move
re-cuts after the forced candidate fails low.

Measurement-A: 85-92% of natural move-loop cutoffs happen on the ordinal-0
move; a forced ordinal-1 candidate itself cuts off on 27-35% of rows (ord 2
19-21%, ord 3 11-13%), always after a full slot-1 (unreduced) search.

Classification-preserving local-oracle savings (cheapest ordinal 0..3 whose
forced replay preserves the baseline fail-high status), per root:
19.1-29.6% of baseline nodes (mean 23.1%, sd 3.6%), with a later ordinal
chosen on 21-30% of rows. Pooled: 166,807 -> ~128k baseline nodes
(~23%). Restricted to baseline >= 10-node rows the savings are larger
(21-34% across the canonical set). Exact-value equivalence bounds the
learnable gain from below at 9-18% (canonical set).

## Interpretation for the go/no-go

The gap between blind promotion (costs ~2.2x) and the classification-
preserving oracle (saves ~23%) is the room a learnable, context-conditioned
proof-scheduler policy would occupy. The consistency across 12 independent
roots (ratio sd 0.20 at the root level) means the Phase-6 q/e and
interaction-gap studies can be run on this corpus with root-clustered
uncertainty. Whether a model with entry-state features can realize a
meaningful share of that gap (and survive global interaction) is the
decisive Phase-6 question; nothing in this corpus contradicts the universal
policy endpoint, and the corrected measurements now show both sides of the
economics honestly.

Reproduction: `tools/policy_research/p5b_analysis.py
docs/policy-research/evidence/p5-phase6-breadth`.
