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

# 10. Resolved architecture decisions

These decisions close the eight questions raised by the initial Phase 0 inventory.
Implementation remains subject to the phase-specific tests and exit gates in
`plan.md`.

## 10.1 TT overlay strategy

**Decision:** use one search implementation parameterized by a compile-time TT
accessor concept; do not copy `search()`/`qsearch()` and do not add virtual dispatch
to the production hot path.

The accessor concept must provide:

```text
probe(Key)       -> {hit, TTData, writer}
generation()     -> u8
first_entry(Key) -> prefetchable address
```

The returned writer must provide the existing `write(...)` and `penalize(int)`
operations. Two implementations are required:

1. `LiveTTAccess`: a zero-overhead forwarding adapter over the current
   `TranspositionTable`.
2. `ResearchTTOverlay`: an immutable base TT plus private copy-on-write clusters.
   On first write to a cluster, copy the whole 32-byte base cluster and apply the
   existing replacement/write algorithm to the private copy. All later reads of
   that cluster use the private copy. Discard the overlay after the probe.

`search`, `qsearch`, and recursive calls must receive the accessor statically. A
research build may tolerate accessor-passing overhead; the normal build must be
verified to inline the live adapter. If compiler inspection or benchmark shows a
production regression, put the accessor-parameter form behind `POLICY_RESEARCH`
while retaining one source body through conditional declarations/helpers. A
separate copied search implementation is rejected because it will diverge from the
engine being measured.

`Position::do_move(..., const TranspositionTable*)` uses the TT pointer only to
prefetch (`position.cpp:1007-1008`); shadow search may prefetch the immutable base or
pass `nullptr`. This has no search-semantic effect. The current `TTEntry` and
`Cluster` definitions/replacement logic are private to `tt.cpp`; research overlay
implementation will require a research-gated internal header/friend API rather than
reimplementing the layout independently.

**Verification:** normal build bench signature unchanged; no measurable disabled-
feature NPS regression; overlay unit tests from plan §20.3; base-table checksum
unchanged after every shadow probe.

## 10.2 ProbCut, singular extension, and null move inside probes

**Decision:** descendants of a counterfactual candidate use the exact current
search policy, including normal null-move pruning, ProbCut, singular extensions,
LMR, re-searches, history updates, and TT writes — but all writes go to the probe's
isolated worker/TT state. The `ScopedShadowProbe` guard disables only recursive
*instrumentation/intervention*, not normal search behavior.

The first internal dataset is deliberately narrower:

- non-root, `NonPV`, null-window decisions;
- normal main-search move loop, not qsearch;
- not in check;
- `excludedMove == Move::none()`;
- no tablebase probe;
- one explicitly named MovePicker stage (initially quiets);
- TT move already absent or already attempted, so it is not displaced;
- fixed current `alpha`, `beta`, effective `depth`, `moveCount`, and stage.

Node-level null move and ProbCut occur before `moves_loop`; a sampled move-loop
decision therefore inherits their real effects in its common pre-decision state.
Descendant nodes execute them normally. A forced candidate receives the exact
search treatment of the next slot, including the baseline reduction/window. The
initial internal counterfactual target is the candidate probe outcome/cost. Full
"remaining node cost if chosen next" is deferred until the move-loop continuation
can be isolated without duplicating search logic. Root-level force-first experiments
remain the first source of complete total-search-cost counterfactuals.

If future experiments include the TT move, singular-extension behavior must remain
active exactly as in the normal search. It must never be approximated by an
ordinary child probe.

## 10.3 Continuation-history mapping

At main-search node stack pointer `ss`, `search.cpp` builds:

```text
contHist[0] = (ss - 1)->continuationHistory   previous move at distance 1
contHist[1] = (ss - 2)->continuationHistory   previous move at distance 2
contHist[2] = (ss - 3)->continuationHistory   previous move at distance 3
contHist[3] = (ss - 4)->continuationHistory   previous move at distance 4
contHist[4] = (ss - 5)->continuationHistory   previous move at distance 5
contHist[5] = (ss - 6)->continuationHistory   previous move at distance 6
```

When parent move `p` is made, `Worker::do_move` sets the parent's stack entry to:

```text
&continuationHistory[parentWasInCheck][parentWasCapture]
                    [pieceMovedByParent][parentDestination]
```

At the child, scoring candidate `m` reads that selected `PieceToHistory` table at:

```text
[pieceMovedByCandidate][candidateDestination]
```

Thus each value is a move-pair feature: previous move context × current
`[piece][to]`.

