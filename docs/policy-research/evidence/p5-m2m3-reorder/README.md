# Phase 5 M2/M3 reorder-estimand dataset analysis

Post-review evidence for the corrected estimand (entry-hook whole-node replay,
slot-1 force-next with prefix-preserving reorder, `internal-counterfactual/2`).

## Run provenance

- Engine: research build at `fe046e92` (`make research-build ARCH=x86-64-avx2 COMP=gcc`),
  macro-off bench parity 453,169 nodes unchanged.
- UCI: `Threads 1`, `Hash 16`, `PolicyResearchMode internal_counterfactual`,
  `PolicyResearchSampleRate 1.0`, `PolicyResearchNodeBudget 800`,
  `PolicyResearchSeed 0`, root `position startpos moves e2e4 c7c5 g1f3 d7d6`,
  `go depth 8`.
- Dataset: `seed_a1.jsonl.gz` (this directory) — 1,356 decision rows, 1,356
  `node_exit` audit rows, one `run_start`/`root_start`/`root_end`; decompress
  to validate (`gzip -dc`). Seed-1 run (`/tmp/seed_b1.jsonl`) selects the same
  node set with different sample sub-hashes; a second seed-0 run reproduces the
  seed-0 file exactly (decision rows byte-identical).

## Validation results

- Deterministic sampling identity: seed-0 rerun produced an identical decision
  row set (1,356 rows, same keys/values); seed-1 differs only in sample seeds.
- Decision/node_exit join: all 1,356 decisions have exactly one `node_exit`
  with the identical `(root_key, pos_key, ply, entry_depth, sample_seed)`.
- Live-vs-replay node determinism (287 sampled nodes with >= 10 nodes on both
  sides): median live/baseline ratio **1.000**, p10 0.938, p90 1.000,
  mean 0.987, 96.9% within 0.5x–2.0x — the isolated baseline replays reproduce
  the live subtree costs almost exactly (deviations from live TT warmth /
  eviction).
- Censored baselines: 0 (fixed-depth root with budget 800 fully covers the
  sampled subtree costs).

## Ordinal outcome shape under genuine slot-1 reorder

`forced FH` = the ordinal-k candidate still cuts off (replayed node value
>= beta) when forced into slot 1. `FH->FL` = the node's baseline cut (some
natural earlier move >= beta) no longer cuts when candidate k is moved to slot
1 — the prefix is re-searched at its shifted slots, and moveCount-dependent
pruning (LMR etc.) can genuinely change the outcome.

| ordinal | probes | forced FH % | FH->FL % |
|---|---|---|---|
| 0 | 1356 | 58.8 | 0.0 |
| 1 | 1356 | 54.2 | 9.3 |
| 2 | 1356 | 50.9 | 14.7 |
| 3 | 1356 | 52.4 | 12.7 |
| 4 | 100 | 53.0 | 7.4 |
| 5 | 95 | 52.6 | 11.1 |
| 6 | 90 | 45.6 | 22.4 |
| 7 | 115 | 58.3 | 12.0 |
| 8 | 96 | 49.0 | 17.5 |
| 9 | 106 | 52.8 | 11.5 |
| 10 | 110 | 52.7 | 13.6 |
| 11 | 89 | 56.2 | 2.0 |
| 12 | 94 | 51.1 | 20.7 |
| 13 | 101 | 58.4 | 12.1 |
| 14 | 94 | 60.6 | 9.8 |
| 15 | 96 | 63.5 | 4.9 |
| 16 | 105 | 50.5 | 17.5 |
| 17 | 100 | 54.0 | 20.3 |
| 18 | 91 | 41.8 | 24.0 |
| 19 | 94 | 50.0 | 16.4 |
| 20 | 94 | 47.9 | 18.2 |
| 21 | 103 | 63.1 | 13.5 |
| 22 | 115 | 40.9 | 20.7 |
| 23 | 102 | 56.9 | 15.9 |
| 24 | 100 | 53.0 | 16.4 |
| 25 | 108 | 46.3 | 15.8 |
| 26 | 116 | 52.6 | 20.0 |
| 27 | 96 | 51.0 | 25.4 |
| 28 | 74 | 50.0 | 23.3 |
| 29 | 54 | 35.2 | 30.8 |

Aggregate: 4,714 baseline-FH probes across ordinals; 11.7% flip to fail-low
when moved to slot 1 (9.3% at ordinal 1 rising to ~16–31% deep in the tail).

Key controls:

- **Ordinal 0 is bit-identical by construction** (forcing the natural first
  emission reorders nothing): forced FH 58.8% == baseline FH 58.8%, FH->FL
  0.0% — confirmed again here at dataset scale.
- The shape is fundamentally different from the pre-fix deletion semantics:
  the natural cutoff move is *re-searched* after the forced move fails low, so
  later ordinals keep most of their cutting power (FH% decays only mildly with
  ordinal) instead of being deleted outright.

Note on reading `value` in /2 rows: for a forced probe, `value >= beta`
means the whole reordered node still failed high (forced move itself, or the
re-searched prefix/suffix reached beta); `value < beta` means the reordered
node failed low. Both are genuine reorder outcomes of the engine's own slot
semantics; they are not directly comparable to the baseline value (LMR and
related pruning act on the shifted slots).

## Files

- `seed_a1.jsonl.gz` — full seed-0 dataset (gzip; 13,388,646 bytes raw,
  809,129 compressed). Validation: first line `run_start`, second `root_start`,
  last line `root_end`; 1,356 decision + 1,356 node_exit rows; `root_end.rows
  == 1356`, `root_end.bytes` == decompressed file size, `overflow == false`.
