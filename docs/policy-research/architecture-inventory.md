# Architecture Inventory (Phase 0)

Snapshot of the Stockfish source on branch `policy-research` (base `06675f70`,
"update pairwise mul explanation"). All line numbers are approximate anchors; always
re-grep by symbol before editing. This document records only what was verified by
reading the source in this working tree.

Files examined: `src/search.{h,cpp}`, `src/thread.{h,cpp}`, `src/movepick.{h,cpp}`,
`src/position.{h,cpp}`, `src/tt.{h,cpp}`, `src/history.h`, `src/types.h`,
`src/evaluate.h`, `src/nnue/*` (architecture, accumulator, feature transformer,
features/half_ka_v2_hm, network), `src/engine.cpp`, `src/ucioption.cpp`,
`src/Makefile`, `tests/reprosearch.sh`.

---

# 1. Search

## 1.1 Entry points and overall flow

1. UCI `go` handling constructs `LimitsType` and calls
   `ThreadPool::start_thinking(...)` — `src/thread.cpp:297`.
   - Builds `Search::RootMoves` from `limits.searchmoves` or from a full
     `MoveList<LEGAL>(pos)` (`thread.cpp:309-322`).
   - Runs `Tablebases::rank_root_moves(options, pos, rootMoves)` (`thread.cpp:323`).
   - Copies `limits`, `rootMoves`, `rootPos` (from FEN), `rootState`, `tbConfig` into
     every worker via `run_custom_job` (`thread.cpp:337-362`).
2. `ThreadPool::start_searching()` → main thread `Worker::start_searching()`
   (`search.cpp:191`):
   - `accumulatorStack.reset()`; on the main thread: `tm.init(...)`,
     `tt.new_search()`, `threads.start_searching()`, then
     `iterative_deepening()`.
3. `Worker::iterative_deepening()` (`search.cpp:270`) is the per-thread iterative
   deepening loop; workers other than the main thread call it directly.
4. Inside the iterative-deepening loop, per `pvIdx` (MultiPV line), an aspiration
   window is set (`search.cpp:340-350`), and the loop calls
   `search<Root>(rootPos, ss, alpha, beta, adjustedDepth, false)`
   (`search.cpp:395`). Aspiration re-search on fail high/low happens at
   `search.cpp:409-430`.
5. `Worker::search<nodeType>` (`search.cpp:721`) is the recursive alpha-beta search.
6. `Worker::qsearch<nodeType>` (`search.cpp:1653`) is quiescence search.

## 1.2 Node types and templates

- `enum NodeType { NonPV, PV, Root }` — `src/search.h:37`.
- `search` is templated on `NodeType`; `Root` is a sub-mode of PV used only at the
  top-level call. `constexpr bool PvNode = nodeType != NonPV`,
  `constexpr bool rootNode = nodeType == Root` (`search.cpp:723-724`).
- `allNode = !(PvNode || cutNode)`, where `cutNode` is a runtime parameter
  (`search.cpp:725`).
- Invariants: `PvNode || (alpha == beta - 1)` (null-window) and
  `!(PvNode && cutNode)` (`search.cpp:742-744`).

## 1.3 Recursion structure inside `search()` (`search.cpp:721-1644`)

Step annotations from the source:

- Step 1 Initialize node (`search.cpp:764`): `ss->inCheck = pos.checkers()`,
  `ss->moveCount = 0`, `bestValue = -VALUE_INFINITE`; `ss->followPV` computed from
  parent state and `lastIterationIdxPV`.
- Step 2 Stop/draw/ply checks (`search.cpp:787`): `threads.stop`,
  `pos.is_draw(ss->ply)`, `ss->ply >= MAX_PLY`.
- Step 3 Mate distance pruning (`search.cpp:792`).
- Step 4 TT lookup (`search.cpp:814`): `tt.probe(posKey)` returns
  `(ttHit, ttData, ttWriter)`. `ttData.move` overridden by root move at root.
- Step 5 Static evaluation (`search.cpp:825`): via `evaluate(pos)` (NNUE,
  `search.cpp:1901`) or from TT, then corrected by correction history
  (`to_corrected_static_eval`). TT write of `unadjustedStaticEval` on miss.
- Step 6 Early TT cutoff at non-PV nodes (`search.cpp:872`), with graph-history
  partial workaround (`rule50 < 96`) and TT-penalize on bound mismatch.