Quiet MovePicker scoring intentionally reads distances **1, 2, 3, 4, and 6**:
`contHist[0], [1], [2], [3], [5]`; distance 5 (`contHist[4]`) is explicitly
omitted in this source revision. The omission is treated as intentional/tuned
behavior because the accesses are explicit, while `update_continuation_histories`
separately updates all distances 1 through 6 with weights
`{520,390,145,251,66,209}`. No unsupported rationale is inferred.

Other uses differ and must be logged separately: quiet pruning and LMR stat score
use distances 1 and 2; evasion scoring uses distance 1; capture MovePicker scoring
uses capture history rather than continuation history. Dataset fields will store the
five quiet-ordering values separately, plus a distance-5 value only if needed for
an ablation; they must not be represented as one undocumented sum.

## 10.4 Exact value-NNUE accumulator semantics

Runtime inference code is authoritative for the Jacobian experiment; an external
trainer description is secondary and must be version/checksum matched before using
training-space gradients.

Verified runtime structure:

- `AccumulatorState` contains, per color, `i16 accumulation[1024]`,
  `i32 psqtAccumulation[8]`, and `computed` flags, plus `Dirties`.
- The accumulator begins with FeatureTransformer biases and adds/subtracts full
  weight columns for active `HalfKAv2_hm`, `FullThreats`, and `PP_3Wide` features.
  HalfKA columns are i16; threat/pawn-pair columns are i8 widened into i16;
  corresponding PSQT columns update the 8 i32 buckets
  (`nnue_accumulator.cpp`, `apply_combined`).
- For each perspective, `transform_perspective` splits the 1024 accumulator values
  into two 512-value halves, clips each to `[0,255]`, multiplies corresponding
  pairs, divides by 512, and emits 512 u8-like transformed features. Concatenating
  side-to-move and opponent perspectives yields the 1024 inputs consumed by
  `fc_0`; `NNZInfo<1024>` records nonzero chunks.
- `LayerStacks = 8`; `Network::evaluate` selects
  `bucket = (pos.count<ALL_PIECES>() - 1) / 4`, then uses
  `network[bucket].propagate(...)`.
- Network topology is sparse affine `1024 -> 32`, squared/clipped and clipped
  activations, affine `64 -> 32`, squared/clipped and clipped activations, then
  affine `(64 + 64) -> 1`, with the documented fc0 skip term.
- A king move requires refresh for that king's perspective; castling is refresh-
  only there. Captures can change the material bucket. Child side-to-move swaps the
  perspective concatenation order.

Therefore a naive parent-gradient dot move-delta is not generally valid for:
king moves/castling, material-bucket-crossing captures, or a child evaluated with
swapped perspective order. The first Jacobian experiment must restrict its clean
baseline to quiet non-king moves, hold a canonical evaluation perspective, and
remain in the same material bucket. Later variants must handle bucket and
perspective transitions explicitly.

Because production inference is quantized and clipped, "Jacobian" must be named
precisely: either a derivative of the matched dequantized training graph, a chosen
straight-through surrogate, or an exact finite-difference sensitivity. Any hand-
written adjoint must be checked against the actual forward implementation using
finite differences away from activation boundaries and exact child-value deltas at
boundaries.

## 10.5 Deterministic research protocol

**Decision:** Phase 1's canonical scientific mode is one thread, fixed depth, fixed
hash, MultiPV 1, full strength (`Skill Level=20`, `UCI_LimitStrength=false`), fixed
value-net checksum, tablebases disabled, no ponder/time stop, and a fresh process per
root for strict experiments. `ucinewgame` + `isready` may be used for the faster
batched mode only after matching the fresh-process result.

Empirical checks completed on the audited source with an x86-64-avx2 build:

1. Three fresh-process repetitions over startpos, Kiwipete, and a middlegame FEN at
   depth 11 produced identical best move, score, node count, and PV in every run.
2. Two complete cycles over the same three positions in one process, with
   `ucinewgame` + `isready` before each position, were identical. The recorded
   depth-11 tuples were:
   - startpos: `e2e4`, `cp 26`, `12455` nodes;
   - Kiwipete: `e2a6`, `cp -152`, `5400` nodes;
   - middlegame: `a3a4`, `cp -111`, `15149` nodes.

The repository's `tests/reprosearch.sh` was inspected but not run because `expect`
was not installed in the environment. Phase 1 must add a dependency-free equivalent
or install/run `expect` in CI. This evidence establishes the proposed protocol, not
a universal determinism claim: SMP TT races, time-based stopping, skill RNG, binary/
compiler differences, network changes, and tablebase configuration remain outside
it.

## 10.6 `RootMove::effort` as a label

