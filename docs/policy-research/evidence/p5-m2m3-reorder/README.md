# Phase 5 M2/M3 reorder-estimand dataset analysis

Post-review evidence for the corrected estimand (entry-hook whole-node replay,
slot-1 force-next with prefix-preserving reorder, `internal-counterfactual/2`),
regenerated on the frame-bound research engine after the forced-probe
nondeterminism root-cause fix (see "Determinism root cause and fix" below).
The earlier seed-0 dataset shipped at `fe046e92` measured forced-probe rows
corrupted by the stale-prefix-pop defect and is superseded by this file set.

**Supersession note (post-`08cb5cdb` / post-`5da89d37`):** the corpora in
this directory were collected on the engine BEFORE two later fixes, so their
replay node counts and all node-cost tables below are pre-fix numbers:

1. `08cb5cdb` root-caused the residual baseline-vs-live subtree mismatches
   ("warm-TT artifacts" below) to missing sibling fail-high context: a
   node's move loop reads `(ss+1)->cutoffCnt` (children of earlier siblings
   included) for LMR adjustments, and the replay's future-frame zeroing made
   every replay diverge from the live continuation whenever the live node
   followed cutting siblings (tt_hit cut nodes; live counts up to hundreds of
   do_moves smaller). The frame rebind now mirrors the live future-frame
   `cutoffCnt`, and replay node counts equal live subtree counts on every
   row (860/860 in the fix-validation smoke run; per-row parity field
   `baseline.ss1_cutoff_cnt`). Node-cost comparisons from these /2 files are
   therefore approximate (pre-fix replays were marginally cheaper on the
   affected rows); outcome classifications (FH stability) are unaffected.
2. `5da89d37` bumped the dataset schema to `internal-counterfactual/3` and
   added the measurement-A attribution (`first`/`cutoff` per replay,
   `prefix_pops`), which this file set does not contain.

Regeneration of the corpus set under schema /3 on the fixed engine is the
next evidence milestone (see `docs/policy-research/overview.md`).

## Run provenance

- Engine: research build at `c186fc9e` + remediation commit `bdd94d32`
  (frame-token binding; `position.cpp` `set_check_info` + `thread.h` hygiene).
  Macro-off parity: `bench 16 1 10 default depth` = **453,169** nodes;
  plain `bench` = 2,497,913 nodes (identical to pristine HEAD).
- UCI: `Threads 1`, `Hash 16`, `PolicyResearchMode internal_counterfactual`,
  `PolicyResearchSampleRate 1.0`, `PolicyResearchNodeBudget 800`,
  `PolicyResearchTopK 4` (probe-all for `N <= 8`), fixed depth.
- Roots:
  - root A = `startpos moves e2e4 c7c5 g1f3 d7d6`, depth 8, seeds 0 and 1
    (`seed_a1.jsonl.gz`, `seed_a2.jsonl.gz`);
  - root B = `rnbqkb1r/pppp1ppp/4pn2/8/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 0 3`
    (1.d4 Nf6 2.c4 e6), depth 8 and depth 10, seed 0
    (`root_b_d8.jsonl.gz`, `root_b_d10.jsonl.gz`).
- One process per root (`go`-to-`quit` pacing); files gzip-compressed.

## Determinism root cause and fix

**Symptom.** Same-seed corpus reruns differed in 61-64 decision rows under
default ASLR and were byte-identical with ASLR disabled (`setarch -R`).
Differences were confined to forced-probe rows whose probes had non-empty
prefixes (the hash-sampled marginals, sel >= 4); baselines and top-K rows
were stable. Valgrind pinpointed reads of uninitialized `checkSquares[0]` in
`Position::gives_check` (empty from-square), and debug instrumentation
captured six stale-move emissions (c7c6 / e7e6 / a7a6 at positions where
those pawns had already moved).

**Root cause.** `force_next_buffer_pop()` (the prefix drain at the main
moves-loop head) was not bound to the armed node: after the forced candidate
was consumed at the armed node, the *first descendant search frame* drained
the buffered prefix instead of the armed node's own loop. Stale prefix moves
(legal in the armed node) then reached descendant nodes as if emitted by
their own pickers, passed the weak `Position::legal()` filter (no from-square
occupancy/color check), and read `checkSquares[0]` — an index
`set_check_info()` never writes (only 1..6 of the 8-entry array) — inside
`gives_check`, producing ASLR-dependent pruning. The same family of defect
(audit item #4: arms not bound to the exact search frame) lets
same-`(posKey, ply)` re-entry frames (singular-extension re-searches,
null-move-verification re-searches) consume arms, drain the prefix, or pop
the live oracle in place of the sampled frame.

**Fix (in this engine build).**
1. `force_next_buffer_pop(posKey, ply, move)` — pops only at the armed
   node's own `(posKey, ply)` (search.cpp loop head passes both).
2. Frame-token binding (`ResearchFrameScope` at every search() frame entry +
   `research_frame_enter`): capture and force-next arms bind to the first
   frame entering their `(posKey, ply)` — the replay root — and the
   decision-point capture, the force-next protocol, and the live-subtree
   oracle all require the innermost frame token to match the bound token.
3. `set_check_info()` now writes `checkSquares[0]` and `[7]` (defensive
   hygiene; the stale-move path is dead after 1-2).
4. `ThreadPool::stop/increaseDepth` initialized in the class (removes the
   last two uninitialized-read artifacts).

**Post-fix verification.** Valgrind sandbox: **0 errors, 0 contexts**
(previously 8); `SANDBOX_TEST_OK`. Same-seed corpus reruns with default ASLR
on two separate processes are **byte-identical** (1,356 decision + 1,356
node_exit rows, 0 differing). Live search stays bit-identical armed vs
unarmed (sandbox live-non-perturbation test). Macro-off bench parity
unchanged (453,169).

## Validation results (fixed engine)

- Sampling identity: seed-0 and seed-1 root-A runs record the same 1,356
  decision nodes (top-K rows are a census of the eligible decision nodes;
  marginal draws differ by seed, as designed).
- `root_key` on every row equals the `root_start` root key (previously the
  mutable mid-search `rootPos.key()` was recorded, breaking the join to the
  lifecycle rows at ply > 0).
- Decision/node_exit join: one `node_exit` per `decision` on the per-visit
  `sample_id` (monotonic 1..N per run). The former 5-field identity key
  collides for 120/1,356 root-A identity keys (305 visits, i.e. 185
  duplicate occurrences from repeated visits of the same node at equal
  depth), which is why `sample_id` was added.
- Live-vs-replay node determinism (baseline nodes vs live subtree from the
  oracle): root A 98.3% exact (max |delta| 18), root B 94.6-94.9% exact
  with deltas up to a few hundred, all on `tt_hit` nodes. **Post-`08cb5cdb`
  this is understood and fixed**: the deltas were sibling `(ss+1)->cutoffCnt`
  LMR context the replay's zero-initialized future frames lacked (the live
  continuation had it), not warm-TT artifacts; the rebind now mirrors the
  live values and replay node counts match the live subtree on every row.
  The tables below predate that fix (see the supersession note above).

