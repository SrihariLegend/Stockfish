# Search Mutation Audit (Phase 0)

Every mutable object reachable from the recursive search path, with owner, type,
approximate size, write sites, and the isolation requirement for counterfactual
"shadow probe" search (plan §10.3).

Legend for the *counterfactual treatment* column:

- **clone** — must be copied into the research worker before a shadow subtree and
  discarded after.
- **restore** — mutated by the shadow subtree; must be saved/restored around a
  probe, or cloned.
- **disable** — must be turned off inside shadow subtrees.
- **shared** — deliberately shared/racy in production; the research worker needs its
  own instance to make probes deterministic and independent.
- **n/a** — not mutated by search or not reachable from a shadow probe.

All line anchors are from the Phase 0 inventory pass; re-verify by symbol before
implementing.

---

## A. Per-worker search state (`Search::Worker`, `src/search.h:306-430`)

| Object | Type / size | Write sites | Treatment |
|---|---|---|---|
| `mainHistory` | `Stats<i16,7183,2,65536>` = **262,144 B (256 KiB)** | `clear()` search.cpp:692; eval-diff shift search.cpp:975; TT-cutoff bonus search.cpp:888; `update_quiet_histories` search.cpp:2047; fail-low countermove search.cpp:1588 | **clone** (or **restore**) |
| `lowPlyHistory` | `Stats<i16,7183,5,65536>` = **655,360 B (640 KiB)** | `clear()` search.cpp:693; `update_quiet_histories` search.cpp:2050 | **clone/restore** |
| `captureHistory` | `Stats<i16,10692,16,64,8>` = **16,384 B (16 KiB)** | `clear()` search.cpp:693; `update_all_stats` capture paths search.cpp:2001,2019; prior-capture countermove search.cpp:1611 | **clone/restore** |
| `continuationCorrectionHistory` | `CorrectionHistory<Continuation>` = **2,097,152 B (2 MiB)**; worker-local nested `[previous pc][previous to][candidate pc][candidate to]` correction stats | `clear()` search.cpp:708; `update_correction_history` search.cpp:130-131 (via `(ss-2)/(ss-4)->continuationCorrectionHistory`); read by `correction_value` search.cpp:85-101 | **clone/restore** (accessed through `Stack` pointers, which must also be fixed up) |
| `ttMoveHistory` | `StatsEntry<i16,8192>` (scalar) | `clear()` search.cpp:714; `<< -421-110*depth` multi-cut search.cpp:1294; `<< ±` best-move-vs-ttMove search.cpp:1571 | **clone/restore** |
| `nodes`, `tbHits`, `bestMoveChanges` | `RelaxedAtomic<u64>` | `do_move` search.cpp:654 (`++nodes`); TB probe search.cpp:952 (`++tbHits`); root bestMoveChanges search.cpp:1526 | **isolate** (shadow nodes must not pollute real counters) |
| `selDepth`, `nmpMinPly` | `int` | `selDepth` search.cpp:781; `nmpMinPly` null-move verification search.cpp:1040-1046 | **restore** around probes |
| `optimism[COLOR_NB]` | `Value[2]` | set per root move in iterative_deepening search.cpp:352-353; read in `evaluate` search.cpp:1904 | **clone** (shadow subtree reads it; unchanged within a search) |
| `reductions[MAX_MOVES]` | `std::array<int, MAX_MOVES>` | `clear()` search.cpp:716-717 (read-only afterwards) | n/a (const during search) |
| `rootPos`, `rootState`, `rootMoves`, `rootDepth`, `rootDelta`, `pvIdx`, `pvLast`, `lastIterationIdxPV` | Position+StateInfo / RootMoves / Depth / Value / usize / PVMoves | root-level only (iterative_deepening) | **clone** if an internal probe must re-enter root-level code (not needed for subtree-only probes; guard against it) |
| `limits` | `LimitsType` | set per search start | n/a (read-only during search) |
| `accumulatorStack` | `AccumulatorStack` = **1,138,240 B (~1.086 MiB)** on the audited build: `(MAX_PLY+1 = 247)` × `AccumulatorState` (4,608 B) plus stack index/padding | `push` in do_move search.cpp:656; `pop` in undo_move search.cpp:685; `reset` start_searching search.cpp:193; refreshed on king moves / null-window eval | **clone/restore** (shadow subtree must evaluate; its pushes/pops must not desync the real stack) |
| `refreshTable` | `AccumulatorCaches` (64 king squares × 2 colors refresh entries of 1024 i16 + 8 i32 + board) | `clear()` search.cpp:719; used during accumulator refresh | **clone/restore** |
| `continuationHistory` (member ref to shared `[2][2]` block) and `sharedHistory` | `SharedHistories&` (NUMA-shared, atomic entries) | see section C | **shared → clone per research worker** for deterministic probes |
| `tbConfig` | `Tablebases::Config` | per search start | n/a (TB disabled initially) |
| `network`, `tt`, `options`, `threads` refs | — | `tt` writes throughout | TT: see section E |