- Step 7 Tablebase probe (`search.cpp:922`).
- Steps 8-12 Pruning before the move loop: razoring (`search.cpp:989`), futility
  child pruning (`search.cpp:994`), null-move search with verification
  (`search.cpp:1010`), internal iterative reductions (`search.cpp:1048`), ProbCut
  (`search.cpp:1054`).
- `moves_loop:` label (`search.cpp:1098`) — reached directly when in check.
- Step 13 small ProbCut (`search.cpp:1100`).
- **MovePicker construction at `search.cpp:1133-1137`:**
  `MovePicker mp(pos, ttData.move, depth, &mainHistory, &lowPlyHistory,
  &captureHistory, contHist, &sharedHistory, ss->ply)` where `contHist[]` is the
  array of the 6 prior plies' `continuationHistory` pointers
  (`search.cpp:1125-1130`, `(ss-1)`..`(ss-6)`).
- Step 14 move loop (`search.cpp:1145`): `while ((move = mp.next_move())...)`.
  - legality filter `pos.legal(move)`; root searchmoves/MultiPV filter.
  - `ss->moveCount = ++moveCount`.
  - reduction base: `int r = reduction(improving, depth, moveCount, delta)`
    (`search.cpp:1164`; `reduction()` defined `search.cpp:1885`).
- Step 15 shallow-depth pruning (`search.cpp:1171`): move-count based
  `mp.skip_quiet_moves()`; capture/check futility and SEE pruning; quiet-move
  history pruning and futility; negative-SEE pruning.
- Step 16 Singular extensions (`search.cpp:1234`): uses the `ss->excludedMove`
  mechanism with a recursive `search<NonPV>` on the same node; multi-cut pruning and
  negative extension.
- Step 17 make move (`search.cpp:1307`): `do_move(pos, move, st, givesCheck, ss)`.
- Step 18 LMR (`search.cpp:1313`): compute `r`, then if `depth >= 2 && moveCount > 1`
  do a reduced null-window search, with post-LMR fail-high full-depth re-search and
  depth adjustments (`doDeeperSearch`/`doShallowerSearch`, `search.cpp:1376-1390`).
- Step 19 full-depth search when LMR skipped (`search.cpp:1394`).
- Step 20 PV full-window re-search (`search.cpp:1406`).
- Step 21 undo (`search.cpp:1425`).
- Step 22 new-best-move handling, fail-high break, alpha update, and depth reduction
  on improvement (`search.cpp:1430-1560`). Root-move `effort` accumulation
  (`search.cpp:1462`), root score/EMA bookkeeping (`search.cpp:1471-1530`).
- Step 23 post-loop: mate/stalemate, `update_all_stats` when a best move exists
  (`search.cpp:1554`), countermove bonus/malus paths.
- Step 24 TT write (`search.cpp:1619`) and correction-history update
  (`search.cpp:1636`).

## 1.4 qsearch structure (`search.cpp:1653-1883`)

- PV/non-PV only; no Root. Early repetition-draw handling.
- TT probe; stand-pat (`bestValue >= beta` return); capture/evasion MovePicker
  (`DEPTH_QS`, only `(ss-1)->continuationHistory` in `contHist`);
  futility/moveCount/SEE pruning; recursive `-qsearch<nodeType>(-beta,-alpha)`.

## 1.5 Node counting and determinism

- `Worker::nodes` is a `RelaxedAtomic<u64>` (`search.h:376`), incremented in
  `Worker::do_move` (`search.cpp:654`, `++nodes;`), i.e., once per made move.
- Total = `ThreadPool::nodes_searched()` summing worker nodes
  (`thread.h` `accumulate(...)`).
- Determinism caveats found:
  - `Skill::pick_best` uses `static PRNG rng(now())` (`search.cpp:2106`) — only
    active when skill/`UCI_LimitStrength` enabled; keep disabled in research runs.
  - `SearchManager::check_time` uses `static TimePoint lastInfoTime`
    (`search.cpp:2220`) — output-only, no node-count effect.
  - `value_draw(nodes)` (`search.cpp:133`) is deterministic in `nodes`.
- There is an existing determinism harness: `tests/reprosearch.sh`.

---

# 2. MovePicker

## 2.1 Files

- `src/movepick.h`, `src/movepick.cpp`.

## 2.2 Stages (`movepick.cpp:23-39`, enum `Stages`)

Main search (not in check): `MAIN_TT, CAPTURE_INIT, GOOD_CAPTURE, QUIET_INIT,
GOOD_QUIET, BAD_CAPTURE, BAD_QUIET`.

