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

| file | ord | FH% | FH->FL% | FL->FH% | forced/baseline node ratio |
|---|---|---|---|---|---|
| seed_a1 | 0 | 58.8 | 0.0 | 0.0 | 1.000 |
| seed_a1 | 1 | 58.6 | 1.4 | 1.2 | 2.170 |
| seed_a1 | 2 | 58.3 | 1.5 | 1.0 | 2.366 |
| seed_a1 | 3 | 58.1 | 1.6 | 1.0 | 2.557 |
| root_b_d8 | 0 | 61.8 | 0.0 | 0.0 | 1.000 |
| root_b_d8 | 1 | 61.0 | 1.8 | 0.9 | 2.303 |
| root_b_d8 | 2 | 61.6 | 1.8 | 1.6 | 2.265 |
| root_b_d8 | 3 | 61.7 | 1.8 | 1.7 | 2.400 |
| root_b_d10 | 0 | 60.3 | 0.0 | 0.0 | 1.000 |
| root_b_d10 | 1 | 59.7 | 1.4 | 0.8 | 2.179 |
| root_b_d10 | 2 | 60.0 | 1.5 | 1.2 | 2.143 |
| root_b_d10 | 3 | 60.2 | 1.4 | 1.4 | 2.286 |

**Corrected cost picture (important).** With the sibling-context fix, blind
promotion is substantially costlier than the pre-fix /2 corpora suggested:
pooled forced/baseline node ratios are ~2.14-2.56 at ordinals 1-3 (the /2
files showed ~1.20-1.35). The whole-node fail-high rate stays flat because
the natural lead move still cuts off after the forced candidate fails low
(prefix re-search); the extra ~110-140% is the price of always searching the
promoted candidate at full slot-1 depth (no LMR, moveCount == 1) before the
natural cutoff is found. Per-visit: at ordinal 1, forcing is cheaper on
17.6-19.9% of rows, equal on 28.6-30.8%, costlier on 49.3-53.6%; at
ordinals 2-3 cheaper on 11-15%, costlier on 63-77%.

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
and the driver of the promotion cost.

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

Blind fixed-ordinal promotion costs 114-156% while the classification-
preserving local oracle saves 19-28%: the adaptive-selection hypothesis
(learn a per-candidate, context-conditioned policy that preserves natural
order on most nodes and promotes where cheaper) survives the corrected
measurements; the interaction-gap between the oracle and any learnable rule
is the Phase-6 go/no-go question.

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