**Decision:** `RootMove::effort` is useful telemetry and a sanity check, but is not
a primary policy-training label.

At each root move attempt, `N = nodes - nodeCount` includes that move's child
searches and re-searches, then `rm.effort += N`. `effort` is initialized only when
new `RootMoves` are constructed for a `go`; it accumulates across iterative-
deepening depths, aspiration re-searches, and MultiPV activity. It does not retain
per-attempt depth/window/outcome boundaries. Consequently its final value mixes
multiple policies and search contexts.

For observational root data, log the local `N` at the existing update site together
with depth, window, outcome, re-search count, and iteration. For force-first root
experiments, use total run nodes/wall time as the causal label. Retain final
`RootMove::effort` only to validate that the sum of logged per-attempt contributions
matches engine bookkeeping.

## 10.7 Extra MovePicker and candidate enumeration

Source audit confirms MovePicker owns a local `ExtMove[MAX_MOVES]` buffer and local
cursor/stage state, while Position and history inputs are const references. Its
scoring, sorting, SEE, and attack queries do not update search state.

An empirical audit was also run in a temporary detached worktree: an independently
constructed MovePicker was fully drained before the real main-search MovePicker at
every main-search node. Baseline and audit builds used the same clean source, net,
compiler, and `ARCH=x86-64-avx2`. On `bench 16 1 10 default depth`, all normalized
info rows (depth, score, nodes, PV), best moves, and total nodes were identical;
both searched **453169 nodes**. The extra drain increased runtime, as expected, so
it is permitted only at sampled research nodes.

**Important limitation:** a fresh MovePicker at an arbitrary later decision point
starts again from TT/stage zero and includes already-attempted moves. It is not an
exact snapshot of the remaining candidates. Phase 2 observational logging should
capture stage-generated lists when the real picker creates/sorts them. If Phase 5
needs exact remaining-candidate enumeration, add a research-only MovePicker snapshot
that copies `moves[]`, stage/flags/scalars, and rebases every internal pointer
(`cur`, `endCur`, `endBadCaptures`, `endCaptures`, `endGenerated`) into the clone's
own array, then drains only the clone. Do not re-enable the normal deleted copy
constructor and do not use a raw `memcpy` because the internal pointers would still
refer to the original buffer.

## 10.8 Serialization types, move encoding, score ranges, and perspective

The version-1 data schema must use fixed-width fields independent of native C++ ABI:

- `move_raw: uint16` reproduces `Move::raw()`:
  - bits 0-5 destination square;
  - bits 6-11 origin square;
  - bits 12-13 promotion piece type minus KNIGHT;
  - bits 14-15 move type (`0 normal`, `1 promotion`, `2 en passant`, `3 castling`);
  - `Move::none() = 0`, `Move::null() = 65`.
- Internal castling encodes king-from to friendly-rook-from ("king captures friendly
  rook", `position.cpp:1320`), not necessarily the external UCI destination.
  Therefore also store canonical decoded `from:uint8`, `to:uint8`,
  `move_type:uint8`, `promotion_type:uint8`, `is_chess960:bool`, and an optional UCI
  string for diagnostics. Legal candidate records must never contain none/null.
- `Value` and `Depth` are native `int`; serialize as signed `int32`.
  `VALUE_DRAW=0`, `VALUE_MATE=32000`, `VALUE_INFINITE=32001`,
  `VALUE_NONE=32002`; `DEPTH_QS=0`, `DEPTH_UNSEARCHED=-2`, `DEPTH_NONE=-3`.
  Store an explicit validity/status flag rather than interpreting a sentinel as a
  searched score.
- `Bound` serializes as `uint8`: none=0, upper=1, lower=2, exact=3.
- Do not serialize C++ `Score` as the primary target. `Score` is a presentation
  variant (`Mate`, `Tablebase`, `InternalUnits`); ordinary UCI cp conversion is
  position dependent (`UCIEngine::to_cp`). Store raw `Value`, bound, alpha, beta,
  and position context; derive UCI presentation offline when needed.
- Search values, alpha, beta, static evaluation, and returned candidate value are
  all recorded in the **current parent node's side-to-move/negamax perspective**.
  A child return must be negated before logging as the candidate's parent-facing
  value. Store the pre-attempt alpha and beta. Classify at that same parent window:
  `fail_high = value >= beta`, `alpha_raise = alpha < value < beta`, and
  `fail_low = value <= alpha`; retain exact/TT bound separately.
- Store colors, pieces, and squares as explicit fixed-width numeric fields under a
  schema version, plus a position/root identifier. Never dump native `Move`,
  `Score`, or padded structs directly.