In check (evasion): `EVASION_TT, EVASION_INIT, EVASION`.

ProbCut: `PROBCUT_TT, PROBCUT_INIT, PROBCUT`.

Qsearch: `QSEARCH_TT, QCAPTURE_INIT, QCAPTURE`.

`next_move()` (`movepick.cpp:260-335`) drives the state machine. Key order facts for
main-search (non-check) nodes:

1. TT move first (`MAIN_TT`), if present and pseudo-legal; skipped by all later
   `select()` filters via `*cur != ttMove`.
2. `CAPTURE_INIT`: generate `MoveList<CAPTURES>`, `score<CAPTURES>`, full sort
   (`partial_insertion_sort(cur, endCur, INT_MIN)`).
3. `GOOD_CAPTURE`: emit captures passing
   `pos.see_ge(*cur, -cur->value / 18)` (MVV/history-adjusted SEE); failing
   captures are moved to `endBadCaptures` region.
4. `QUIET_INIT`: unless `skipQuiets`, generate `MoveList<QUIETS>`,
   `score<QUIETS>`, partial sort with limit `-3560 * depth`.
5. `GOOD_QUIET`: emit quiets with `value > goodQuietThreshold` (`-14000`).
6. `BAD_CAPTURE`, then `BAD_QUIET` (`value <= -14000`).

So the effective **quiet stage boundary is `goodQuietThreshold = -14000`**
(`movepick.cpp:263`). Quiets scoring already produces a continuous `m.value`
(see below) — an existing insertion point for a learned ordering residual.

## 2.3 Scoring (`MovePicker::score`, `movepick.cpp:148-227`)

- `CAPTURES`: `value = captureHistory[pc][to][type_of(captured)] + 7 * PieceValue[captured]`
  (`movepick.cpp:182`).
- `QUIETS` (`movepick.cpp:185-211`):
  - `2 * mainHistory[us][move.raw()]`
  - `+ 2 * pawnHistory_entry(pos)[pc][to]`
  - `+ continuationHistory[0..3][pc][to]` and `continuationHistory[5][pc][to]`
    (note: indices 0,1,2,3,5; index 4 is skipped)
  - `+ 16384` if the move gives check (`check_squares(pt) & to`) and
    `pos.see_ge(move, -75)`
  - `+ PieceValue[pt] * 20 * (threatened-by-lesser at from − at to)` using
    precomputed `threatByLesser[]` per piece type
  - `+ 8 * lowPlyHistory[ply][move.raw()] / (1 + ply)` if `ply < LOW_PLY_HISTORY_SIZE (5)`
- `EVASIONS`: capture moves get `PieceValue[captured] + (1 << 28)`; others
  `mainHistory + continuationHistory[0]`.

All values are ints on an engine-defined scale (~16-bit magnitude); quiet scores
below `-14000` are "bad".

## 2.4 Move ordering mechanics

- `partial_insertion_sort(begin, end, limit)` (`movepick.cpp:72-105`) is an
  insertion sort that only guarantees that moves with `value >= limit` are moved to
  the front in descending order; order of the tail is unspecified.
- AVX512 path uses a `MoveSorter` over 16 elements (`movepick.cpp:42-88`).
- Moves are generated lazily: `MoveList<CAPTURES/QUIETS>` at stage entry, stored in
  the MovePicker's stack array `ExtMove moves[MAX_MOVES]`.
- `select(filter)` (`movepick.cpp:234-246`) scans `[cur, endCur)` and returns the
  first element matching the predicate, always skipping `ttMove`.

## 2.5 Constructors

- Main/qsearch constructor (`movepick.cpp:113-133`): picks initial stage from
  `pos.checkers()` and `depth > 0` (`QSEARCH_TT` when depth <= 0); stores const
  pointers/refs to all history tables, plus `depth`, `ply`, `ttMove`.
- ProbCut constructor (`movepick.cpp:137-145`): capture history only, SEE threshold.
- MovePicker is **non-copyable** (`movepick.h:43-44`) and has **no dynamic
  allocation**; it holds only const references/pointers. Draining a second,
  freshly-constructed MovePicker for the same position and the same history-table
  state produces the same ordering — verified: constructors and `score()` only
  read. (Risk to confirm in Phase 2: `partial_insertion_sort` and `select` only
  mutate the local `ExtMove` buffer and `cur/endCur` pointers.)

