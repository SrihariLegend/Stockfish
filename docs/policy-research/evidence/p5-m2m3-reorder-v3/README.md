# Phase 5 M2/M3 reorder-estimand dataset — corrected evidence (schema /3)

Regenerated evidence set for the corrected estimand on the FIXED engine:
whole-node entry replay with slot-1 force-next (prefix-preserving reorder),
schema `internal-counterfactual/3` (measurement-A attribution per replay),
collected after the sibling-`(ss+1)->cutoffCnt` replay-context fix
(`08cb5cdb`) and the measurement-A instrumentation (`5da89d37`).

The four `/2` corpora in `../p5-m2m3-reorder/` are PRE-FIX for replay node
counts (their replays lacked the live sibling fail-high context, so forced
replays understated the true cost of slot-1 promotion); outcome
classifications in those files are unaffected. All node-cost numbers below
supersede them. Pre-`fe046e92` evidence is superseded entirely.

## Run provenance

- Engine: research build at `5da89d37` (after `08cb5cdb`). Macro-off parity:
  `bench 16 1 10 default depth` = **453,169** nodes; plain `bench` =
  2,497,913 nodes.
- UCI: `Threads 1`, `Hash 16`, `PolicyResearchMode internal_counterfactual`,
  `PolicyResearchSampleRate 1.0`, `PolicyResearchNodeBudget 800`,
  `PolicyResearchTopK 4` (probe-all for `N <= 8`), fixed depth.
- Roots (same protocol as the /2 set):
  - root A = `startpos moves e2e4 c7c5 g1f3 d7d6`, depth 8, seeds 0 and 1
    (`seed_a1.jsonl.gz`, `seed_a2.jsonl.gz`);
  - root B = `rnbqkb1r/pppp1ppp/4pn2/8/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 0 3`
    (1.d4 Nf6 2.c4 e6), depth 8 and depth 10, seed 0
    (`root_b_d8.jsonl.gz`, `root_b_d10.jsonl.gz`).
- One process per root (`go`-to-`quit` pacing); files gzip-compressed.
  Rows: 1,356 / 1,356 / 1,470 / 2,744 decisions (+ one node_exit each).

## Validation on the fixed engine

- Baseline-vs-live node determinism: **0 mismatches on all 5,576 joined
  rows** (`baseline.nodes == node_exit.live_subtree_nodes` everywhere; joins
  on the per-visit `sample_id`). The old "warm-TT" residual is gone; the
  mechanism and fix are documented in `../reviews/phase5b-review-actions.md`
  (R2) and `data-schema.md`.
- Same-seed determinism: a second root-A seed-0 depth-8 process is
  **byte-identical** to `seed_a1.jsonl.gz` under default ASLR.
- Census identity across seeds: seed-0 and seed-1 root-A runs record the
  same decision-node multiset (identical `(pos_key, ply, entry_depth,
  root_depth)` counts; 120 identity keys visited twice+, covering 305
  visits), and the positional baseline rows (nodes, fail_high) agree on all
  1,356 rows.
- Ordinal-0 identity control: for every row, the ordinal-0 forced replay
  equals the baseline replay exactly in nodes, value, fail_high, and the
  measurement-A attribution (`first`, `cutoff`; 860/860 on the smoke set,
  spot-checked across all four files).
- `root_end` accounting complete: `rows == #decision`, no overflow/io
  failures, `completed == true` everywhere; every row joins its node_exit on
  a unique `sample_id`.

## Ordinal outcome shape under genuine slot-1 reorder (corrected)

`forced FH` = ordinal-k candidate's forced whole-node replay still fails
high; `FH->FL`/`FL->FH` = whole-node fail-high flips vs the baseline replay.
Top ordinals 0-3 are a census (probe-all); tail ordinals are the
hash-sampled marginals (Hájek/HT weighting with `1/prob`).

| file | ord | FH% | FH->FL% | FL->FH% | aggregate ratio | mean row ratio |
|---|---|---|---|---|---|---|
| seed_a1 | 0 | 58.8 | 0.0 | 0.0 | 1.000 | 1.000 |
| seed_a1 | 1 | 58.6 | 1.4 | 1.2 | 1.312 | 2.170 |
| seed_a1 | 2 | 58.3 | 1.5 | 1.0 | 1.301 | 2.366 |
| seed_a1 | 3 | 58.1 | 1.6 | 1.0 | 1.339 | 2.557 |
| root_b_d8 | 0 | 61.8 | 0.0 | 0.0 | 1.000 | 1.000 |
| root_b_d8 | 1 | 61.0 | 1.8 | 0.9 | 1.279 | 2.303 |
| root_b_d8 | 2 | 61.6 | 1.8 | 1.6 | 1.269 | 2.265 |
| root_b_d8 | 3 | 61.7 | 1.8 | 1.7 | 1.314 | 2.400 |
| root_b_d10 | 0 | 60.3 | 0.0 | 0.0 | 1.000 | 1.000 |
| root_b_d10 | 1 | 59.7 | 1.4 | 0.8 | 1.219 | 2.179 |
| root_b_d10 | 2 | 60.0 | 1.5 | 1.2 | 1.217 | 2.143 |
| root_b_d10 | 3 | 60.2 | 1.4 | 1.4 | 1.230 | 2.286 |