`Stack` frames (`src/search.h:77-99`; allocated per search,
`Stack stack[MAX_PLY+10]`, `search.cpp:292`):

| Field | Written by | Notes |
|---|---|---|
| `currentMove`, `ply`, `inCheck`, `staticEval`, `statScore`, `moveCount`, `excludedMove`, `ttPv`, `ttHit`, `followPV`, `cutoffCnt`, `reduction` | search.cpp steps 1-24 | Shadow subtree writes its own higher frames; frames ≤ the probed node are read (`(ss-k)` lookups) and must be unchanged when the probe returns |
| `pv` | PV updates search.cpp:1417,1543 | Cloned per node on the real stack; shadow probes must not clobber |
| `continuationHistory` pointer | `do_move` search.cpp:664-666; `do_null_move` search.cpp:677 | Must point into the *cloned* shared block during probes |
| `continuationCorrectionHistory` pointer | `do_move` search.cpp:667-669 | Must point into the *cloned* per-worker table during probes |

**Critical subtlety:** search reads `(ss-k)` for k up to 6 (contHist,
`correction_value`, eval-diff comparisons, `is_shuffling`). A shadow probe that
searches a *child* of the current node will overwrite frames above the current
`ss`; that is the normal behavior. A probe that re-searches the *same node*
(excluded-move style, singular probes) reuses the same `Stack*` — the real
`excludedMove` machinery (search.cpp:1234-1305) already manages save/restore of
`ss->excludedMove`; any research node-level probe must replicate that discipline.

---

## B. Per-search locals (function-scope state)

| Object | Where | Notes |
|---|---|---|
| `StateInfo st` per node | search.cpp:768, qsearch.cpp:1672 | chained `st->previous`; shadow subtree allocates its own stack States |
| `PVMoves pv` per node | search.cpp:767 | written only on PV nodes; root `RootMove::pv` uses `RootPVMoves` |
| `MovePicker mp` per node | search.cpp:1133; qsearch search.cpp:1770; ProbCut search.cpp:1071 | local; holds const refs only |
| `SearchedList capturesSearched/quietsSearched` | search.cpp:761-762, capacity 32 (`SEARCHEDLIST_CAPACITY`, search.cpp:73) | node-local; written while iterating moves; feeds Step 23 stat updates |

---

## C. NUMA-shared histories (`SharedHistories`, `src/history.h:247-281`)

Shared across the threads of one NUMA node. Entries are atomic
(`AtomicStats`/`StatsEntry<T,D,true>`), deliberately racy.

| Object | Approx. size per NUMA node | Write sites | Treatment |
|---|---|---|---|
| `continuationHistoryBlock` = `ContinuationHistory table[2][2]` (`[inCheck][capture]`) of `PieceToHistory` (`AtomicStats<i16,30000,16,64>`) | `PieceToHistory` = 2,048 B; one `ContinuationHistory` = 2 MiB; full block = **8,388,608 B (8 MiB)** | `update_continuation_histories` search.cpp:2025-2042 (weighted, bounded +/−30000) | **shared → isolate** in research worker clone |
| `pawnHistory` `DynStats<AtomicStats<i16,8192,16,64>, 8192>` | **16 MiB per configured NUMA thread unit** (`threadCount × 8192 × 2048 B`) | `clear_range` search.cpp:699; `update_quiet_histories` search.cpp:2056-2058; eval-diff pawn path search.cpp:977; fail-low pawn path search.cpp:1590 | **shared → isolate** |
| `correctionHistory` `UnifiedCorrectionHistory` (DynStats of `MultiArray<CorrectionBundle<i16,1024>,2>`) | **1 MiB per configured NUMA thread unit** (`threadCount × 65536 × 16 B`) | `clear_range` search.cpp:698; `update_correction_history` search.cpp:118-131 | **shared → isolate** |