## 2.6 Interaction points used by search

- `mp.skip_quiet_moves()` sets `skipQuiets` (`movepick.cpp:337-338`), called from
  `search()` when `moveCount >= (3 + depth*depth)/(2 - improving)`
  (`search.cpp:1175-1177`).
- Note: `search()` also consumes `ttData.move` (TT move) and the ProbCut
  MovePicker is a separate instance created inside Step 12 with the probcut
  constructor (`search.cpp:1071-1073`).

---

# 3. Worker state and per-thread structures

`Search::Worker` (`src/search.h:306-430`):

Public history/stats members:

- `ButterflyHistory mainHistory` (per worker).
- `LowPlyHistory lowPlyHistory` (per worker).
- `CapturePieceToHistory captureHistory` (per worker).
- `CorrectionHistory<Continuation> continuationCorrectionHistory` (per worker;
  statically sized `PIECE_NB x SQUARE_NB` of `PieceToHistory`-typed correction
  bundles — see `history.h`).
- `TTMoveHistory ttMoveHistory` (per worker, single `StatsEntry<i16,8192>`).
- `SharedHistories& sharedHistory` (reference; shared across a NUMA node).
- `ContinuationHistory (&continuationHistory)[2][2]` — reference to the shared
  `[inCheck][Captures/NoCaptures]` block (in `SharedHistories`).

Private:

- `LimitsType limits` (copy of UCI limits).
- `pvIdx, pvLast`; `nodes, tbHits, bestMoveChanges` (`RelaxedAtomic<u64>`);
  `selDepth, nmpMinPly` (ints).
- `Value optimism[COLOR_NB]`.
- `Position rootPos; StateInfo rootState; RootMoves rootMoves; Depth rootDepth;
  Value rootDelta;`
- `PVMoves lastIterationIdxPV`.
- `threadIdx, numaThreadIdx, numaTotal; numaAccessToken`.
- `std::array<int, MAX_MOVES> reductions;` — lookup table initialized in `clear()`
  (`search.cpp:721` region) as `int(2872/128.0 * log(i))`.
- `Tablebases::Config tbConfig`.
- Option/thread/TT/network references:
  `const OptionsMap& options; ThreadPool& threads; TranspositionTable& tt;
  const LazyNumaReplicatedSystemWide<Eval::NNUE::Network>& network;`
- NNUE: `Eval::NNUE::AccumulatorStack accumulatorStack;`
  `Eval::NNUE::AccumulatorCaches refreshTable;`

`Worker::clear()` (`search.cpp:690-718`) is called before each game; fills
histories, `reductions`, `ttMoveHistory`, refreshes `refreshTable`.

Main-thread-only manager state (`SearchManager`, `search.h:257-305`): `tm`,
`iterValue[4]`, `previousTimeReduction`, `bestPreviousScore`,
`bestPreviousAverageScore`, `callsCnt`, `stopOnPonderhit`, `ponder`, and the update
callbacks.

`Stack` (`search.h:77-99`): per-ply info: `pv`, `continuationHistory`,
`continuationCorrectionHistory`, `ply`, `currentMove`, `excludedMove`,
`staticEval`, `statScore`, `moveCount`, `inCheck`, `ttPv`, `ttHit`, `followPV`,
`cutoffCnt`, `reduction`. Allocated once per search as
`Stack stack[MAX_PLY + 10]` with `ss = stack + 7` (`search.cpp:292-297`);
`ss->ply` assigned for all frames; frames below `ss-7` and above `ss+2` are used.

Root move data (`RootMove`, `search.h:110-158`): `effort` (nodes), `score`,
`previousScore`, `averageScore`, `meanSquaredScore`, `uciScore`, inexact flags,
`selDepth`, `tbRank`, `tbScore`, `pv`, `previousPV`.

---

# 4. Histories (mutation targets)

All in `src/history.h`:

