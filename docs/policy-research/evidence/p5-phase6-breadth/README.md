# Phase-6 breadth corpus — 10 root positions (schema internal-counterfactual/3)

Breadth evidence for the Phase-6 oracle/ratio studies: ten separately
executed root searches across openings, collected with the same fixed-engine
protocol as the p5-m2m3-reorder-v3 set (research build at `5da89d37`,
`Threads 1`, `Hash 16`, `PolicyResearchMode internal_counterfactual`, sample
rate 1.0, `TopK 4`, fixed depth 8, seed 0, one process per root). The root
positions are manually selected opening lines — an exploratory breadth
sample, NOT a random sample from a deployment population (no middlegame/
endgame roots yet; all later rows are correlated within a root).

## Censoring note

`b8_nimzo` was originally collected at `PolicyResearchNodeBudget 800`, which
censored exactly two forced probes (rows 862 and 865; baselines 770/761
nodes, probes stopped at 800/801 — both were already costlier than their
baselines at censoring, so no oracle minimum was affected). The file was
re-collected at `NodeBudget 5000` and replaced; it now contains zero
censored probes. All other roots have zero censored probes at budget 800.

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

## Headline numbers (12 root positions incl. the p5-m2m3-reorder-v3 canonicals; 15,825 rows)

**Aggregate blind-promotion cost (plan §11.1: sum costs before dividing):**
pooled (sum forced)/(sum baseline) at ordinals 1-3 = **1.226 / 1.259 /
1.295** (+22.6% / +25.9% / +29.5% of search nodes). Per-root aggregate
ratios at ordinal 1: 1.046-1.339 (mean 1.241, sd 0.093). The previously
reported 2.19/2.29/2.40 figures are the arithmetic mean of per-row ratios
and must not be read as aggregate cost: they overstate the cost because tiny
subtree rows dominate the ratio average (a 1-node baseline doubled costs 2
nodes but shows ratio 2). The per-visit distribution is nevertheless
unfavorable everywhere: forcing ordinal 1 is cheaper on 19.0% of rows, equal
on 29.9%, costlier on 51.1% (ordinals 2-3: costlier on 65-73%). Whole-node
fail-high rates stay flat (55-63% per root at every ordinal; FH->FL
0.8-2.5% at ordinal 1) because the natural lead move re-cuts after the
forced candidate fails low.

Measurement-A (pooled 12-root set): 86.8% of natural move-loop cutoffs
happen on the ordinal-0 move (7,615/8,774); a forced ordinal-1 candidate
itself cuts off on 28.0% of rows (ord 2 18.6%, ord 3 12.7%), always after a
full slot-1 (unreduced) search; ~41% of oracle-selected promotions save
nodes through continuation effects rather than by the promoted candidate
itself cutting off.

Classification-preserving local-oracle savings (cheapest ordinal 0..3 whose
forced replay preserves the baseline fail-high status), per root:
19.1-29.6% of baseline nodes (mean 23.1%, sd 3.6%), with a later ordinal
chosen on 21-30% of rows. Pooled: 166,807 -> 126,753 (24.0%). Restricted
to baseline >= 10-node rows the savings are larger (89.6% of all savings
come from those 23.8% of rows; up to ~37% saved on baseline >= 100-node
rows). Exact-value equivalence: 12.75% pooled (root mean 12.15%, sd 2.48).

## Interpretation for the go/no-go

Twelve separately executed root positions (opening lines only; manually
selected, not a random sample from a deployment distribution — treat the
spread as exploratory, not as population inference). The gap between blind
promotion (aggregate +22-30% of nodes; per-row means much higher because of
tiny-subtree overweighting) and the classification-preserving oracle (saves
~19-30% per root) is the room a learnable, context-conditioned
proof-scheduler policy would occupy. The consistency across the 12 roots
(root-level aggregate-ratio sd ~0.09-0.11) means the Phase-6 learnability
and interaction-gap studies can be run on this corpus with root-clustered
uncertainty. Whether a model with entry-state features can realize a
meaningful share of that gap (and survive global interaction) is the
decisive Phase-6 question; nothing in this corpus contradicts the universal
policy endpoint.

Reproduction: `tools/policy_research/p5b_analysis.py
docs/policy-research/evidence/p5-phase6-breadth
docs/policy-research/evidence/p5-m2m3-reorder-v3/seed_a1.jsonl.gz
docs/policy-research/evidence/p5-m2m3-reorder-v3/root_b_d8.jsonl.gz
--measurement-a` (the breadth directory alone reproduces only the ten-root
numbers 1.212/1.254/1.289; the pooled figures above span the 12-root set
including the two canonical files).