`PieceToHistory` is also the type of each per-worker continuation-correction table
entry and of the contHist targets; entries are `AtomicStats` with `D=30000`.

**Implication (plan §10.3):** a research worker needs its **own** copies of the
continuation block, pawn history, and correction history (or an isolation scheme
over the shared ones) so that two counterfactual probes from the same decision
point see identical history state, and probes do not perturb the real search's
online-learning tables.

---

## D. Position and board state

| Object | Notes |
|---|---|
| `Position` (mutable, copy-deleted) | A shadow subtree must run on its own `Position` clone or drive make/undo on the real one within the probe scope. `Position` is **non-copyable** (`position.h:47-48`); cloning must go through `Position::set(fen, ...)` + replay of a move list (per `ThreadPool::start_thinking`, thread.cpp:352) or an explicit research copy constructor — to be designed in Phase 5. |
| `StateInfo` chain (incl. `setupStates` root history) | `StateInfo` copies are plain structs (`memcpy`-friendly prefix, `position.cpp:826`); root history states live in `ThreadPool::setupStates` (thread.cpp:330) and `Worker::rootState`. |
| Zobrist keys / cuckoo tables (`static std::array<Key,8192> cuckoo`, `cuckooMove`, position.cpp:116-117) | read-only after `Position::init()`; not mutated by search. |
| `Dirties scratchDirties` | `Position` member used by the simple `do_move` wrapper (position.h:427); search uses the full overload with its own `Dirties` from the accumulator stack. |

---

## E. Transposition table (global, all threads)

| Item | Detail | Treatment |
|---|---|---|
| `TranspositionTable table` + `generation8` | single global; racy concurrent writes in production (`tt.h` header comment) | For causal probing use the plan §10.4 **copy-on-write overlay over a common pre-node base TT**; never run two candidate probes against a table they mutate in place. `tt.new_search()` (tt.cpp:213) only bumps generation — not a content reset. |
| `TTWriter` lifetime | `tt.cpp:239-263` returns writer to a live entry (or replace candidate); the node holds it until its Step 24 write | Overlay must return writers into overlay-owned cluster copies on first write (copy-on-write), and preserve `probe`'s replacement-pointer semantics. |
| `prefetch(tt.first_entry(...))` | do_move search.cpp:642 | Prefetch must target the base or overlay cluster consistently; harmless if it misses in research builds. |

---

## F. Thread-pool / manager / global state