| Table | Type | Indexing | Sharing |
|---|---|---|---|
| `mainHistory` | `Stats<i16,7183,COLOR_NB,UINT_16_HISTORY_SIZE>` | `[us][move.raw()]` | per worker |
| `lowPlyHistory` | `Stats<i16,7183,LOW_PLY_HISTORY_SIZE(5),UINT_16>` | `[ply][move.raw()]` | per worker |
| `captureHistory` | `Stats<i16,10692,PIECE_NB,SQUARE_NB,PIECE_TYPE_NB>` | `[pc][to][captured]` | per worker |
| `PieceToHistory` | `AtomicStats<i16,30000,PIECE_NB,SQUARE_NB>` | `[pc][to]` | per worker (continuation block is shared) |
| `continuationHistory` | `MultiArray<PieceToHistory,PIECE_NB,SQUARE_NB>` `[2][2]` block | `[inCheck][capture][pc][to]` via `Stack` | NUMA-shared (`SharedHistories::continuationHistoryBlock`), atomic entries |
| `continuationCorrectionHistory` | `CorrectionHistory<Continuation>` = `MultiArray<PieceToHistory-like,PIECE_NB,SQUARE_NB>` (of `CorrectionBundle`) | `[pc][to]` per `Stack` frame | per worker |
| `pawnHistory` | `DynStats<AtomicStats<i16,8192,PIECE_NB,SQUARE_NB>,8192>` | `[pawn_key & mask][pc][to]` | NUMA-shared |
| `correctionHistory` (`UnifiedCorrectionHistory`) | `DynStats<MultiArray<CorrectionBundle<i16,1024>,COLOR_NB>,65536>` | `[pawn/minor/nonpawn key & mask][color].{pawn,minor,nonPawnW,nonPawnB}` | NUMA-shared |
| `ttMoveHistory` | `StatsEntry<i16,8192>` | scalar per worker | per worker |

`StatsEntry::operator<<` (`history.h:73-82`) performs the standard decayed update:
`val + bonus - val*|bonus|/D`.

Search sites that write these:
- `update_all_stats` (`search.cpp:1956-2022`).
- `update_quiet_histories` (`search.cpp:2044-2062`).
- `update_continuation_histories` (`search.cpp:2025-2042`).
- `update_correction_history` (`search.cpp:118-131`).
- TT-cutoff quiet-history bonus path (`search.cpp:885-891`).
- Eval-diff history adjustment before razoring (`search.cpp:971-978`).
- Fail-low countermove paths in Step 23 (`search.cpp:1568-1620`).
- Multi-cut and correction updates in singular-extension paths
  (`search.cpp:1290-1300`).

`Search::Worker::do_move` sets `ss->continuationHistory` and
`ss->continuationCorrectionHistory` pointers from the freshly computed
`dirtyPiece` (`search.cpp:660-672`).

---

# 5. Transposition table

## 5.1 API and ownership (`src/tt.h`, `src/tt.cpp`)

- Single global `TranspositionTable` for all threads, owned via
  `Search::SharedState` (`search.h:196-226`); each `Worker` holds a reference.
- `resize(mbSize, threads)` allocates `clusterCount = mbSize * 1MiB / 32` clusters
  of 32 bytes (`tt.cpp:146-163`). Option `Hash` handled in `engine.cpp:90`.
- `TTEntry` is 10 bytes: `key16, depth8, genBound8 (pv|bound|generation), move16,
  value16, eval16` (`tt.cpp:30-58`). Cluster = 3 entries + 2 pad = 32 bytes.
- `probe(key)` (`tt.cpp:239-263`) returns `tuple<bool, TTData, TTWriter>`:
  - scans 3 entries for `key16 == u16(key)`;
  - on miss returns writer to the least-valuable entry chosen by
    `depth - 8*relative_age` replacement;
  - **the `TTWriter` wraps a pointer into the live global table** — writes mutate
    the shared table immediately (`TTWriter::write` → `TTEntry::save`,
    `tt.cpp:65-115`). `probe` is `const` but returns a non-const writer into the
    table (`tt.cpp:243` obtains `TTEntry* const tte = first_entry(key)`).
- `first_entry(key)` = `&table[mul_hi64(key, clusterCount)].entry[0]`
  (`tt.cpp:265-267`).
- Aging: 5-bit generation; `new_search()` increments (`tt.cpp:213-217`);
  `generation()` for writes; `relative_age` via unsigned wrap
  (`tt.cpp:117-125`); secondary aging of decisive scores on replacement
  (`tt.cpp:96-108`).
- Prefetch: `prefetch(tt.first_entry(pos.prefetch_key(move)))` in
  `Worker::do_move` (`search.cpp:642`). `prefetch_key` approximates castling/EP/
  promotion.

## 5.2 Implications for research (verified)

- There is **no virtual/indirection layer** on the TT: `search` and `qsearch`
  capture `auto [ttHit, ttData, ttWriter] = tt.probe(posKey);` locally and use
  `ttWriter` throughout the node (including nested SE search at the same node).
