# Phase 6.3 — shared-permutation interaction-gap corpus (schema /4, superseded)

> **Superseded by `../p6-interaction-v2/` (schema `/5`).** Schema `/4`
> included 3,822 replays whose observed slots were not a prefix of the
> requested order, conflated target skips with early cutoffs, used invalid
> full-K force-next controls, and mislabeled every swap(2,3) entry as
> `cheap` in the analyzer. This directory is retained only as an audit trail;
> its headline tables must not be cited.

First interaction-gap corpus measured with the plan-11.3 shared-permutation
engine mode (`1870e277`): each decision row carries a fixed battery of top-K
shared-permutation replays whose TT/history/cutoff context accumulates across
the K candidates inside one continuing replay. Four roots (5,282 decision
rows; protocol: Threads 1, Hash 16, internal_counterfactual, sample rate 1.0,
TopK 4, NodeBudget 5000, seed 0, depth 8): `seed_a1` (root A) and
`root_b_d8` (root B) are reruns of the canonical /3 roots (baselines and all
scalar probes byte-identical to the committed /3 files), plus `b1_ruy` and
`b3_qgd` breadth roots.

Reproduction: `tools/policy_research/p6_interaction.py
docs/policy-research/evidence/p6-interaction`.

## Battery and validation

Battery per row (K = min(4, N), deduplicated): identity `[0..K-1]` (control
vs the baseline replay), force-next controls `[k, 0..k-1, k+1..K-1]`
(control vs the ordinal-k scalar probe), reverse, rotation, adjacent swaps,
and the ex-post cheapest-first order built from the row's own scalar probe
costs. Every permutation entry carries nodes/completed/value/fail_high, the
slot-1 and final-cutoff attribution, per-slot records (move, ordinal,
searched/pruned, child cost, returned value for slots 1..8), served count
and `fully_served`.

Validation results:
- Scalar probes == force-next control permutations on 99.86% of rows (the
  few exceptions: root_b_d8 rows 590/867, values equal, node deltas +15/+4;
  caveat below).
- Identity permutation == baseline on 99.66% of rows (15/4,455 + 3/1,470
  flagged rows; values always equal). These flagged rows share one
  documented quirk: at a small fraction of nodes the capture-time candidate
  enumeration order differs from the node's own picker emission order
  (quiet-stage detail), so an enumeration-ordered identity can legitimately
  differ from the natural replay. It is a schema-level property of the /3
  ordinals (affects ~0.2-0.3% of rows, value-preserving), NOT a replay
  machinery failure: the probe/control path and all forced-replay semantics
  are unaffected.
- Zero censored permutations; zero duplicate orders (1 flagged row in
  b1_ruy); every row's node_exit join and root_end accounting complete.
- `fully_served == false` is legitimate: a cutoff before all K targets were
  served is natural search semantics.

## Results (pooled, ratio-of-sums; baseline 54,357 nodes, 5,282 rows)

| order | aggregate ratio | saved % |
|---|---:|---:|
| identity | 1.005 | -0.5 |
| ctrl1 [1,0,2,3] | 1.279 | -27.9 |
| ctrl2 [2,0,1,3] | 1.305 | -30.5 |
| ctrl3 [3,0,1,2] | 1.326 | -32.6 |
| reverse [3,2,1,0] | 1.493 | -49.3 |
| rotation [1,2,3,0] | 1.314 | -31.4 |
| swap(1,2) [0,2,1,3] | 1.010 | -1.0 |
| cheapest-first (ex-post scalar) | 0.902 | +9.8 |

Per-row ex-post references: best tested shared permutation (per row over the
whole battery, including multi-move reorders) saves 29.4%; the
classification-preserving scalar oracle saves 23.3%; the scalar cheapest-
first shared order saves 24.2% (its pooled cost is within a point of the
per-row scalar oracle — shared-permutation costs are almost fully predicted
by scalar whole-node costs).

## Interaction gap (plan 11.3)

The plan's interaction gap, C_shared(scalar-index order) minus
C_shared(best tested permutation), measured locally:

- Ordering the top-K by scalar whole-node costs (cheapest-first) realizes
  24.2% pooled; per-row adaptive best-of-battery reaches 29.4%. The
  **interaction headroom beyond scalar costs is ~5 percentage points of
  eligible subtree nodes**, and beyond the best single promotion
  (classification-preserving oracle, 23.3%) it is ~6pp. Multi-move order
  interactions inside a node are a real but secondary resource: the
  dominant, predictable component is the scalar per-candidate whole-node
  cost.
- Blind orderings are expensive everywhere (+27 to +49%); the only
  cheap non-identity order is the (1,2) swap (+1.0%), consistent with the
  earlier finding that ordinal-2-before-ordinal-1 is often harmless.
- Stratification and per-root spreads: reverse/rotation penalties and the
  cheap-first saving are directionally identical on all four roots
  (cheapest-first saving 6.9-12.0% per root; best-perm saving 26.5-32.6%).

Caveats: local interaction gaps do not compose globally (ancestor/descendant
policy composition, changed node distributions and on-policy shift still
require a live policy run); the two flagged control rows and the identity-
flagged rows (~0.2%) are value-preserving and excluded from strict
equivalence claims; K = 4 top ordinals only (tail candidates are not
permuted).