**Cost aggregation (plan §11.1).** Two different ratios are shown because
they answer different questions and the plan requires summing costs before
dividing. `aggregate ratio` = (sum of forced nodes over rows) / (sum of
baseline nodes over rows) for the ordinal's census probes — the correct
estimate of the total search-node cost change under always-promote.
`mean row ratio` = the arithmetic mean of per-row ratios; it is a purely
descriptive per-visit statistic and heavily overweights tiny subtrees
(turning a 1-node baseline into 3 is 3x on that row but costs 2 nodes), so
it must NOT be read as the aggregate cost multiplier.

**Corrected cost picture.** Blind slot-1 promotion costs **+22-34% of
aggregate search nodes** (aggregate ratios 1.22-1.34 at ordinals 1-3) —
not the 2.1-2.6x that the mean-row-ratio column might suggest. The
sibling-context fix (`08cb5cdb`) changed the *per-visit distribution* of
replay costs (live-faithful LMR context) but left the aggregate promotion
cost essentially where the /2 corpora placed it (~1.20-1.35). The whole-node
fail-high rate stays flat because the natural lead move still cuts off
after the forced candidate fails low (prefix re-search); the extra ~20-35%
is the price of always searching the promoted candidate at full slot-1
depth (no LMR, moveCount == 1) before the natural cutoff is found.
Per-visit (pooled, 12-root set): at ordinal 1, forcing is cheaper on 19.0%
of rows, equal on 29.9%, costlier on 51.1%; at ordinals 2-3 cheaper on
12-14%, costlier on 65-73%.

## Measurement-A attribution (first results)

`baseline.cutoff`: move-loop cutoffs (value >= beta break) account for
56-58% of rows (the rest fail low, or fail high via pre-loop paths — TT
cutoffs, razor/futility/ProbCut: 22-54 rows per file fail high without a
move-loop cutoff). Of the move-loop cutoffs, **85-92% happen on the natural
ordinal-0 move** — natural MovePicker order almost always cuts at its first
candidate.

Forced slot-1 probes (ordinals 1-3): the slot-1 candidate is searched on
100% of rows (never pruned before search at these ordinals), but the
candidate itself cuts off (`cutoff_by_first`) on only **27-35% (ord 1),
19-21% (ord 2), 11-13% (ord 3)** of rows — i.e., the forced candidate proves
the bound only about a quarter of the time even at ordinal 1, and the whole-
node outcome is then decided by the natural lead move re-searched at a
shifted slot. This is the direct causal confirmation of the flat-FH pattern
and the reason promotion costs aggregate nodes even though it changes
outcomes only rarely.

## Corrected local-oracle headroom (top four ordinals, post hoc)

Cheapest classification-preserving choice over ordinals 0-3 (forced probe
allowed only when its fail-high status matches the baseline):

- seed_a1: baseline 13,038 nodes -> oracle 10,544 (**19.1% saved**); a
  later ordinal chosen on 372/1,356 rows (27.4%).
- root_b_d8: 14,127 -> 10,747 (**23.9%**); later picks on 340/1,470 (23.1%).
- root_b_d10: 31,096 -> 22,408 (**27.9%**); later picks on 701/2,744 (25.6%).
- Restricted to baseline >= 10 nodes: **21.5% / 29.4% / 33.2%** saved.
- Exact-value equivalence (forced value == baseline value): 9.0% / 13.1% /
  17.7% saved.

Blind fixed-ordinal promotion costs +22-34% of aggregate nodes (the
2.1-2.6x figure is the mean of per-row ratios and overweights tiny subtrees;
see the aggregation note above) while the classification-preserving local
oracle saves 19-28%: the adaptive-selection hypothesis (learn a per-
candidate, context-conditioned policy that preserves natural order on most
nodes and promotes where cheaper) survives the corrected measurements; the
interaction gap between the oracle and any learnable rule is the Phase-6
go/no-go question.

## Files

- `seed_a1.jsonl.gz` — root A, seed 0, depth 8: 1,356 decision + 1,356
  node_exit rows (gzip 938,521 bytes).
- `seed_a2.jsonl.gz` — root A, seed 1, depth 8 (938,361 bytes).
- `root_b_d8.jsonl.gz` — root B, seed 0, depth 8: 1,470 rows (1,015,738).
- `root_b_d10.jsonl.gz` — root B, seed 0, depth 10: 2,744 rows (2,019,572).

All rows carry `schema == "internal-counterfactual/3"`. Each file: first
line `run_start`, second `root_start`, last `root_end`; `root_end.rows`
equals the decision count; `root_end.bytes` equals the decompressed size.

## Next steps (Phase 6)

Breadth corpus (many independent roots, more declared seeds, root-clustered
and Hájek/HT inference with intervals), the candidate-level q/e ratio study
using the measurement-A fields, and the interaction-gap report that decides
the go/no-go for the universal context-conditioned proof-scheduler policy
(see `docs/policy-research/reviews/phase5b-review-actions.md` and
`overview.md`).