- Every probe/write goes to the same global table through `Worker::tt`.
- A copy-on-write research overlay therefore requires either (a) templating search
  and qsearch on a TT accessor type, (b) a compile-time research search path with a
  shadow TT, or (c) an accessor interface. The plan's §10.4 requirement
  ("no virtual TT call in production hot paths") points to (a) or (b).
- The `TTWriter` pattern (pointer to one specific entry, possibly a replace
  candidate) must be preserved by any overlay so writes land in the same
  cluster-entry the probe selected.

---

# 6. Position and move making

`Position` (`src/position.h`): piece-centric board (`board[64]`, `byTypeBB`,
`byColorBB`, counts), castling arrays, `StateInfo* st`, `gamePly`, `sideToMove`,
`chess960`, `Dirties scratchDirties`.

`StateInfo` (`position.h:39-64`): copied-on-move fields (keys, material, castling,
rule50, epSquare) and recomputed fields (`key`, `checkersBB`, blockers/pinners,
checkSquares, `capturedPiece`, `repetition`, `previous`).

Key methods:

- `do_move(Move, StateInfo&, bool givesCheck, Dirties&, const TT*, const
  SharedHistories*)` (`position.cpp:817`) — updates board, keys,
  `dirties.dirtyPawnPairs/dirtyThreats/dirtyPiece` for NNUE, and threat/pawn-pair
  feature bookkeeping. Public wrapper
  `do_move(Move, StateInfo&, const TT* = nullptr)` (`position.h:427-433`) fills
  scratch dirties (used outside search; search uses the full overload).
- `undo_move(Move)` (`position.cpp:1086`), `do_null_move` (`position.cpp:1344`),
  `undo_null_move`.
- Legality/check/capture helpers used per candidate:
  `pseudo_legal`, `legal`, `capture`, `capture_stage`, `gives_check`, `see_ge`,
  `checkers`, `check_squares`, `attackers_to`, `attacks_by`, `captured_piece`.
- Keys: `key()` (with rule50 adjustment), `pawn_key`, `material_key`,
  `minor_piece_key`, `non_pawn_key`, `prefetch_key(move)`.

Search wraps these in `Worker::do_move/do_null_move/undo_move/undo_null_move`
(`search.cpp:635-688`): TT prefetch, `++nodes`, `accumulatorStack.push()` before
`pos.do_move(...)`, continuation pointer setup after; `accumulatorStack.pop()` on
undo. `null move` does not push/pop accumulator state (no piece changes) — see
`nnue_accumulator` handling.

---

# 7. NNUE evaluation

## 7.1 Top level

- Network loaded via `EvalFile` option (`engine.cpp:136`); embedded default
  `"nn-1a298aa575a0.nnue"` (`evaluate.h:37`). Network objects are lazily
  replicated per NUMA node (`LazyNumaReplicatedSystemWide<Eval::NNUE::Network>`),
  accessed through `network[numaAccessToken]` (`search.cpp:1902-1905`).
- `Eval::evaluate(network, pos, accumulatorStack, refreshTable, optimism)` →
  `Network::evaluate` (`network.cpp`): computes material `bucket =
  (pieceCount-1)/4` and selects one of `LayerStacks = 8` network stacks.

## 7.2 Accumulator (the incremental part)

`src/nnue/nnue_accumulator.h`:

- `struct alignas(64) Accumulator` holds, per color:
  - `accumulation: array<i16, L1=1024>`
  - `psqtAccumulation: array<i32, PSQTBuckets=8>`
  - `computed: bool[COLOR_NB]`
- `AccumulatorState: public Accumulator, Dirties` — one per ply.
- `AccumulatorStack`: `std::array<AccumulatorState, MAX_PLY+1>` + `size`;
  `push()` (`nnue_accumulator.cpp:79`) returns the new top's `Dirties&` (filled by
  `pos.do_move`); `pop()` (`:89`); `evaluate(pos, ft, cache)` (`:94`) updates the
  two perspectives incrementally from the most recent usable accumulator
  (`forward_update_incremental[_both]`) or by refresh
  (`update_accumulator_refresh_cache`, Finny tables keyed by king square and
  piece configuration), with a hybrid path for same-file king moves at
  `pieceCount >= 15` (`evaluate_side`, `:110-140`).