| Object | Notes | Treatment |
|---|---|---|
| `ThreadPool::stop`, `increaseDepth` (`std::atomic_bool`) | checked at every node (search.cpp:790,1437); probes must not trip stop | **disable** (research builds must not honor stop during a bounded probe except the probe's own node budget); the probe budget censor must use a separate mechanism. |
| `SearchManager` (main thread only) | `tm`, `callsCnt`, `ponder`, `stopOnPonderhit`, `iterValue`, time bookkeeping; `check_time` called at every main-thread node (search.cpp:776) | **disable** inside probes (non-main-thread worker recommended). |
| `Skill::best`, `static PRNG rng(now())` | search.cpp:2106-2117 | keep disabled in research runs. |
| `SearchManager::check_time` `static TimePoint lastInfoTime` | search.cpp:2220 | output only. |
| `Position::init()` tables, attacks tables, `Zobrist` | read-only after init | n/a. |
| `Tablebases` (mapped files) | `Tablebases::init` engine.cpp:168; probing search.cpp:922-968 | **disable** for the first datasets (plan §6.1). |

---

## G. Write-site inventory (search.cpp) — quick index

Move-order/reduction relevant updates that a research observer must account for
when replaying a node:

1. `search.cpp:885-891` — TT-cutoff quiet-history bonus + previous-ply
   continuation penalty.
2. `search.cpp:971-978` — static-eval-diff shift of mainHistory/pawnHistory
   (before razoring), only when previous move is ok & not capture & not in check.
3. `search.cpp:1040-1046` — `nmpMinPly` save/restore around null-move verification.
4. `search.cpp:1290-1300` — multi-cut: ttMoveHistory penalty + correction-history
   bonus.
5. `search.cpp:1462-1526` — root move `effort`, average score, best-move-change
   bookkeeping.
6. `search.cpp:1554-1571` — Step 23 `update_all_stats` + `ttMoveHistory` update.
7. `search.cpp:1568-1620` — fail-low countermove bonus paths (continuation,
   mainHistory, pawnHistory, captureHistory).
8. `search.cpp:1636-1648` — end-of-node correction-history update.
9. `qsearch` writes TT only (`search.cpp:1874-1880`) and does not update move
   histories.
10. `Skill::pick_best` — only when skill enabled.

---

## H. Audit conclusions

1. **Node count, TB, and stop flags are the only cross-cutting state every probe
   touches.** A shadow subtree must run in a worker where `nodes` is a local
   counter and `threads.stop` cannot trigger.
2. **All move-ordering histories (per-worker and NUMA-shared) must be isolated per
   probe** or two probes from one decision point will not share a common pre-state,
   and probes will contaminate the real search's online learning (violating plan
   §2.3/§10.3).
3. **The accumulator stack must be cloned/restored around probes** because probes
   make and unmake moves on a `Position` whose `Dirties` feed NNUE incremental
   updates; `AccumulatorStack::size` and contents must return to the pre-probe
   state.
4. **TT requires a copy-on-write overlay** keyed on cluster identity; direct in-place
   mutation during probes is prohibited (§E).
5. **`Stack` frames above the probed node are scratch space** — reuse is safe and
   expected, but `(ss-k)` backward reads at probe time must see the exact pre-probe
   values, so probes may only write frames deeper than the current decision point.
6. **No hidden global mutable state was found on the hot path** other than those
   listed in §F; the codebase is otherwise worker/stack disciplined.

The static size audit was verified with a temporary `sizeof(...)` program against
this source/build configuration: `ButterflyHistory=262144`,
`LowPlyHistory=655360`, `CapturePieceToHistory=16384`,
`PieceToHistory=2048`, `ContinuationHistory=2097152`,
`ContinuationHistoryBlock=8388608`,
`CorrectionHistory<Continuation>=2097152`, `Accumulator=4224`,
`AccumulatorState=4608`, `AccumulatorStack=1138240`, `Stack=56`, and
`StateInfo=192` bytes. Dynamic history sizes scale with configured NUMA thread
count as described above.

Exit gate satisfied: every mutable object reachable from recursive search is
accounted for in sections A-F, with update sites and a proposed treatment. The
design decisions for the eight original open questions are recorded in
`architecture-inventory.md` §10. The overlay/isolated-worker implementation now
exists in `src/policy_research/` and is covered by the sandbox and macro-off
parity gates.

## H. Research-only live QE intervention (Phase 7)

`PolicyResearchLiveQE` is deliberately different from a shadow probe: it changes
the real search order. At each eligible null-window node it performs a separate,
read-only MovePicker enumeration, computes the frozen linear QE score, and, on a
promotion, reserves the selected static prefix from the node's actual local
MovePicker. The local loop then serves `[chosen,0..chosen-1]` before resuming at
the real suffix cursor. Recursive children have independent stack-local prefix
buffers; no TLS permutation arm is nested.

Mutation/ownership consequences:

- feature enumeration only reads live histories and position state;
- the actual reordered search intentionally changes TT, histories, pruning and
  descendant call distribution exactly as ordinary search would;
- the model and options compile only under `POLICY_RESEARCH`, default off;
- all experiments require Threads 1 and no concurrent dataset probes;
- default-off research parity and macro-off bench remain mandatory.

The live benchmark fails node and wall-time gates, so this hook is evidence
infrastructure, not a production candidate.