## Ordinal outcome shape under genuine slot-1 reorder (root A, seed 0, depth 8)

`forced FH` = the ordinal-k candidate still cuts off when forced into slot 1.
`FH->FL` = the node's baseline cut (some natural earlier move >= beta) no
longer cuts when candidate k is forced to slot 1 — the prefix is re-searched
at shifted slots, so moveCount-dependent pruning can change the outcome. The
top-K ordinals (0-3) are a census (1,356 probes each, weight 1); tail
ordinals are the hash-sampled marginals, reported with Hájek/HT weights
(`1/prob`, prob = 2/(N-K)) so the tail means are unbiased for the full
candidate population.

| ordinal | probes | weighted | forced FH % | FH->FL % |
|---|---|---|---|---|
| 0 | 1356 | 1356 | 58.8 | 0.0 |
| 1 | 1356 | 1356 | 58.6 | 1.4 |
| 2 | 1356 | 1356 | 58.3 | 1.5 |
| 3 | 1356 | 1356 | 58.1 | 1.6 |
| 4 | 114 | 1553 | 63.6 | 1.1 |
| 5 | 100 | 1353 | 57.0 | 1.0 |
| 6 | 95 | 1314 | 57.0 | 0.0 |
| 7 | 86 | 1169 | 57.1 | 0.0 |
| 8 | 91 | 1210 | 56.1 | 0.0 |
| 9 | 102 | 1407 | 62.7 | 1.2 |
| 10 | 92 | 1214 | 65.6 | 1.1 |
| 11 | 90 | 1203 | 59.3 | 0.0 |
| 12 | 114 | 1551 | 61.2 | 4.4 |
| 13 | 118 | 1629 | 58.1 | 0.9 |
| 14 | 83 | 1132 | 51.9 | 2.5 |
| 15 | 93 | 1256 | 66.1 | 0.0 |
| 16 | 108 | 1465 | 53.1 | 3.8 |
| 17 | 115 | 1572 | 56.9 | 4.2 |
| 18 | 76 | 1033 | 59.5 | 2.5 |
| 19 | 83 | 1117 | 69.9 | 1.1 |
| 20 | 115 | 1557 | 57.4 | 2.4 |
| 21 | 95 | 1292 | 61.8 | 0.0 |
| 22 | 93 | 1268 | 54.6 | 1.3 |
| 23 | 96 | 1347 | 59.3 | 1.1 |
| 24 | 103 | 1375 | 55.9 | 3.0 |
| 25 | 117 | 1592 | 66.7 | 0.9 |
| 26 | 97 | 1319 | 53.2 | 1.1 |
| 27 | 86 | 1178 | 54.4 | 0.0 |
| 28 | 91 | 1255 | 54.1 | 2.0 |
| 29 | 52 | 788 | 40.1 | 0.0 |
| 30 | 48 | 773 | 41.8 | 3.7 |
| 31 | 29 | 463 | 51.3 | 4.1 |
| 32 | 29 | 492 | 43.2 | 3.5 |
| 33+ | 101 | 1866 | 45.2 | 0.9 |

The pre-fix tables (noted in earlier evidence) showed FH->FL rates of 9-22%
at ordinals 1-3; those were artifacts of the stale-prefix corruption. The
fixed engine shows the honest prefix-preserving reorder: slot-1 forcing
changes an ordinal-k candidate's own fail-high outcome only marginally
(FH rate ~58% at every ordinal — the natural slot-1 base rate), and FH->FL
rates are small (0-4%): earlier candidates survive being shifted almost
always, exactly what prefix-preserving reorder semantics predicts.

## Files

- `seed_a1.jsonl.gz` — root A, seed 0, depth 8: 1,356 decision + 1,356
  node_exit rows + lifecycle rows (raw 13,431,039 bytes; gzip 803,095).
  First line `run_start`, second `root_start`, last `root_end`;
  `root_end.rows == 1356`, `root_end.bytes` == decompressed size,
  `overflow == false`, `io_failed == false`.
- `seed_a2.jsonl.gz` — root A, seed 1, depth 8 (1,356 rows).
- `root_b_d8.jsonl.gz` — root B, seed 0, depth 8 (1,470 rows).
- `root_b_d10.jsonl.gz` — root B, seed 0, depth 10 (2,744 rows).

All four files carry schema `internal-counterfactual/2` (pre-fix replay node
counts; see the supersession note above).