- Feature sets (all three feed one transformer whose per-side output is the
  accumulator):
  - `PSQFeatureSet = HalfKAv2_hm` (`features/half_ka_v2_hm.h`): king-relative
    piece-square features, 32-bit-oriented; feature index =
    `(square ^ OrientTBL[ksq] ^ flip) + PieceSquareIndex[pc] + KingBuckets[ksq]`
    (`half_ka_v2_hm.cpp:81-86`). King buckets: 32 buckets (`KingBuckets` table),
    so **any king move changes the king bucket of the moving side and forces a
    refresh of that side** (`requires_refresh`: `diff.pc == king of perspective`,
    `half_ka_v2_hm.cpp:102-105`); king-square also gates every other feature's
    index, so the moved king's own feature and orientation change.
  - `ThreatFeatureSet = FullThreats` (`features/full_threats.h`): threat
    features, updated from `DirtyThreats` produced during `do_move`.
  - `PairFeatureSet = PP_3Wide` (`features/pp_3wide.h`): pawn-pair features from
    `DirtyPawnPairs`.
- Feature index updates are consumed in `nnue_accumulator.cpp`
  `update_accumulator_incremental<...>` via each feature set's
  `append_changed_indices(perspective, ksq, diff, removed, added)`; each changed
  index adds/subtracts one weight column of the FeatureTransformer.

## 7.3 FeatureTransformer

`src/nnue/nnue_feature_transformer.h`:

- `HalfDimensions = L1 = 1024`; `InputDimensions` is the concatenation of the three
  feature sets; per-side weight arrays `weights` (HalfKA) and
  `threatAndPpWeights` (threats + pawn pairs), plus per-feature PSQT weights.
- `transform(...)` (`nnue_feature_transformer.h:243-260`) runs
  `accumulatorStack.evaluate`, reads `psqtAccumulation` for the given bucket, and
  converts each side's 1024-dim i16 `accumulation` into a 512-dim (per side)
  clipped pairwise product feature vector `output` (`transform_perspective`, `:270-
  400`) — the accumulator is split into two 512-halves whose pairwise product (with
  clipping, `FtMaxVal=255`) forms the net input. `NNZInfo` records nonzero output
  chunks for the sparse first layer.
- Result: per-bucket `NetworkArchitecture network[bucket]` computes
  `psqt + positional` (`network.h` `NetworkArchitecture::propagate`, architecture
  L1=1024 sparse → L2=32 → L3=32, plus skip connections; `nnue_architecture.h`).

## 7.4 Refresh semantics relevant to policy research

- A per-worker `AccumulatorCaches refreshTable` provides king-square-keyed refresh
  entries ("Finny tables").
- King moves: full refresh of the moving side unless the hybrid (same-file, dense
  position) shortcut applies.
- Null move: no piece movement; accumulator stack not pushed; evaluation at the
  null-move child reuses the same accumulator contents with flipped
  side-to-move perspective selection.
- **Practical conclusion for the planned policy accumulator**: the existing value
  accumulator is 1024-dim i16 per side (pairwise-product encoding) and is king-
  bucket dependent. The research design (§plan 15.1) already calls for a *separate,
  smaller, king-bucket-free* policy accumulator; the existing Dirties plumbing
  (`Position::do_move` → `Dirties&` → per-feature `append_changed_indices`) is the
  template to reuse for incremental updates. The Jacobian experiment (plan §13)
  must operate on the real value net semantics above and mark king-move/castling
  deltas as refresh-only.

---

# 8. Threading, options, and build/test conventions

## 8.1 Thread model

- `ThreadPool` (`thread.h:118-189`) owns `Thread` objects; `Thread` owns a
  `LargePagePtr<Search::Worker>`. `Worker` created with
  `SharedState` = `(OptionsMap, ThreadPool, TranspositionTable,
  SharedHistories map per NUMA node, Network replication)`.
- Main thread runs `SearchManager`; other threads run `NullSearchManager`.
- `threads.stop`, `threads.increaseDepth` are shared atomics.
- For research: single-thread, fixed-depth runs are the deterministic baseline;
  `Threads` option default 1.

## 8.2 Options relevant to research (all in `engine.cpp`)

`Threads` (`:84`), `Hash` (`:90`), `MultiPV` (`:105`), `Skill Level` (`:107`),
`UCI_LimitStrength` (`:115`), `SyzygyPath` (`:124`), `EvalFile` (`:136`).
Research options should be added to the same map and gated by the
`POLICY_RESEARCH` compile flag.

## 8.3 Build and test

- Build: `make -C src -j target ARCH=...` from `src/Makefile`; targets include
  `build`, `profile-build`, `net`, `help`. Default net fetch is `make net` in
  `src/`.
- Tests live in `tests/`: `reprosearch.sh` (determinism harness over repeated
  `go nodes N` runs), `perft.sh`, `signature.sh`, `testing.py` (instrumented runs).
- A research compile flag can be threaded through `src/Makefile` (e.g. `EXTRACXXFLAGS`
  style variables, or a dedicated target); confirm the mechanism when implementing.

---

# 9. Proposed integration-point diagram (Phase 0 draft)

Policy research hook candidates, in order of increasing invasiveness. Each marked
[research-gated] must compile to nothing without `POLICY_RESEARCH`.

```text
iterative_deepening()                search.cpp:270
  └─ per-pvIdx aspiration loop
      └─ search<Root>(...)           search.cpp:395     [root-order override hook: plan §9]
          └─ search<PV/NonPV>(...)
              └─ qsearch when depth<=0  search.cpp:731
              Step 1..13 pre-move pruning
              moves_loop:            search.cpp:1098
              Step 14 move loop      search.cpp:1145
                ├─ MovePicker mp(...)          search.cpp:1133  [per-node init hook]
                ├─ per move:
                │    baseline score assigned  (MovePicker::score, movepick.cpp:148)
                │    └─ [policy scoring hook, quiets stage only first: plan §16.1]
                │    pruning (Step 15)         search.cpp:1171
                │    LMR decision (Step 18)    search.cpp:1313 [log-only until §18]
                │    do_move / undo_move       search.cpp:1307/1425
                │        └─ accumulatorStack.push/pop + nodes++  [policy-accumulator
                │           update would live beside accumulatorStack here]
                │    new-best-move / cutoff    search.cpp:1430
                └─ node completion: TT write (Step 24) search.cpp:1619
                                      history updates (Step 23) search.cpp:1554
  └─ time management / stop checks    search.cpp:508-600

TT probe sites (search.cpp:814, 1703(qsearch); nested SE probe 1254):
   every site captures {ttHit, ttData, ttWriter} = tt.probe(posKey)
   [overlay must intercept here under research template/compile-path]

Worker state entry points for counterfactual cloning (plan §10.3):
   Worker members (search.h:306-430) + SharedHistories + Stack contents.
```

---

# 10. Unresolved architecture questions (to resolve before Phase 1)

1. TT overlay strategy: template `search`/`qsearch` on a TT accessor vs. research
   search copy. The hot path currently captures a `TTWriter` into the live table;
   the chosen approach must keep the production path untouched (§5.2).
2. ProbCut, singular-extension (`excludedMove`), and null-move searches write TT and
   mutate worker state too; counterfactual probing must decide whether to include
   them in shadow subtrees (plan §10.5 restricts the first dataset to one stage and
   to non-PV/null-window nodes — confirm exclusion covers ProbCut entries).
3. Continuation-history index details: `MovePicker` uses `contHist[0..3,5]`
   (`movepick.cpp:203-204`) — the exact (ss-k) mapping and its `[pc][to]` indexing
   must be recorded as feature semantics (a model input), and the skipping of index
   4 confirmed intentional.
4. The exact quantitative semantics of `accumulation` (1024 i16 per side =
   two 512-halves combined pairwise in `transform_perspective`) must be confirmed
   against a trainer document before any Jacobian implementation; also confirm
   `LayerStacks=8` per-material-bucket network stacks and the `bucket=(n-1)/4`
   rule (already read: `network.cpp`).
5. Determinism: confirm no remaining nondeterminism beyond `Skill`/debug-print
   statics when running single-thread fixed-depth (verify with
   `tests/reprosearch.sh` at the intended research settings).
6. Root `effort` accumulation (`search.cpp:1462`) already provides per-root-move
   node accounting — check suitability as an observational cost label before adding
   new accounting.
7. Confirm whether constructing an extra `MovePicker` for candidate enumeration at a
   sampled node has any observable effect (all refs are const; MovePicker is
   non-copyable and stack-allocated) — must be proven by audit test in Phase 2.
8. `Value`/`Score` types, move encoding (`Move` = u16 with type/promotion bits,
   `types.h:443`), and score ranges must be documented as part of the data schema
   (plan §7) before logging; raw engine values are negamax, side-to-move relative.
