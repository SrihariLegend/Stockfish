/*
  Stockfish, a UCI chess playing engine derived from Glaurung 2.1
  Copyright (C) 2004-2026 The Stockfish developers (see AUTHORS file)

  Stockfish is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  Stockfish is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "worker_snapshot.h"

#ifdef POLICY_RESEARCH

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../engine.h"
#include "../misc.h"
#include "../movegen.h"
#include "../movepick.h"
#include "../thread.h"
#include "../uci.h"
#include "research_log.h"
#include "research_options.h"

namespace Stockfish::Research {

namespace {

// Release-safe, exception-free fatal path (Stockfish builds with -fno-exceptions;
// see src/Makefile: CXXFLAGS). All research entry points pre-check
// is_safe_environment() before constructing an IsolatedWorker (search hook,
// CandidateProbeRunner, unit tests), so reaching this branch indicates a
// programming error, not a user misconfiguration.
[[noreturn]] void research_fatal(const std::string& msg) {
    std::cerr << "CRITICAL ERROR: " << msg << std::endl;
    std::exit(EXIT_FAILURE);
}

}  // namespace

bool IsolatedWorker::is_safe_environment(const Search::Worker& liveWorker) {
    return is_safe_configuration(int(liveWorker.options["Threads"]),
                                 liveWorker.threads.num_threads());
}

void IsolatedWorker::do_move(Position& pos, Move m, StateInfo& st, Search::Stack* ss) {
    shadowWorker->do_move(pos, m, st, ss);
}

void IsolatedWorker::undo_move(Position& pos, Move m) {
    shadowWorker->undo_move(pos, m);
}

Value IsolatedWorker::evaluate(const Position& pos) {
    return shadowWorker->evaluate(pos);
}

Value IsolatedWorker::evaluate_worker(Search::Worker& w, const Position& pos) {
    return w.evaluate(pos);
}

IsolatedWorker::IsolatedWorker(const Search::Worker& liveWorker) {
    // Safe isolation requires a single-thread pool (Threads == 1 option AND one
    // live worker). This is enforced in every build (release and debug alike;
    // no assert, no C++ exceptions under -fno-exceptions). The search hook, the
    // sandbox test entry, and CandidateProbeRunner all pre-check
    // is_safe_environment() before construction, so a violation here is a
    // programming error and takes the exception-free fatal path.
    if (!is_safe_environment(liveWorker))
        research_fatal("IsolatedWorker requires Threads == 1 and a single-thread pool for safe "
                       "isolation");

    const usize threadCount =
      std::max(usize(1), liveWorker.sharedHistory.get_size() / CORRHIST_BASE_SIZE);

    const NumaIndex numaIdx = liveWorker.numaAccessToken.get_numa_index();
    privateSharedHists.emplace(numaIdx, threadCount);

    privateThreadPool = std::make_unique<ThreadPool>();
    privateThreadPool->stop = false;

    privateSharedState = std::make_unique<Search::SharedState>(
      liveWorker.options, *privateThreadPool, liveWorker.tt, privateSharedHists, liveWorker.network);

    shadowWorker = make_unique_large_page<Search::Worker>(
      *privateSharedState,
      std::make_unique<Search::NullSearchManager>(),
      1,  // threadId = 1 (non-mainthread: suppresses time checks and UCI PV outputs)
      0,  // numaThreadId
      1,  // numaTotalThreads
      liveWorker.numaAccessToken);

    shadowStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
    shadowPV    = std::make_unique<std::array<Search::PVMoves, MAX_PLY + 10>>();

    sync_from(liveWorker);
}

IsolatedWorker::~IsolatedWorker() = default;

void IsolatedWorker::stop() {
    privateThreadPool->stop = true;
}

void IsolatedWorker::reset_stop() {
    privateThreadPool->stop = false;
}

bool IsolatedWorker::is_stopped() const {
    return privateThreadPool->stop.load(std::memory_order_relaxed);
}

void IsolatedWorker::sync_from(const Search::Worker& liveWorker) {
    // Worker-local history tables
    shadowWorker->mainHistory                   = liveWorker.mainHistory;
    shadowWorker->lowPlyHistory                 = liveWorker.lowPlyHistory;
    shadowWorker->captureHistory                = liveWorker.captureHistory;
    shadowWorker->continuationCorrectionHistory = liveWorker.continuationCorrectionHistory;
    shadowWorker->ttMoveHistory                 = liveWorker.ttMoveHistory;

    // Shared history tables (deep-copied into private storage)
    *shadowWorker->sharedHistory.continuationHistoryBlock =
      *liveWorker.sharedHistory.continuationHistoryBlock;

    const usize pawnSize = std::min(shadowWorker->sharedHistory.pawnHistory.get_size(),
                                    liveWorker.sharedHistory.pawnHistory.get_size());
    for (usize i = 0; i < pawnSize; ++i)
        shadowWorker->sharedHistory.pawnHistory[i] = liveWorker.sharedHistory.pawnHistory[i];

    const usize corrSize = std::min(shadowWorker->sharedHistory.correctionHistory.get_size(),
                                    liveWorker.sharedHistory.correctionHistory.get_size());
    for (usize i = 0; i < corrSize; ++i)
        shadowWorker->sharedHistory.correctionHistory[i] =
          liveWorker.sharedHistory.correctionHistory[i];

    // Search state scalars
    shadowWorker->nodes.store(liveWorker.nodes.load(std::memory_order_relaxed),
                              std::memory_order_relaxed);
    shadowWorker->tbHits.store(liveWorker.tbHits.load(std::memory_order_relaxed),
                               std::memory_order_relaxed);
    shadowWorker->bestMoveChanges.store(
      liveWorker.bestMoveChanges.load(std::memory_order_relaxed), std::memory_order_relaxed);
    shadowWorker->selDepth        = liveWorker.selDepth;
    shadowWorker->nmpMinPly       = liveWorker.nmpMinPly;
    shadowWorker->optimism[WHITE] = liveWorker.optimism[WHITE];
    shadowWorker->optimism[BLACK] = liveWorker.optimism[BLACK];
    shadowWorker->rootDepth       = (liveWorker.rootDepth > 0) ? liveWorker.rootDepth : Depth(1);
    shadowWorker->rootDelta       = (liveWorker.rootDelta > 0) ? liveWorker.rootDelta : Value(30);
    shadowWorker->reductions      = liveWorker.reductions;
    shadowWorker->limits          = liveWorker.limits;
    shadowWorker->tbConfig        = liveWorker.tbConfig;
    shadowWorker->lastIterationIdxPV = liveWorker.lastIterationIdxPV;
    shadowWorker->rootMoves       = liveWorker.rootMoves;
    shadowWorker->pvIdx           = liveWorker.pvIdx;
    shadowWorker->pvLast          = liveWorker.pvLast;

    shadowWorker->accumulatorStack = liveWorker.accumulatorStack;
    shadowWorker->refreshTable     = liveWorker.refreshTable;
}

Search::Stack* IsolatedWorker::clone_and_rebind_stack(const Search::Worker& liveWorker,
                                                      const Search::Stack*  liveSS,
                                                      int                   windowBefore,
                                                      int                   maxDepth) {
    const int targetPly = liveSS->ply;
    assert(targetPly >= 0 && targetPly < MAX_PLY);
    (void) maxDepth;

    Search::Stack* shadowSS = &(*shadowStack)[7 + targetPly];

    const char* live_cont_base =
      reinterpret_cast<const char*>(&liveWorker.continuationHistory);
    const char* shadow_cont_base =
      reinterpret_cast<const char*>(&shadowWorker->continuationHistory);

    const char* live_corr_base =
      reinterpret_cast<const char*>(&liveWorker.continuationCorrectionHistory);
    const char* shadow_corr_base =
      reinterpret_cast<const char*>(&shadowWorker->continuationCorrectionHistory);

    // Copy backwards window (including sentinel at -7 if requested)
    const int actualWindow = std::min(windowBefore, 7 + targetPly);
    for (int k = actualWindow; k >= 0; --k)
    {
        const Search::Stack* src = liveSS - k;
        Search::Stack*       dst = shadowSS - k;
        *dst                     = *src;

        // Rebind PV pointer to private storage
        dst->pv = &(*shadowPV)[7 + targetPly - k];

        // Rebind continuationHistory pointer into private continuationHistoryBlock
        if (src->continuationHistory != nullptr)
        {
            ptrdiff_t offset =
              reinterpret_cast<const char*>(src->continuationHistory) - live_cont_base;
            assert(offset >= 0 && offset < ptrdiff_t(sizeof(ContinuationHistoryBlock)));
            dst->continuationHistory = reinterpret_cast<PieceToHistory*>(
              const_cast<char*>(shadow_cont_base + offset));
        }
        else
        {
            dst->continuationHistory =
              &shadowWorker->continuationHistory[0][0][NO_PIECE][0];
        }

        // Rebind continuationCorrectionHistory pointer into private correction table
        if (src->continuationCorrectionHistory != nullptr)
        {
            ptrdiff_t offset =
              reinterpret_cast<const char*>(src->continuationCorrectionHistory) - live_corr_base;
            assert(offset >= 0
                   && offset < ptrdiff_t(sizeof(liveWorker.continuationCorrectionHistory)));
            dst->continuationCorrectionHistory = reinterpret_cast<CorrectionHistory<PieceTo>*>(
              const_cast<char*>(shadow_corr_base + offset));
        }
        else
        {
            dst->continuationCorrectionHistory =
              &shadowWorker->continuationCorrectionHistory[NO_PIECE][0];
        }
    }

    // Initialize ALL future forward frames up to the end of the stack array
    // so that selective extensions and quiescence searches to any ply have
    // valid sentinel pointers, correct ply values, and clean state.
    //
    // R2 fix (frame-token audit, docs/policy-research/reviews/): cutoffCnt of
    // the sampled node's own (ss + 1) frame is NOT reset by the node itself
    // (only (ss + 2) is zeroed at node entry); it accumulates the fail-high
    // count of every child searched at the next ply under this node's parent
    // -- including children of the node's EARLIER SIBLINGS. The main move
    // loop reads (ss + 1)->cutoffCnt for LMR adjustments, so zeroing future
    // frames here made the isolated replay's reductions diverge from the
    // live continuation whenever the live node followed cutting siblings
    // (observed as tt_hit-only baseline-vs-live node-count mismatches, live
    // subtree up to hundreds of do_moves smaller). Mirror the live stack's
    // cutoffCnt values instead; deeper future frames are re-zeroed by the
    // replayed node's own (ss + 2) reset before any read.
    for (int idx = 7 + targetPly + 1; idx < MAX_PLY + 10; ++idx)
    {
        Search::Stack* future                 = &(*shadowStack)[idx];
        future->ply                           = idx - 7;
        future->pv                            = &(*shadowPV)[idx];
        future->continuationHistory           = &shadowWorker->continuationHistory[0][0][NO_PIECE][0];
        future->continuationCorrectionHistory = &shadowWorker->continuationCorrectionHistory[NO_PIECE][0];
        future->staticEval                    = VALUE_NONE;
        future->statScore                     = 0;
        future->moveCount                     = 0;
        future->inCheck                       = false;
        future->ttPv                          = false;
        future->ttHit                         = false;
        future->followPV                      = false;
        future->cutoffCnt =
          (liveSS + (idx - (7 + targetPly)))->cutoffCnt;  // mirror live sibling leftovers
        future->reduction                     = 0;
        future->excludedMove                  = Move::none();
        future->currentMove                   = Move::none();
    }

    return shadowSS;
}

bool run_sandbox_unit_tests(Engine& engine) {
    std::cout << "info string research: starting sandbox unit tests..." << std::endl;

    Search::Worker* liveWorker = engine.main_worker();
    if (!liveWorker)
    {
        std::cerr << "sandbox_test: no live worker available" << std::endl;
        return false;
    }

    // Release-mode safety gate (must run before any IsolatedWorker construction):
    // the whole suite requires a single-thread configuration. Under
    // `setoption Threads 2` the suite must fail fast with a clear marker instead
    // of crashing or probing, and the engine must stay alive for the UCI caller.
    if (!IsolatedWorker::is_safe_environment(*liveWorker))
    {
        std::cerr << "sandbox_test: refusing to run: safe isolation requires Threads == 1 and a "
                     "single-thread pool (option Threads = "
                  << std::string(engine.get_options()["Threads"])
                  << ", pool threads = " << engine.get_threads().num_threads() << ")" << std::endl;
        return false;
    }

    // Part 1: Deep Position Cloning Test
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position pos;
        pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());

        const Key rootKey = pos.key();
        const std::string rootFen = pos.fen();

        // Play 4 moves to build up StateInfo chain and repetition context
        const std::vector<std::string> moves = {"e2e4", "c7c5", "g1f3", "d7d6"};
        std::vector<Move> appliedMoves;
        for (const auto& uciMove : moves)
        {
            Move m = UCIEngine::to_move(pos, uciMove);
            appliedMoves.push_back(m);
            states->emplace_back();
            pos.do_move(m, states->back());
        }

        const Key keyAfter4 = pos.key();
        const std::string fenAfter4 = pos.fen();
        assert(keyAfter4 != rootKey);

        Position clonedPos;
        std::vector<StateInfo> clonedStates;
        pos.clone_to(clonedPos, clonedStates);

        // Verification 1.1: FEN, key, side to move agreement
        if (clonedPos.key() != keyAfter4 || clonedPos.fen() != fenAfter4)
        {
            std::cerr << "sandbox_test 1.1 failed: clone key/fen mismatch" << std::endl;
            return false;
        }

        // Verification 1.2: Chain rebinding integrity
        if (clonedStates.size() != 5)
        {
            std::cerr << "sandbox_test 1.2 failed: state chain length mismatch (" << clonedStates.size() << " vs 5)" << std::endl;
            return false;
        }
        if (clonedPos.state() != &clonedStates[0])
        {
            std::cerr << "sandbox_test 1.2 failed: clonedPos.st does not point to head of clonedStates" << std::endl;
            return false;
        }
        for (size_t i = 0; i < 4; ++i)
        {
            if (clonedStates[i].previous != &clonedStates[i + 1])
            {
                std::cerr << "sandbox_test 1.2 failed: broken previous pointer at index " << i << std::endl;
                return false;
            }
        }
        if (clonedStates[4].previous != nullptr)
        {
            std::cerr << "sandbox_test 1.2 failed: terminal state previous != nullptr" << std::endl;
            return false;
        }

        // Verification 1.3: Mutation on clonedPos does not alter pos
        Move m5 = UCIEngine::to_move(clonedPos, "d2d4");
        StateInfo extraSt;
        clonedPos.do_move(m5, extraSt);
        if (pos.key() != keyAfter4 || pos.fen() != fenAfter4)
        {
            std::cerr << "sandbox_test 1.3 failed: move on clonedPos mutated original pos" << std::endl;
            return false;
        }

        // Verification 1.4: Undo on clonedPos restores identical state
        clonedPos.undo_move(m5);
        if (clonedPos.key() != keyAfter4 || clonedPos.fen() != fenAfter4)
        {
            std::cerr << "sandbox_test 1.4 failed: undo on clonedPos did not restore state" << std::endl;
            return false;
        }

        // Verification 1.5: Undoing all moves back to root on clonedPos restores rootKey
        for (int i = int(appliedMoves.size()) - 1; i >= 0; --i)
        {
            clonedPos.undo_move(appliedMoves[i]);
        }
        if (clonedPos.key() != rootKey || clonedPos.fen() != rootFen)
        {
            std::cerr << "sandbox_test 1.5 failed: full undo to root on clonedPos failed" << std::endl;
            return false;
        }
        if (pos.key() != keyAfter4)
        {
            std::cerr << "sandbox_test 1.5 failed: full undo on clonedPos mutated original pos" << std::endl;
            return false;
        }

        // Verification 1.6: Repetition detection on cloned position (twofold and true threefold)
        {
            Position repPos;
            std::vector<StateInfo> repStates;
            pos.clone_to(repPos, repStates);

            // pos is at ply 4: 1. e4 c5 2. Nf3 d6
            // Play knights back and forth twice to test twofold and true threefold repetition:
            // Cycle 1: 3. Ng1 Nf6 4. Nf3 Ng8 (2nd occurrence: distance 4)
            // Cycle 2: 5. Ng1 Nf6 6. Nf3 Ng8 (3rd occurrence: distance -4, true 3-fold!)
            const std::vector<std::string> repMoves = {
              "f3g1", "g8f6", "g1f3", "f6g8",
              "f3g1", "g8f6", "g1f3", "f6g8"
            };
            std::vector<StateInfo> moveStates(repMoves.size());
            std::vector<Move> appliedRepMoves;

            // Apply cycle 1 (moves 0..3)
            for (size_t i = 0; i < 4; ++i)
            {
                Move m = UCIEngine::to_move(repPos, repMoves[i]);
                appliedRepMoves.push_back(m);
                repPos.do_move(m, moveStates[i]);
            }

            // At 2nd occurrence: repetition distance is positive (+4)
            if (repPos.state()->repetition != 4)
            {
                std::cerr << "sandbox_test 1.6 failed: repPos 2nd occurrence repetition distance != 4 ("
                          << repPos.state()->repetition << ")" << std::endl;
                return false;
            }
            if (!repPos.has_repeated())
            {
                std::cerr << "sandbox_test 1.6 failed: repPos did not detect twofold repetition" << std::endl;
                return false;
            }
            if (repPos.is_draw(0))
            {
                std::cerr << "sandbox_test 1.6 failed: repPos is_draw(0) should be false for twofold at root" << std::endl;
                return false;
            }
            if (!repPos.is_draw(5))
            {
                std::cerr << "sandbox_test 1.6 failed: repPos is_draw(5) should be true for twofold after root" << std::endl;
                return false;
            }

            // Apply cycle 2 (moves 4..7)
            for (size_t i = 4; i < 8; ++i)
            {
                Move m = UCIEngine::to_move(repPos, repMoves[i]);
                appliedRepMoves.push_back(m);
                repPos.do_move(m, moveStates[i]);
            }

            // At 3rd occurrence: repetition distance must be negative (-4) in Stockfish
            if (repPos.state()->repetition != -4)
            {
                std::cerr << "sandbox_test 1.6 failed: repPos 3rd occurrence repetition distance != -4 ("
                          << repPos.state()->repetition << ")" << std::endl;
                return false;
            }
            if (!repPos.is_draw(0))
            {
                std::cerr << "sandbox_test 1.6 failed: repPos is_draw(0) should be true for threefold" << std::endl;
                return false;
            }
            if (!repPos.has_repeated())
            {
                std::cerr << "sandbox_test 1.6 failed: repPos did not detect threefold repetition" << std::endl;
                return false;
            }

            if (pos.has_repeated())
            {
                std::cerr << "sandbox_test 1.6 failed: live pos has_repeated was mutated" << std::endl;
                return false;
            }

            // Undo all 8 moves step-by-step
            for (int i = int(appliedRepMoves.size()) - 1; i >= 4; --i)
                repPos.undo_move(appliedRepMoves[i]);

            // Back at cycle 1: repetition distance must be +4 again
            if (repPos.state()->repetition != 4)
            {
                std::cerr << "sandbox_test 1.6 failed: repPos repetition distance != 4 after partial undo ("
                          << repPos.state()->repetition << ")" << std::endl;
                return false;
            }

            for (int i = 3; i >= 0; --i)
                repPos.undo_move(appliedRepMoves[i]);

            if (repPos.has_repeated() || repPos.state()->repetition != 0)
            {
                std::cerr << "sandbox_test 1.6 failed: repPos still has_repeated after full undo" << std::endl;
                return false;
            }
            if (repPos.key() != keyAfter4 || repPos.fen() != fenAfter4)
            {
                std::cerr << "sandbox_test 1.6 failed: repPos key/fen mismatch after full undo" << std::endl;
                return false;
            }
        }
    }

    // Part 2: Worker Isolation and History Deep Copy
    {
        IsolatedWorker shadowOwner(*liveWorker);

        // Seed distinctive values into live worker
        const u16 moveRaw = 100;
        const Square toSq = SQ_E4;

        const i16 origMain = liveWorker->mainHistory[WHITE][moveRaw];
        const i16 origCapt = liveWorker->captureHistory[W_PAWN][toSq][PAWN];
        const i16 origCorr = liveWorker->continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq];
        const i16 origCont = liveWorker->continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq];

        liveWorker->mainHistory[WHITE][moveRaw] = i16(123);
        liveWorker->captureHistory[W_PAWN][toSq][PAWN] = i16(456);
        liveWorker->continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq] = i16(789);
        liveWorker->continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq] = i16(321);

        shadowOwner.sync_from(*liveWorker);

        // Verification 2.1: Values copied correctly to shadow worker
        if (i16(shadowOwner.worker().mainHistory[WHITE][moveRaw]) != 123
            || i16(shadowOwner.worker().captureHistory[W_PAWN][toSq][PAWN]) != 456
            || i16(shadowOwner.worker().continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq]) != 789
            || i16(shadowOwner.worker().continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq]) != 321)
        {
            std::cerr << "sandbox_test 2.1 failed: sync_from did not copy history values" << std::endl;
            return false;
        }

        // Verification 2.2: Mutating shadow worker does not mutate live worker
        shadowOwner.worker().mainHistory[WHITE][moveRaw] = i16(999);
        shadowOwner.worker().captureHistory[W_PAWN][toSq][PAWN] = i16(888);
        shadowOwner.worker().continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq] = i16(777);
        shadowOwner.worker().continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq] = i16(666);

        if (i16(liveWorker->mainHistory[WHITE][moveRaw]) != 123
            || i16(liveWorker->captureHistory[W_PAWN][toSq][PAWN]) != 456
            || i16(liveWorker->continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq]) != 789
            || i16(liveWorker->continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq]) != 321)
        {
            std::cerr << "sandbox_test 2.2 failed: mutation in shadow worker mutated live worker!" << std::endl;
            return false;
        }

        // Clean up live worker: restore EXACT original values
        liveWorker->mainHistory[WHITE][moveRaw] = origMain;
        liveWorker->captureHistory[W_PAWN][toSq][PAWN] = origCapt;
        liveWorker->continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq] = origCorr;
        liveWorker->continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq] = origCont;
    }

    // Part 3: Stack Window Cloning and Pointer Rebinding
    {
        IsolatedWorker shadowOwner(*liveWorker);

        auto liveStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* liveSS = &(*liveStack)[7 + 3]; // ply 3
        liveSS->ply = 3;
        (liveSS - 1)->ply = 2;
        (liveSS - 2)->ply = 1;

        const Square toSq = SQ_E4;
        (liveSS - 1)->continuationHistory = &liveWorker->continuationHistory[0][0][W_PAWN][toSq];
        (liveSS - 1)->continuationCorrectionHistory = &liveWorker->continuationCorrectionHistory[W_PAWN][toSq];

        Search::Stack* shadowSS = shadowOwner.clone_and_rebind_stack(*liveWorker, liveSS, 7, 10);

        // Verification 3.1: Shadow stack is rebound
        if (shadowSS == liveSS)
        {
            std::cerr << "sandbox_test 3.1 failed: shadowSS == liveSS" << std::endl;
            return false;
        }
        if (shadowSS->ply != 3)
        {
            std::cerr << "sandbox_test 3.1 failed: shadowSS ply mismatch" << std::endl;
            return false;
        }
        if ((shadowSS - 1)->continuationHistory == (liveSS - 1)->continuationHistory)
        {
            std::cerr << "sandbox_test 3.1 failed: continuationHistory still points to live worker!" << std::endl;
            return false;
        }
        if ((shadowSS - 1)->continuationHistory != &shadowOwner.worker().continuationHistory[0][0][W_PAWN][toSq])
        {
            std::cerr << "sandbox_test 3.1 failed: continuationHistory does not point to shadowWorker" << std::endl;
            return false;
        }
        if ((shadowSS - 1)->continuationCorrectionHistory == (liveSS - 1)->continuationCorrectionHistory)
        {
            std::cerr << "sandbox_test 3.1 failed: continuationCorrectionHistory still points to live worker!" << std::endl;
            return false;
        }
        if ((shadowSS - 1)->continuationCorrectionHistory != &shadowOwner.worker().continuationCorrectionHistory[W_PAWN][toSq])
        {
            std::cerr << "sandbox_test 3.1 failed: continuationCorrectionHistory does not point to shadowWorker" << std::endl;
            return false;
        }
    }

    // Part 4: Real Shadow Search Execution with ResearchTTOverlay
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position pos;
        // Standard starting position after e2e4
        pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        Move m1 = UCIEngine::to_move(pos, "e2e4");
        states->emplace_back();
        pos.do_move(m1, states->back());

        const Key liveKeyBefore = pos.key();
        const u64 liveNodesBefore = liveWorker->get_nodes();
        const bool liveStopBefore = engine.get_threads().stop.load();

        TranspositionTable& baseTT = engine.get_tt();
        ResearchTTOverlay overlay(baseTT);

        // Snapshot the ENTIRE base TT byte buffer before search to verify 100% whole-table immutability
        std::vector<u8> wholeTTBefore(baseTT.byte_size());
        std::memcpy(wholeTTBefore.data(), baseTT.cluster_data(), baseTT.byte_size());

        IsolatedWorker shadowOwner(*liveWorker);

        Position shadowPos;
        std::vector<StateInfo> shadowStates;
        pos.clone_to(shadowPos, shadowStates);

        auto dummyLiveStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummyLiveSS = &(*dummyLiveStack)[7 + 1];
        dummyLiveSS->ply = 1;
        for (int i = 7; i > 0; --i)
        {
            (dummyLiveSS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummyLiveSS - i)->continuationCorrectionHistory = &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummyLiveSS - i)->staticEval = VALUE_NONE;
        }

        Search::Stack* shadowSS = shadowOwner.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);

        // Execute shadow search at depth 3
        const Value val1 = shadowOwner.search<PV>(
            shadowPos, shadowSS, -100, 100, Depth(3), false, overlay);

        // Verification 4.1: Search produced valid score
        if (std::abs(val1) >= VALUE_INFINITE)
        {
            std::cerr << "sandbox_test 4.1 failed: shadow search returned infinite score (" << val1 << ")" << std::endl;
            return false;
        }

        // Verification 4.2: Shadow worker nodes incremented, live worker nodes untouched
        const u64 shadowNodes = shadowOwner.nodes_searched() - liveNodesBefore;
        if (shadowNodes == 0)
        {
            std::cerr << "sandbox_test 4.2 failed: shadow search did not search any nodes" << std::endl;
            return false;
        }
        if (liveWorker->get_nodes() != liveNodesBefore)
        {
            std::cerr << "sandbox_test 4.2 failed: live worker nodes mutated ("
                      << liveWorker->get_nodes() << " vs " << liveNodesBefore << ")" << std::endl;
            return false;
        }

        // Verification 4.3: Live position and cloned position keys intact
        if (pos.key() != liveKeyBefore)
        {
            std::cerr << "sandbox_test 4.3 failed: live position key was mutated" << std::endl;
            return false;
        }
        if (shadowPos.key() != liveKeyBefore)
        {
            std::cerr << "sandbox_test 4.3 failed: shadow position key was not restored after search" << std::endl;
            return false;
        }

        // Verification 4.4: Overlay captured writes, base TT untouched across 100% of its clusters
        if (overlay.overlay_cluster_count() == 0)
        {
            std::cerr << "sandbox_test 4.4 failed: overlay recorded 0 cluster copies during search" << std::endl;
            return false;
        }
        if (std::memcmp(wholeTTBefore.data(), baseTT.cluster_data(), baseTT.byte_size()) != 0)
        {
            std::cerr << "sandbox_test 4.4 failed: base TT was mutated during shadow search!" << std::endl;
            return false;
        }

        // Verification 4.5: Determinism — repeat search with fresh overlay and cloned state
        ResearchTTOverlay overlay2(baseTT);
        IsolatedWorker shadowOwner2(*liveWorker);
        Position shadowPos2;
        std::vector<StateInfo> shadowStates2;
        pos.clone_to(shadowPos2, shadowStates2);
        Search::Stack* shadowSS2 = shadowOwner2.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);

        const Value val2 = shadowOwner2.search<PV>(
            shadowPos2, shadowSS2, -100, 100, Depth(3), false, overlay2);

        const u64 shadowNodes2 = shadowOwner2.nodes_searched() - liveNodesBefore;

        if (val1 != val2)
        {
            std::cerr << "sandbox_test 4.5 failed: non-deterministic value (" << val1 << " vs " << val2 << ")" << std::endl;
            return false;
        }
        if (shadowNodes != shadowNodes2)
        {
            std::cerr << "sandbox_test 4.5 failed: non-deterministic nodes (" << shadowNodes << " vs " << shadowNodes2 << ")" << std::endl;
            return false;
        }

        // Verification 4.6: Zero-window NonPV shadow search
        ResearchTTOverlay overlayNonPV(baseTT);
        IsolatedWorker shadowOwnerNonPV(*liveWorker);
        Position shadowPosNonPV;
        std::vector<StateInfo> shadowStatesNonPV;
        pos.clone_to(shadowPosNonPV, shadowStatesNonPV);
        Search::Stack* shadowSSNonPV = shadowOwnerNonPV.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);

        const Value valNonPV = shadowOwnerNonPV.search<NonPV>(
            shadowPosNonPV, shadowSSNonPV, val1 - 1, val1, Depth(3), false, overlayNonPV);

        if (std::abs(valNonPV) >= VALUE_INFINITE)
        {
            std::cerr << "sandbox_test 4.6 failed: NonPV shadow search returned infinite score" << std::endl;
            return false;
        }
        const u64 shadowNodesNonPV = shadowOwnerNonPV.nodes_searched() - liveNodesBefore;
        if (shadowNodesNonPV == 0)
        {
            std::cerr << "sandbox_test 4.6 failed: NonPV shadow search searched 0 nodes" << std::endl;
            return false;
        }

        // Verification 4.7: Independent stop state on shadow worker
        {
            IsolatedWorker cancellableOwner(*liveWorker);
            Position cancelPos;
            std::vector<StateInfo> cancelStates;
            pos.clone_to(cancelPos, cancelStates);
            Search::Stack* cancelSS = cancellableOwner.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);
            ResearchTTOverlay cancelOverlay(baseTT);

            cancellableOwner.stop();
            if (!cancellableOwner.is_stopped())
            {
                std::cerr << "sandbox_test 4.7 failed: shadow worker is not stopped after stop()" << std::endl;
                return false;
            }

            // Live thread pool stop flag must NOT be mutated
            if (engine.get_threads().stop.load() != liveStopBefore)
            {
                std::cerr << "sandbox_test 4.7 failed: cancellableOwner.stop() mutated live thread pool stop flag!" << std::endl;
                return false;
            }

            const u64 nodesBeforeCancel = cancellableOwner.nodes_searched();
            cancellableOwner.search<PV>(cancelPos, cancelSS, -100, 100, Depth(5), false, cancelOverlay);
            const u64 nodesDuringCancel = cancellableOwner.nodes_searched() - nodesBeforeCancel;
            if (nodesDuringCancel > 2)
            {
                std::cerr << "sandbox_test 4.7 failed: stopped shadow search ran " << nodesDuringCancel << " nodes instead of aborting" << std::endl;
                return false;
            }

            cancellableOwner.reset_stop();
            if (cancellableOwner.is_stopped())
            {
                std::cerr << "sandbox_test 4.7 failed: shadow worker still stopped after reset_stop()" << std::endl;
                return false;
            }
        }

        // Verification 4.8: ScopedShadowProbe guard integrity
        if (ScopedShadowProbe::is_active())
        {
            std::cerr << "sandbox_test 4.8 failed: ScopedShadowProbe active outside probe scope" << std::endl;
            return false;
        }
        {
            ScopedShadowProbe guard;
            if (!ScopedShadowProbe::is_active() || ScopedShadowProbe::depth() != 1)
            {
                std::cerr << "sandbox_test 4.8 failed: ScopedShadowProbe not active inside guard" << std::endl;
                return false;
            }
        }
        if (ScopedShadowProbe::is_active())
        {
            std::cerr << "sandbox_test 4.8 failed: ScopedShadowProbe still active after guard destroyed" << std::endl;
            return false;
        }

        // Verification 4.9: Automatic node budget in shadow search
        {
            IsolatedWorker budgetOwner(*liveWorker);
            Position bPos;
            std::vector<StateInfo> bStates;
            pos.clone_to(bPos, bStates);
            Search::Stack* bSS = budgetOwner.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);
            ResearchTTOverlay bOverlay(baseTT);

            const u64 bLiveNodesBefore = liveWorker->get_nodes();
            // Search with a budget of 15 nodes on a depth 5 search
            const u64 nodeBudget = 15;
            budgetOwner.search<PV>(bPos, bSS, -100, 100, Depth(5), false, bOverlay, nodeBudget);

            // Shadow search must have stopped when reaching budget
            if (!budgetOwner.is_stopped())
            {
                std::cerr << "sandbox_test 4.9 failed: shadow worker did not stop after reaching budget" << std::endl;
                return false;
            }
            const u64 bNodesSearched = budgetOwner.nodes_searched() - bLiveNodesBefore;
            if (bNodesSearched == 0 || bNodesSearched > 40)
            {
                std::cerr << "sandbox_test 4.9 failed: nodes searched (" << bNodesSearched
                          << ") not cleanly bounded by budget (" << nodeBudget << ")" << std::endl;
                return false;
            }
            // Live thread pool stop flag must NOT be mutated
            if (engine.get_threads().stop.load() != liveStopBefore)
            {
                std::cerr << "sandbox_test 4.9 failed: budget search mutated live thread pool stop flag!" << std::endl;
                return false;
            }
        }

        // Verification 4.10: End-to-end recorder suppression during shadow search
        {
            Recorder& rec = recorder();
            rec.test_arm_for_unit_tests(0x12345678ULL);

            if (!rec.active() || !rec.is_root_open())
            {
                std::cerr << "sandbox_test 4.10 failed: recorder not active outside probe" << std::endl;
                rec.test_disarm_for_unit_tests();
                return false;
            }

            const u64 recDecBefore = rec.get_run_decision_total();
            const u64 recAttBefore = rec.get_run_attempt_total();

            // Run shadow search under active recorder arming
            IsolatedWorker recShadowOwner(*liveWorker);
            Position recPos;
            std::vector<StateInfo> recStates;
            pos.clone_to(recPos, recStates);
            Search::Stack* recSS = recShadowOwner.clone_and_rebind_stack(*liveWorker, dummyLiveSS, 7, 10);
            ResearchTTOverlay recOverlay(baseTT);

            recShadowOwner.search<PV>(recPos, recSS, -100, 100, Depth(3), false, recOverlay);

            // Verify that zero records were emitted and recorder state was completely untouched
            if (rec.get_run_decision_total() != recDecBefore || rec.get_run_attempt_total() != recAttBefore)
            {
                std::cerr << "sandbox_test 4.10 failed: shadow search emitted recorder events!" << std::endl;
                rec.test_disarm_for_unit_tests();
                return false;
            }

            // Verify ScopedShadowProbe directly inside probe scope
            {
                ScopedShadowProbe shadowGuard;
                if (rec.active())
                {
                    std::cerr << "sandbox_test 4.10 failed: recorder still active inside ScopedShadowProbe" << std::endl;
                    rec.test_disarm_for_unit_tests();
                    return false;
                }
            }

            rec.test_disarm_for_unit_tests();
        }
    }

    // Part 5: Active Internal-Node NNUE Evaluation and Accumulator Fidelity
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position nnuePos;
        nnuePos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        Move m1 = UCIEngine::to_move(nnuePos, "e2e4");
        states->emplace_back();
        nnuePos.do_move(m1, states->back());

        Position shadowNnuePos;
        std::vector<StateInfo> shadowNnueStates;
        nnuePos.clone_to(shadowNnuePos, shadowNnueStates);

        IsolatedWorker shadowOwner(*liveWorker);

        // Verification 5.1: Bit-for-bit NNUE evaluation agreement between live and shadow workers
        const Value liveVal = IsolatedWorker::evaluate_worker(*liveWorker, nnuePos);
        const Value shadowVal = shadowOwner.evaluate(shadowNnuePos);

        if (liveVal != shadowVal)
        {
            std::cerr << "sandbox_test 5.1 failed: NNUE evaluation mismatch ("
                      << liveVal << " vs " << shadowVal << ")" << std::endl;
            return false;
        }

        // Verification 5.2: State mutation and undo on cloned position preserves NNUE fidelity
        Move testMove = UCIEngine::to_move(shadowNnuePos, "c7c5");
        StateInfo testSt;
        shadowNnuePos.do_move(testMove, testSt);
        const Value shadowValMoved = shadowOwner.evaluate(shadowNnuePos);
        if (shadowValMoved == VALUE_NONE || std::abs(shadowValMoved) >= VALUE_INFINITE)
        {
            std::cerr << "sandbox_test 5.2 failed: NNUE evaluation invalid after move ("
                      << shadowValMoved << ")" << std::endl;
            return false;
        }

        shadowNnuePos.undo_move(testMove);
        const Value shadowValRestored = shadowOwner.evaluate(shadowNnuePos);
        if (shadowValRestored != liveVal)
        {
            std::cerr << "sandbox_test 5.2 failed: NNUE evaluation after undo mismatch ("
                      << shadowValRestored << " vs " << liveVal << ")" << std::endl;
            return false;
        }

        // Verification 5.3: Live worker evaluation is completely untouched
        const Value liveValAfter = IsolatedWorker::evaluate_worker(*liveWorker, nnuePos);
        if (liveValAfter != liveVal)
        {
            std::cerr << "sandbox_test 5.3 failed: live worker NNUE evaluation mutated!" << std::endl;
            return false;
        }
    }

    // Part 6: Single-Thread Safety Enforcement (assert-free, runtime check in release builds)
    {
        if (!IsolatedWorker::is_safe_environment(*liveWorker))
        {
            std::cerr << "sandbox_test 6.1 failed: live worker does not report safe environment" << std::endl;
            return false;
        }

        // 6.2: the pure configuration predicate rejects both unsafe halves
        // without touching the live pool (raising the Threads option would
        // resize the pool and replace Thread objects, dangling liveWorker).
        // End-to-end runtime rejection with a real 2-thread pool is exercised
        // by the Python driver on a dedicated engine process.
        if (!IsolatedWorker::is_safe_configuration(1, 1))
        {
            std::cerr << "sandbox_test 6.2 failed: safe configuration misreported unsafe" << std::endl;
            return false;
        }
        if (IsolatedWorker::is_safe_configuration(2, 1)
            || IsolatedWorker::is_safe_configuration(1, 2))
        {
            std::cerr << "sandbox_test 6.2 failed: unsafe configuration misreported safe" << std::endl;
            return false;
        }
    }

    // Part 7: Structured ProbeResult & Budget Censoring
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p7Pos;
        p7Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        Move m = UCIEngine::to_move(p7Pos, "e2e4");
        states->emplace_back();
        p7Pos.do_move(m, states->back());

        IsolatedWorker worker7(*liveWorker);
        Position shadowPos;
        std::vector<StateInfo> shadowStates;
        p7Pos.clone_to(shadowPos, shadowStates);

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply = 1;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory = &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }
        Search::Stack* shadowSS = worker7.clone_and_rebind_stack(*liveWorker, dummySS, 7, 10);
        ResearchTTOverlay overlay7(engine.get_tt());

        // 7.1: Normal unbudgeted search produces completed ProbeResult with a value
        ProbeResult resNormal = worker7.probe<PV>(shadowPos, shadowSS, -100, 100, Depth(3), false, overlay7, 0);
        if (!resNormal.completed || resNormal.hit_budget || resNormal.nodes == 0
            || resNormal.score == VALUE_NONE || std::abs(resNormal.score) >= VALUE_INFINITE)
        {
            std::cerr << "sandbox_test 7.1 failed: normal probe not completed" << std::endl;
            return false;
        }

        // 7.2: Budgeted search with tight budget sets hit_budget and completed == false,
        // and the censored probe carries NO value (score == VALUE_NONE, never a numeric
        // score and never a draw substitute).
        ResearchTTOverlay overlayBudget(engine.get_tt());
        IsolatedWorker workerBudget(*liveWorker);
        Position shadowPosB;
        std::vector<StateInfo> shadowStatesB;
        p7Pos.clone_to(shadowPosB, shadowStatesB);
        Search::Stack* shadowSSB = workerBudget.clone_and_rebind_stack(*liveWorker, dummySS, 7, 10);

        ProbeResult resBudget = workerBudget.probe<PV>(shadowPosB, shadowSSB, -100, 100, Depth(6), false, overlayBudget, 10);
        if (resBudget.completed)
        {
            std::cerr << "sandbox_test 7.2 failed: budgeted probe marked completed" << std::endl;
            return false;
        }
        if (!resBudget.hit_budget)
        {
            std::cerr << "sandbox_test 7.2 failed: budgeted probe hit_budget is false" << std::endl;
            return false;
        }
        if (resBudget.score != VALUE_NONE)
        {
            std::cerr << "sandbox_test 7.2 failed: censored probe carries a score ("
                      << int(resBudget.score) << ") instead of VALUE_NONE" << std::endl;
            return false;
        }
        if (resBudget.stopReason != ProbeResult::StopReason::Budget)
        {
            std::cerr << "sandbox_test 7.2 failed: censored probe stopReason != Budget" << std::endl;
            return false;
        }

        // 7.3: User-stop censoring (stop raised before the probe) is censored with
        // stopReason == UserStop, no value, and does not masquerade as a budget hit.
        ResearchTTOverlay overlayUserStop(engine.get_tt());
        IsolatedWorker workerUserStop(*liveWorker);
        Position shadowPosU;
        std::vector<StateInfo> shadowStatesU;
        p7Pos.clone_to(shadowPosU, shadowStatesU);
        Search::Stack* shadowSSU = workerUserStop.clone_and_rebind_stack(*liveWorker, dummySS, 7, 10);
        workerUserStop.stop();
        ProbeResult resUserStop = workerUserStop.probe<PV>(
          shadowPosU, shadowSSU, -100, 100, Depth(5), false, overlayUserStop, 0);
        if (resUserStop.completed || resUserStop.score != VALUE_NONE
            || resUserStop.hit_budget
            || resUserStop.stopReason != ProbeResult::StopReason::UserStop)
        {
            std::cerr << "sandbox_test 7.3 failed: user-stopped probe misreported" << std::endl;
            return false;
        }
    }

    // Part 8: Whole-Node Replay Isolation, Order Invariance & Force-Next
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p8Pos;
        p8Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                  &states->back());
        Move m = UCIEngine::to_move(p8Pos, "e2e4");
        states->emplace_back();
        p8Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply            = 1;
        dummySS->staticEval     = VALUE_NONE;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        Move candA = UCIEngine::to_move(p8Pos, "c7c5");
        Move candB = UCIEngine::to_move(p8Pos, "e7e5");

        // Sequence A: baseline + forced(candA) then forced(candB) on one runner.
        CandidateProbeRunner runnerA(*liveWorker, p8Pos, dummySS);
        ProbeResult baseA   = runnerA.replay_node(-50, -49, Depth(3), false, 0);
        ProbeResult resA1   = runnerA.replay_node_forced(candA, -50, -49, Depth(3), false, 0);
        ProbeResult resB1   = runnerA.replay_node_forced(candB, -50, -49, Depth(3), false, 0);

        // Sequence B: baseline + forced(candB) then forced(candA) on a fresh
        // runner: per-candidate replays must be order-invariant and identical
        // to sequence A's per-candidate outcomes.
        CandidateProbeRunner runnerB(*liveWorker, p8Pos, dummySS);
        ProbeResult baseB   = runnerB.replay_node(-50, -49, Depth(3), false, 0);
        ProbeResult resB2   = runnerB.replay_node_forced(candB, -50, -49, Depth(3), false, 0);
        ProbeResult resA2   = runnerB.replay_node_forced(candA, -50, -49, Depth(3), false, 0);

        if (baseA.nodes != baseB.nodes || baseA.score != baseB.score
            || baseA.completed != baseB.completed)
        {
            std::cerr << "sandbox_test 8.1 failed: baseline replay non-deterministic across "
                         "runners"
                      << std::endl;
            return false;
        }
        if (resA1.nodes != resA2.nodes || resA1.score != resA2.score
            || resA1.completed != resA2.completed || resA1.forced_slot1 != resA2.forced_slot1)
        {
            std::cerr << "sandbox_test 8.2 failed: forced(candA) non-deterministic across "
                         "evaluation orders ("
                      << resA1.nodes << ":" << int(resA1.score) << ":" << resA1.forced_slot1
                      << " vs " << resA2.nodes << ":" << int(resA2.score) << ":"
                      << resA2.forced_slot1 << ")" << std::endl;
            return false;
        }
        if (resB1.nodes != resB2.nodes || resB1.score != resB2.score
            || resB1.completed != resB2.completed || resB1.forced_slot1 != resB2.forced_slot1)
        {
            std::cerr << "sandbox_test 8.3 failed: forced(candB) non-deterministic across "
                         "evaluation orders"
                      << std::endl;
            return false;
        }

        // 8.4: This is a real decision node reached at the move loop, so the
        // forced replays must report forced_slot1 == true (the arm was
        // consumed by the slot-1 emission).
        if (!resA1.completed || !resA1.forced_slot1)
        {
            std::cerr << "sandbox_test 8.4 failed: whole-node replay did not consume the "
                         "force-next arm (completed="
                      << resA1.completed << " forced_slot1=" << resA1.forced_slot1 << ")"
                      << std::endl;
            return false;
        }

        // 8.5: Unforced baseline replays report forced_slot1 == false and are
        // not censored by an empty arm.
        if (baseA.forced_slot1)
        {
            std::cerr << "sandbox_test 8.5 failed: baseline replay consumed an arm" << std::endl;
            return false;
        }

        // 8.6: Forcing an illegal move is rejected up front (censored,
        // empty result, no search). King e1-d2 is pseudo-legal but illegal
        // (d2 is covered by the rook on e2; Kxe2 is the only legal king move).
        StateListPtr st8x   = std::make_unique<std::deque<StateInfo>>(1);
        Position     p8x;
        p8x.set("4k3/8/8/8/8/8/4r3/4K3 w - - 0 1", false, &st8x->back());
        Move nonLegal = Move(SQ_E1, SQ_D2);
        if (!p8x.pseudo_legal(nonLegal) || p8x.legal(nonLegal))
        {
            std::cerr << "sandbox_test 8.6 setup failed: expected pseudo-legal but illegal move"
                      << std::endl;
            return false;
        }
        CandidateProbeRunner runnerX(*liveWorker, p8x, dummySS);
        ProbeResult resIllegal = runnerX.replay_node_forced(nonLegal, -50, -49, Depth(3), false, 0);
        if (resIllegal.completed || resIllegal.nodes != 0 || resIllegal.forced_slot1)
        {
            std::cerr << "sandbox_test 8.6 failed: illegal forced move not rejected" << std::endl;
            return false;
        }
    }

    // Part 9: Full Candidate Denominator Enumeration (real MovePicker drive)
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p9Pos;
        p9Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                  &states->back());
        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7];
        dummySS->ply        = 0;
        dummySS->staticEval = VALUE_NONE;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        Move ttMove = UCIEngine::to_move(p9Pos, "e2e4");
        auto candidates =
          enumerate_candidates(p9Pos, *liveWorker, dummySS, ttMove, Depth(8));

        // 9.1: full legal denominator, ordinals contiguous from 0 in emission
        // order, every candidate legal.
        if (candidates.size() != 20)
        {
            std::cerr << "sandbox_test 9.1 failed: candidate count != 20 (" << candidates.size()
                      << ")" << std::endl;
            return false;
        }
        for (usize i = 0; i < candidates.size(); ++i)
        {
            const auto& cf = candidates[i];
            if (cf.ordinal != i || !p9Pos.legal(cf.move))
            {
                std::cerr << "sandbox_test 9.2 failed: ordinal/legality invariant at " << i
                          << std::endl;
                return false;
            }
        }

        // 9.3: the pseudo-legal TT move is the first emission with stage TT.
        if (!candidates.empty() && (candidates[0].move != ttMove || !candidates[0].isTTMove
                                    || candidates[0].stage != CandidateStage::TT))
        {
            std::cerr << "sandbox_test 9.3 failed: TT move not emitted first as TT stage (first="
                      << UCIEngine::move(candidates[0].move, false)
                      << " isTT=" << candidates[0].isTTMove
                      << " stage=" << candidate_stage_name(candidates[0].stage) << ")" << std::endl;
            return false;
        }

        // 9.4: differential drive - an independently constructed fresh
        // MovePicker with identical parameters must emit the identical legal
        // sequence with identical per-emission scores.
        const Color        us = p9Pos.side_to_move();
        const PieceToHistory* window[6] = {};
        for (int i = 0; i < 6; ++i)
            window[i] = (dummySS - (i + 1))->continuationHistory;
        MovePicker mp2(p9Pos, ttMove, Depth(8), &liveWorker->mainHistory,
                       &liveWorker->lowPlyHistory, &liveWorker->captureHistory, window,
                       &liveWorker->sharedHistory, dummySS->ply);
        usize      ordinal = 0;
        for (Move mv = mp2.next_move(); mv != Move::none(); mv = mp2.next_move())
        {
            if (!p9Pos.legal(mv))
                continue;
            if (ordinal >= candidates.size() || candidates[ordinal].move != mv)
            {
                std::cerr << "sandbox_test 9.4 failed: independent drive diverged at " << ordinal
                          << std::endl;
                return false;
            }
            const auto& cf = candidates[ordinal];
            if (cf.stage != CandidateStage::TT)
            {
                const int emScore = mp2.research_emitted_score();
                if (emScore != cf.stageScore)
                {
                    std::cerr << "sandbox_test 9.5 failed: emission score mismatch at " << ordinal
                              << std::endl;
                    return false;
                }
                // Classification re-derivation from the picker's own score:
                if (cf.isCapture)
                {
                    const bool good = p9Pos.see_ge(mv, -emScore / 18);
                    if ((cf.stage == CandidateStage::GoodCapture) != good)
                    {
                        std::cerr << "sandbox_test 9.6 failed: capture split mismatch at "
                                  << ordinal << std::endl;
                        return false;
                    }
                }
                else
                {
                    const bool good = emScore > -14000;
                    if ((cf.stage == CandidateStage::GoodQuiet) != good)
                    {
                        std::cerr << "sandbox_test 9.7 failed: quiet split mismatch at "
                                  << ordinal << std::endl;
                        return false;
                    }
                }
            }
            ++ordinal;
        }
        if (ordinal != candidates.size())
        {
            std::cerr << "sandbox_test 9.8 failed: independent drive ended early (" << ordinal
                      << " vs " << candidates.size() << ")" << std::endl;
            return false;
        }

        // 9.9: enumeration must not perturb the live worker histories (pure
        // read-only scoring): a second enumeration equals the first.
        auto candidates2 = enumerate_candidates(p9Pos, *liveWorker, dummySS, ttMove, Depth(8));
        if (candidates2.size() != candidates.size())
        {
            std::cerr << "sandbox_test 9.9 failed: enumeration count changed across drives"
                      << std::endl;
            return false;
        }
        for (usize i = 0; i < candidates.size(); ++i)
            if (candidates2[i].move != candidates[i].move
                || candidates2[i].stageScore != candidates[i].stageScore
                || candidates2[i].mainHist != candidates[i].mainHist)
            {
                std::cerr << "sandbox_test 9.10 failed: enumeration mutated live history state"
                          << std::endl;
                return false;
            }
    }

    // Part 10: Armed-Dataset Hook Invariant, Non-Mutation & Row Pipeline
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p10Pos;
        p10Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                   &states->back());
        Move m = UCIEngine::to_move(p10Pos, "e2e4");
        states->emplace_back();
        p10Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply            = 1;
        dummySS->staticEval     = 0;
        dummySS->ttHit          = false;
        dummySS->excludedMove   = Move::none();
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        const Key  keyBefore   = p10Pos.key();
        const u64  nodesBefore = liveWorker->get_nodes();
        const bool savedStop =
          engine.get_threads().stop.load(std::memory_order_relaxed);
        // The suite runs outside a `go`, where the pool's stop flag is raised
        // (it is cleared when a real search starts). Direct hook invocation
        // must simulate the in-search condition: stop == false. Restored below.
        engine.get_threads().stop.store(false, std::memory_order_relaxed);
        TranspositionTable& baseTT = engine.get_tt();
        std::vector<u8> ttSnapshot(baseTT.byte_size());
        std::memcpy(ttSnapshot.data(), baseTT.cluster_data(), baseTT.byte_size());

        // Arm an internal dataset log (temp file) and the sampler options.
        const std::string datasetPath = "/tmp/policy_research_sandbox_dataset.jsonl";
        std::remove(datasetPath.c_str());
        Research::internal_log().test_arm_for_unit_tests(datasetPath, keyBefore);
        Research::config().switchOn    = true;
        Research::config().mode        = Research::Mode::InternalCounterfactual;
        Research::config().sampleRate  = 1.0;
        Research::config().topK        = 2;
        Research::config().nodeBudget  = 300;
        Research::config().seed        = 0x5EED1234ULL;

        // Give the live worker fixed-depth-style limits (mirrors `go depth N`
        // with no clock) so the hook's gates pass; restore afterwards. The
        // replay budget is generous so every replay reaches its own move loop
        // (deterministic: the live node already passed all pre-loop gates).
        Search::LimitsType savedLimits = liveWorker->research_limits();
        liveWorker->research_limits()  = Search::LimitsType{};
        liveWorker->research_limits().depth       = 4;
        liveWorker->research_limits().movetime    = 0;
        liveWorker->research_limits().infinite    = false;
        liveWorker->research_limits().nodes       = 0;
        liveWorker->research_limits().time[WHITE] = 0;
        liveWorker->research_limits().time[BLACK] = 0;

        Research::config().nodeBudget = 200000;

        // Entry-hook invocation (internal-counterfactual/3 signature): the
        // orchestrator samples the node at its search entry, replays it
        // (baseline, capture-armed), and writes the decision row with the
        // captured decision-point metadata and full candidate enumeration.
        Research::on_internal_node_counterfactual(*liveWorker, p10Pos, dummySS, -50, -49,
                                                  Depth(4), Depth(3), keyBefore, false);

        liveWorker->research_limits() = savedLimits;
        engine.get_threads().stop.store(savedStop, std::memory_order_relaxed);

        if (p10Pos.key() != keyBefore)
        {
            std::cerr << "sandbox_test 10.1 failed: hook mutated position key" << std::endl;
            return false;
        }
        if (liveWorker->get_nodes() != nodesBefore)
        {
            std::cerr << "sandbox_test 10.1 failed: hook mutated live worker nodes" << std::endl;
            return false;
        }
        if (std::memcmp(ttSnapshot.data(), baseTT.cluster_data(), baseTT.byte_size()) != 0)
        {
            std::cerr << "sandbox_test 10.1 failed: hook mutated base TT" << std::endl;
            return false;
        }

        // Reset sampler configuration, disarm the log (closes/flushes the
        // file), then read the row count for the schema validation below.
        Research::config().mode     = Research::Mode::Off;
        Research::config().switchOn = false;
        Research::internal_log().test_disarm_for_unit_tests();
        const u64 rows = Research::internal_log().rows_written();

        // 10.2: the decision-node replay pipeline wrote at least one schema-valid
        // decision row for this node (sample rate 1.0, node reached the loop).
        if (rows < 1)
        {
            std::cerr << "sandbox_test 10.2 failed: no decision row written (rows=" << rows
                      << ")" << std::endl;
            return false;
        }
        {
            // Read back and structurally validate every row as JSONL.
            std::FILE* f = std::fopen(datasetPath.c_str(), "rb");
            if (f == nullptr)
            {
                std::cerr << "sandbox_test 10.3 failed: cannot reopen dataset file" << std::endl;
                return false;
            }
            char buf[1 << 16];
            std::size_t n = 0, lineNo = 0;
            std::string content;
            while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0)
                content.append(buf, n);
            std::fclose(f);
            std::size_t pos = 0;
            while (pos < content.size())
            {
                const std::size_t eol = content.find('\n', pos);
                if (eol == std::string::npos)
                    break;
                const std::string line = content.substr(pos, eol - pos);
                pos = eol + 1;
                ++lineNo;
                if (line.find("\"schema\":\"internal-counterfactual/3\"") == std::string::npos
                    || line.find("\"type\":\"decision\"") == std::string::npos
                    || line.find("\"baseline\":{") == std::string::npos
                    || line.find("\"candidates\":[") == std::string::npos
                    || line.find("\"probes\":[") == std::string::npos
                    || line[0] != '{')
                {
                    std::cerr << "sandbox_test 10.3 failed: malformed decision row " << lineNo
                              << std::endl;
                    return false;
                }
            }
            if (lineNo != rows)
            {
                std::cerr << "sandbox_test 10.4 failed: row count mismatch (" << lineNo
                          << " lines vs " << rows << " rows)" << std::endl;
                return false;
            }
        }
        std::remove(datasetPath.c_str());
    }

    // Part 11: Force-Next Step Semantics (slot-1 swallow/count fidelity)
    {
        // The armed replay of a real decision node must swallow exactly the
        // natural-order emissions preceding the forced candidate: run the same
        // node twice with two different forced candidates whose ordinal
        // positions differ, and verify the arm-consumption marker plus that a
        // forced move that is ALREADY first in MovePicker order costs exactly
        // the same as an unforced replay (slot-1 treatment identity).
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p11Pos;
        p11Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                   &states->back());
        Move m = UCIEngine::to_move(p11Pos, "e2e4");
        states->emplace_back();
        p11Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply            = 1;
        dummySS->staticEval     = VALUE_NONE;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        // Direct force_next_step unit checks (no search):
        const Key  k  = p11Pos.key();
        Move       fA = UCIEngine::to_move(p11Pos, "c7c5");
        Move       fB = UCIEngine::to_move(p11Pos, "g8f6");
        {
            ForceNextScope scope(k, 1, fA);
            Move other = UCIEngine::to_move(p11Pos, "e7e5");
            if (force_next_step(k, 1, other) != 1)
            {
                std::cerr << "sandbox_test 11.1 failed: pre-forced emission not swallowed"
                          << std::endl;
                return false;
            }
            // The swallowed emission must be buffered, not dropped.
            if (gForceNext.prefixCount != 1 || gForceNext.prefix[0] != other
                || gForceNextMaxPrefixSeen != 1)
            {
                std::cerr << "sandbox_test 11.1b failed: pre-forced emission not buffered"
                          << std::endl;
                return false;
            }
            if (force_next_step(k, 1, fA) != 0 || gForceNext.armed || !gForceNext.consumed)
            {
                std::cerr << "sandbox_test 11.2 failed: forced emission did not consume the arm"
                          << std::endl;
                return false;
            }
            // While the arm is consumed the buffer pops in natural order.
            Move popped = Move::none();
            if (!force_next_buffer_pop(k, 1, popped) || popped != other)
            {
                std::cerr << "sandbox_test 11.2b failed: buffered prefix move not served after "
                             "arm consumption"
                          << std::endl;
                return false;
            }
            if (force_next_buffer_pop(k, 1, popped))
            {
                std::cerr << "sandbox_test 11.2c failed: buffer over-served" << std::endl;
                return false;
            }
            if (gForceNextBufferPops != 1)
            {
                std::cerr << "sandbox_test 11.2d failed: buffer pop counter wrong" << std::endl;
                return false;
            }
            if (force_next_step(k, 1, other) != 0)
            {
                std::cerr << "sandbox_test 11.3 failed: post-consumption emission swallowed"
                          << std::endl;
                return false;
            }
        }
        // Mismatched key/ply never arms.
        {
            ForceNextScope scope(k, 1, fB);
            if (force_next_step(k ^ 1, 1, fB) != 0 || force_next_step(k, 2, fB) != 0
                || gForceNext.armed != true)
            {
                std::cerr << "sandbox_test 11.4 failed: arm matched wrong node" << std::endl;
                return false;
            }
        }

        // Forced-natural-first replay equals unforced baseline (slot-1 identity).
        const auto& cands = enumerate_candidates(p11Pos, *liveWorker, dummySS, Move::none(),
                                                 Depth(3));
        if (cands.size() < 3)
        {
            std::cerr << "sandbox_test 11.5 failed: not enough candidates" << std::endl;
            return false;
        }
        CandidateProbeRunner rN(*liveWorker, p11Pos, dummySS);
        ProbeResult base   = rN.replay_node(-50, -49, Depth(3), false, 0);
        ProbeResult forced = rN.replay_node_forced(cands[0].move, -50, -49, Depth(3), false, 0);
        if (base.nodes != forced.nodes || base.score != forced.score)
        {
            std::cerr << "sandbox_test 11.6 failed: forcing the natural first move changed the "
                         "node outcome (nodes "
                      << base.nodes << " vs " << forced.nodes << ")" << std::endl;
            return false;
        }
    }

    // Part 12: Slot-1 Reorder (Prefix-Resume) Semantics -- regression for the
    // review finding that the old force-next deleted the natural-order prefix.
    // Scenario: a natural cutoff exists (baseline fail high) and a later
    // natural candidate FAILS LOW when forced into slot 1, so the node's loop
    // must continue instead of returning. Under genuine reorder semantics the
    // forced replay must then re-search the buffered natural prefix from the
    // loop head. What is asserted is the reorder MECHANISM: the k natural
    // predecessors were buffered (max prefix depth == k) and served back into
    // the search (pops in [1, k]). Scores and subtree costs of the re-searched
    // prefix moves are NOT asserted against the baseline: the prefix moves run
    // at their shifted slot numbers, so LMR and other moveCount-dependent
    // pruning legitimately change their outcome/cost (this is the engine's
    // own slot semantics -- a genuine reorder, not a re-run of the baseline).
    // Deletion-style force-next fails the mechanism checks (no buffer, no
    // pops).
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p12Pos;
        p12Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                   &states->back());
        Move m = UCIEngine::to_move(p12Pos, "e2e4");
        states->emplace_back();
        p12Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply            = 1;
        dummySS->staticEval     = VALUE_NONE;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        // Scan deterministic (depth, ordinal) configurations for the
        // scenario: a natural cutoff exists (baseline fail high) and the
        // forced candidate FAILS LOW in slot 1 (so the loop must continue and
        // drain the buffered prefix instead of returning). Budgets keep each
        // replay bounded; every replay here is fully isolated (fresh sync +
        // overlay), so the scan is deterministic and side-effect free on live
        // state.
        const Value beta12 = Value(-49);
        const auto  cands12 =
          enumerate_candidates(p12Pos, *liveWorker, dummySS, Move::none(), Depth(4));
        bool found = false;
        for (int d = 3; d <= 7 && !found; ++d)
        {
            CandidateProbeRunner rBase(*liveWorker, p12Pos, dummySS);
            const ProbeResult baseD = rBase.replay_node(-50, beta12, Depth(d), false, 200000);
            if (!baseD.completed || baseD.score < beta12)
                continue;  // no natural cutoff at this depth
            for (int k = 1; k < int(cands12.size()) && k <= 6 && !found; ++k)
            {
                CandidateProbeRunner rF(*liveWorker, p12Pos, dummySS);
                const ProbeResult forcedK =
                  rF.replay_node_forced(cands12[k].move, -50, beta12, Depth(d), false, 200000);
                if (!forcedK.completed || forcedK.score >= beta12 || !forcedK.forced_slot1)
                    continue;  // forced candidate did not fail low / not slot-1
                // Reorder regression assertions on this configuration. (The
                // re-searched prefix moves run at their shifted slot numbers,
                // so their scores and subtree costs legitimately differ from
                // the baseline's slot-1..k treatment -- LMR and related
                // moveCount-dependent pruning. What MUST hold is the reorder
                // mechanism itself: the k natural predecessors were buffered
                // (not deleted) and served back into the search after the
                // forced candidate failed low.)
                if (gForceNextMaxPrefixSeen != k)
                {
                    std::cerr
                      << "sandbox_test 12.1 failed: prefix buffer depth " << gForceNextMaxPrefixSeen
                      << " != forced ordinal " << k << " (natural prefix was dropped, not "
                         "buffered) at depth "
                      << d << std::endl;
                    return false;
                }
                if (gForceNextBufferPops < 1 || gForceNextBufferPops > k)
                {
                    std::cerr << "sandbox_test 12.2 failed: buffered prefix pops "
                              << gForceNextBufferPops << " outside [1, " << k
                              << "] (prefix not re-searched after forced fail-low) at depth " << d
                              << std::endl;
                    return false;
                }
                found = true;
            }
        }
        if (!found)
        {
            std::cerr << "sandbox_test 12.3 failed: no (depth, ordinal) configuration exhibited "
                         "baseline-cutoff + forced-fail-low"
                      << std::endl;
            return false;
        }
    }

    // Part 13: Shared-Permutation (top-K reorder) Semantics -- plan 11.3 /
    // schema internal-counterfactual/4. Direct protocol checks plus the two
    // bit-for-bit replay controls that the corpus also validates on every
    // row: the identity permutation [0..K-1] equals the unforced baseline
    // replay, and the permutation [k, 0..k-1, k+1..K-1] equals the ordinal-k
    // force-next replay (same machinery family, different arm path).
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p13Pos;
        p13Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false,
                   &states->back());
        Move m = UCIEngine::to_move(p13Pos, "e2e4");
        states->emplace_back();
        p13Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply            = 1;
        dummySS->staticEval     = VALUE_NONE;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory =
              &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }
        const auto& cands = enumerate_candidates(p13Pos, *liveWorker, dummySS, Move::none(),
                                                 Depth(4));
        if (cands.size() < 4)
        {
            std::cerr << "sandbox_test 13.0 failed: not enough candidates" << std::endl;
            return false;
        }
        const Key k13 = p13Pos.key();

        // 13.1 direct protocol: permutation [2, 0, 1] on the first three
        // candidates (moves), exercising buffered serves out of natural order.
        {
            Move t[3] = {cands[2].move, cands[0].move, cands[1].move};
            PermScope scope(k13, 1, t, 3);
            if (!gPerm.armed || gPerm.next != 0)
            {
                std::cerr << "sandbox_test 13.1 failed: arm not initialized" << std::endl;
                return false;
            }
            Move served = Move::none();
            if (perm_serve_buffered(k13, 1, served) || perm_serve_buffered(k13, 1, served))
            {
                std::cerr << "sandbox_test 13.2 failed: buffer served before any swallow"
                          << std::endl;
                return false;
            }
            if (perm_step(k13, 1, cands[0].move) != 1 || gPerm.bufCount != 1
                || gPerm.buf[0] != cands[0].move)
            {
                std::cerr << "sandbox_test 13.3 failed: natural ord0 not reserved"
                          << std::endl;
                return false;
            }
            if (perm_step(k13, 1, cands[1].move) != 1 || gPerm.bufCount != 2)
            {
                std::cerr << "sandbox_test 13.4 failed: natural ord1 not reserved"
                          << std::endl;
                return false;
            }
            if (perm_step(k13, 1, cands[2].move) != 0 || gPerm.next != 1
                || gPerm.served != 1)
            {
                std::cerr << "sandbox_test 13.5 failed: next target not served from picker"
                          << std::endl;
                return false;
            }
            if (!perm_serve_buffered(k13, 1, served) || served != cands[0].move
                || gPerm.next != 2 || gPerm.bufCount != 1 || gPerm.buf[0] != cands[1].move)
            {
                std::cerr << "sandbox_test 13.6 failed: buffered target not served next"
                          << std::endl;
                return false;
            }
            if (!perm_serve_buffered(k13, 1, served) || served != cands[1].move
                || gPerm.next != 3 || gPerm.bufCount != 0)
            {
                std::cerr << "sandbox_test 13.7 failed: second buffered target not served"
                          << std::endl;
                return false;
            }
            if (perm_active(k13, 1) || perm_step(k13, 1, cands[3].move) != 0)
            {
                std::cerr << "sandbox_test 13.8 failed: post-completion behavior wrong"
                          << std::endl;
                return false;
            }
            // Mismatched key/ply never arm.
            PermScope scope2(k13, 1, t, 3);
            if (perm_step(k13 ^ 1, 1, cands[2].move) != 0
                || perm_step(k13, 2, cands[2].move) != 0 || gPerm.served != 0
                || !gPerm.armed)
            {
                std::cerr << "sandbox_test 13.9 failed: arm matched wrong node" << std::endl;
                return false;
            }
        }

        // 13.10 identity permutation equals the baseline replay (bit-for-bit).
        {
            CandidateProbeRunner rId(*liveWorker, p13Pos, dummySS);
            const ProbeResult base = rId.replay_node(-50, -49, Depth(4), false, 200000);
            Move ident[3] = {cands[0].move, cands[1].move, cands[2].move};
            CandidateProbeRunner rP(*liveWorker, p13Pos, dummySS);
            const ProbeResult idp =
              rP.replay_node_perm(std::vector<Move>(ident, ident + 3), -50, -49, Depth(4),
                                  false, 200000);
            if (!base.completed || !idp.completed || base.nodes != idp.nodes
                || base.score != idp.score)
            {
                std::cerr << "sandbox_test 13.10 failed: identity permutation != baseline "
                          << "(nodes " << base.nodes << " vs " << idp.nodes << ")" << std::endl;
                return false;
            }
            if (idp.permK != 3 || idp.permServed < 1)
            {
                std::cerr << "sandbox_test 13.10b failed: perm metadata missing" << std::endl;
                return false;
            }
        }

        // 13.11 permutation [k, 0..k-1, k+1..] equals force-next ordinal k for
        // k = 1 and k = 2 (bit-for-bit).
        for (int k = 1; k <= 2; ++k)
        {
            CandidateProbeRunner rF(*liveWorker, p13Pos, dummySS);
            const ProbeResult fn =
              rF.replay_node_forced(cands[k].move, -50, -49, Depth(4), false, 200000);
            std::vector<Move> targets;
            targets.push_back(cands[k].move);
            for (int i = 0; i <= k; ++i)
                if (i != k)
                    targets.push_back(cands[i].move);
            targets.push_back(cands[3].move);  // K = 4 control window
            CandidateProbeRunner rP(*liveWorker, p13Pos, dummySS);
            const ProbeResult perm =
              rP.replay_node_perm(targets, -50, -49, Depth(4), false, 200000);
            if (!fn.completed || !perm.completed || fn.nodes != perm.nodes
                || fn.score != perm.score || fn.forced_slot1 != true || !perm.perm)
            {
                std::cerr << "sandbox_test 13.11 failed: perm [" << k
                          << ",0..] != force-next ordinal " << k << " (nodes " << fn.nodes
                          << " vs " << perm.nodes << ")" << std::endl;
                return false;
            }
        }
    }

    std::cout << "info string research: all sandbox unit tests passed successfully." << std::endl;
    return true;
}

CandidateProbeRunner::CandidateProbeRunner(const Search::Worker& liveWorker,
                                           const Position&       livePos,
                                           const Search::Stack*  liveSS,
                                           int                   windowBefore,
                                           int                   maxDepth) :
    liveWorker(liveWorker),
    livePos(livePos),
    liveSS(liveSS),
    windowBefore(windowBefore),
    maxDepth(maxDepth),
    isolatedWorker(liveWorker) {}

ProbeResult CandidateProbeRunner::replay_node(Value alpha, Value beta, Depth depth, bool cutNode,
                                              u64 nodeBudget) {
    return replay_impl(Move::none(), nullptr, alpha, beta, depth, cutNode, nodeBudget);
}

ProbeResult CandidateProbeRunner::replay_node_forced(Move forced, Value alpha, Value beta,
                                                     Depth depth, bool cutNode, u64 nodeBudget) {
    return replay_impl(forced, nullptr, alpha, beta, depth, cutNode, nodeBudget);
}

ProbeResult CandidateProbeRunner::replay_node_perm(const std::vector<Move>& permTargets,
                                                   Value alpha, Value beta, Depth depth,
                                                   bool cutNode, u64 nodeBudget) {
    return replay_impl(Move::none(), &permTargets, alpha, beta, depth, cutNode, nodeBudget);
}

ProbeResult CandidateProbeRunner::replay_impl(Move                  forced,
                                              const std::vector<Move>* permTargets,
                                              Value                  alpha,
                                              Value                  beta,
                                              Depth                  depth,
                                              bool                   cutNode,
                                              u64                    nodeBudget) {
    if (forced != Move::none() && !livePos.legal(forced))
        return ProbeResult{};  // invalid request; the sampler never sends one
    if (permTargets != nullptr && permTargets->size() >= 2)
    {
        for (Move m : *permTargets)
            if (!livePos.legal(m))
                return ProbeResult{};
    }

    // Full per-replay isolation: re-synchronized worker state (histories +
    // search context), fresh position clone, rebound stack window, and an
    // empty copy-on-first-access TT overlay over the base TT.
    isolatedWorker.sync_from(liveWorker);
    isolatedWorker.reset_stop();

    Position shadowPos;
    std::vector<StateInfo> shadowStates;
    livePos.clone_to(shadowPos, shadowStates);

    Search::Stack* shadowSS =
      isolatedWorker.clone_and_rebind_stack(liveWorker, liveSS, windowBefore, maxDepth);

    ResearchTTOverlay overlay(liveWorker.get_tt());

    // Measurement-A attribution channel (schema internal-counterfactual/3):
    // armed for every replay, baseline and forced, so both carry the slot-1
    // and final-cutoff attribution of their root node.
    AttributionScope attrScope(livePos.key(), liveSS->ply);

    ProbeResult result;
    if (permTargets != nullptr && permTargets->size() >= 2)
    {
        // Shared-permutation replay (plan 11.3): the targets are served as
        // this node's first K searched moves in the requested order. The
        // baseline capture stays disarmed (permutation rows need no decision
        // capture) and the force-next arm stays disarmed.
        PermScope scope(livePos.key(), liveSS->ply, permTargets->data(),
                        static_cast<int>(permTargets->size()));
        result = isolatedWorker.probe<NonPV>(shadowPos, shadowSS, alpha, beta, depth, cutNode,
                                             overlay, nodeBudget);
        result.perm          = true;
        result.permK         = static_cast<int>(permTargets->size());
        result.permServed    = gPerm.served;
        result.permComplete  = gPerm.complete;
    }
    else if (forced == Move::none())
    {
        // Baseline replay: arm the decision-point capture for this exact node
        // (position key, ply). The replay's own search fires
        // decision_capture_step() at its main move loop and the orchestrator
        // reads the result after this call returns.
        DecisionCaptureScope captureScope(livePos.key(), liveSS->ply);
        result = isolatedWorker.probe<NonPV>(shadowPos, shadowSS, alpha, beta, depth, cutNode,
                                             overlay, nodeBudget);
    }
    else
    {
        // Armed only for the duration of this synchronous replay; the live move
        // loop consumes it at the matching (position key, ply) node. The baseline
        // capture stays disarmed so forced replays never overwrite it.
        ForceNextScope scope(livePos.key(), liveSS->ply, forced);
        result = isolatedWorker.probe<NonPV>(shadowPos, shadowSS, alpha, beta, depth, cutNode,
                                             overlay, nodeBudget);
    }

    // Copy the attribution content before the scope teardown clears the TLS.
    result.attribution = gAttr;
    return result;
}

std::vector<CandidateFeature> enumerate_candidates(const Position&            pos,
                                                   const Search::Worker&      worker,
                                                   const Search::Stack*       ss,
                                                   Move                        ttMove,
                                                   Depth                       depth,
                                                   const PieceToHistory**      contHist) {
    // Drives the REAL MovePicker (main-search step-14 construction: live worker
    // history tables, the search's continuation-history window, the node's
    // search depth and ply, the node's TT move) to exhaustion and keeps every
    // legal emission in order. No duplicated ordering or scoring heuristics:
    // stageScore is read from the picker's own buffer right after each
    // select()-based emission (research accessor; TT emissions carry no score).
    std::vector<CandidateFeature> result;
    const Color us = pos.side_to_move();

    // Derive the continuation-history window exactly like the step-14 move
    // loop does when the caller did not pass its own copy (search.cpp always
    // passes its own; tests and the UCI debug command use nullptr).
    const PieceToHistory* window[6] = {};
    if (contHist == nullptr && ss != nullptr)
        for (int i = 0; i < 6; ++i)
            window[i] = (ss - (i + 1))->continuationHistory;
    if (contHist == nullptr)
        contHist = window;

    MovePicker mp(pos, ttMove, depth, &worker.mainHistory, &worker.lowPlyHistory,
                  &worker.captureHistory, contHist, &worker.sharedHistory, ss ? ss->ply : 0);

    u16  ordinal = 0;
    bool first   = true;
    for (Move m = mp.next_move(); m != Move::none(); m = mp.next_move())
    {
        const bool ttEmission = first && ttMove != Move::none() && m == ttMove
                             && pos.pseudo_legal(ttMove);
        first = false;

        // The search skips non-legal emissions before moveCount increments;
        // enumeration must mirror that exactly for ordinals to be
        // search-visible slots.
        if (!pos.legal(m))
            continue;

        CandidateFeature cf;
        cf.move      = m;
        cf.ordinal   = ordinal++;
        cf.isCheck   = pos.gives_check(m);
        cf.isCapture = pos.capture_stage(m);
        cf.isTTMove  = ttEmission;

        const Piece pc     = pos.moved_piece(m);
        const Square to    = m.to_sq();
        const Piece captured = pos.piece_on(to);

        cf.mainHist = int(worker.mainHistory[us][m.raw()]);
        if (cf.isCapture)
            cf.captureHist = int(worker.captureHistory[pc][to][type_of(captured)]);
        else
        {
            // Quiet-side features (mirror the picker's own scoring inputs; the
            // exact composite lives in stageScore).
            cf.pawnHist = int(worker.sharedHistory.pawn_entry(pos)[pc][to]);
            cf.contHist = 0;
            for (int i : {0, 1, 2, 3, 5})
                if (contHist[i] != nullptr)
                    cf.contHist += int((*contHist[i])[pc][to]);
            const int ply = ss ? ss->ply : 0;
            if (ply < LOW_PLY_HISTORY_SIZE)
                cf.lowPlyHist = int(worker.lowPlyHistory[ply][m.raw()]);
        }
        cf.seeScore = pos.see_ge(m, 0) ? 1 : (pos.see_ge(m, -100) ? 0 : -1);

        if (ttEmission)
            cf.stage = CandidateStage::TT;  // no picker sort value for TT
        else
        {
            // select()-based emission: the emitted element stays at (cur - 1)
            // until the next call, so the picker's own sort value is readable
            // here (movepick.h research accessor).
            cf.stageScore = mp.research_emitted_score();
            if (cf.isCapture)
                // The picker's good/bad capture split is the same SEE test on
                // the same score; re-apply it for classification.
                cf.stage = pos.see_ge(m, -cf.stageScore / 18)
                             ? CandidateStage::GoodCapture
                             : CandidateStage::BadCapture;
            else
                // goodQuietThreshold (-14000) split, exact.
                cf.stage = cf.stageScore > -14000 ? CandidateStage::GoodQuiet
                                                  : CandidateStage::BadQuiet;
        }

        result.push_back(cf);
    }
    return result;
}

namespace {

// ---------------------------------------------------------------------------
// JSON value formatting helpers for internal-counterfactual/3 decision rows.
// Row STRUCTURE is declared here and in data-schema.md; the file/root/run
// wrapper rows and caps live in InternalDatasetLog (research_log.cpp).
// ---------------------------------------------------------------------------

inline std::string jstr(const std::string& s) {
    std::string o = "\"";
    for (char c : s)
    {
        if (c == '"' || c == '\\')
            o += '\\';
        if (c == '\n')
            o += "\\n";
        else if (c == '\r')
            o += "\\r";
        else
            o += c;
    }
    o += '"';
    return o;
}

inline std::string jnum(i64 v) {
    return std::to_string(v);
}

inline std::string jnum(u64 v) {
    return std::to_string(v);
}

inline std::string jbool(bool b) {
    return b ? "true" : "false";
}

inline std::string jdouble(double v) {
    // Fixed 6 decimals (deterministic, matches run_start formatting).
    std::ostringstream os;
    os.setf(std::ios::fixed);
    os.precision(6);
    os << v;
    return os.str();
}

// Nullable int: censored probes carry no numeric value, ever.
inline std::string jvalue_or_null(Value v, bool valid) {
    return valid ? jnum(i64(int(v))) : std::string("null");
}

inline const char* stop_name(ProbeResult::StopReason r) {
    switch (r)
    {
        case ProbeResult::StopReason::None:     return "none";
        case ProbeResult::StopReason::Budget:   return "budget";
        case ProbeResult::StopReason::UserStop: return "user";
    }
    return "?";
}

inline void ds_mix_step(u64& x) {  // same constants as the run/root rows use
    x ^= x >> 30;
    x *= 0xBF58476D1CE4E5B9ULL;
    x ^= x >> 27;
    x *= 0x94D049BB133111EBULL;
    x ^= x >> 31;
}

// Deterministic sub-hash for the uniform hash-sample over the candidate rest.
inline u64 ds_subhash(u64 h, int ordinal) {
    u64 x = h ^ 0x9E3779B97F4A7C15ULL;
    x += u64(ordinal) * 0xD6E8FEB86659FD93ULL;
    ds_mix_step(x);
    return x;
}

// One selected probe: candidate index in MovePicker order + selection prob.
struct SelectedProbe {
    int    idx = 0;
    double prob = 0.0;
};

}  // namespace

const char* candidate_stage_name(CandidateStage s) {
    switch (s)
    {
        case CandidateStage::TT:          return "tt";
        case CandidateStage::GoodCapture: return "good_capture";
        case CandidateStage::BadCapture:  return "bad_capture";
        case CandidateStage::GoodQuiet:   return "good_quiet";
        case CandidateStage::BadQuiet:    return "bad_quiet";
    }
    return "?";
}

// decision_capture_step: fired by the main move loop of every search node
// (search.cpp moves_loop, POLICY_RESEARCH builds). No-ops unless an isolated
// BASELINE replay of a sampled node is armed for this exact (posKey, ply) --
// in which case this IS the replay's own decision point, reached after the
// full deterministic pre-loop work (TT probe, static eval, history update,
// razor/futility/null-move/ProbCut gates, small ProbCut). Records the
// decision-point metadata and the full legal candidate enumeration from the
// replay's OWN freshly constructed decision state -- the state the slot-1
// forced replays (identical entry state, identical deterministic pre-loop)
// and the live node operate on -- plus the shadow do_moves spent before it.
void decision_capture_step(Search::Worker& worker, Position& pos, Search::Stack* ss,
                           Key posKey, int ply, bool ttHit, Move ttMove, bool improving,
                           Value staticEval, int decisionDepth) {
    auto& c = gDecisionCapture;
    if (!c.armed || c.fired || c.posKey != posKey || c.ply != ply
        || c.frameToken != gResearchCurFrame)
        return;
    c.fired              = true;
    c.ttHit              = ttHit;
    c.improving          = improving;
    c.staticEval         = staticEval;
    c.ttMove             = ttMove;
    c.decisionDepth      = decisionDepth;
    c.nextPlyCutoffCnt   = (ss + 1)->cutoffCnt;
    const u64 shadowNow  = worker.get_nodes();
    c.decisionPointNodes = shadowNow >= gProbeStartNodes ? shadowNow - gProbeStartNodes : 0;
    c.candidates =
      enumerate_candidates(pos, worker, ss, ttMove, Depth(decisionDepth));
}

LiveExitScope::LiveExitScope(const Search::Worker& worker, const Position& pos,
                             const Search::Stack* ss) :
    worker_(&worker),
    pos_(&pos),
    ss_(ss) {}

LiveExitScope::~LiveExitScope() {
    // Shadow frames never push oracle entries (sampling is suppressed inside
    // probes), so the innermost tracked entry can only belong to a live frame.
    if (Research::is_shadow_probe_active())
        return;
    LiveOracleEntry e;
    if (!live_oracle_pop(pos_->key(), ss_->ply, worker_->get_nodes(), &e))
        return;  // this frame is not the innermost tracked sampled node
    if (Research::internal_log().active())
    {
        std::string row;
        row += std::string("{\"schema\":\"") + internal_dataset_schema()
          + "\",\"type\":\"node_exit\"";
        row += ",\"root_key\":" + jnum(u64(e.rootKey));
        row += ",\"pos_key\":" + jnum(u64(e.posKey));
        row += ",\"ply\":" + jnum(i64(e.ply));
        row += ",\"entry_depth\":" + jnum(i64(e.entryDepth));
        row += ",\"sample_seed\":" + jnum(u64(e.sampleSeed));
        row += ",\"sample_id\":" + jnum(u64(e.sampleId));
        row += ",\"live_subtree_nodes\":" + jnum(u64(e.liveNodes));
        row += "}";
        // Audit row: never consumes the decision-row cap; suppressed only
        // when collection already ended (overflow/io failure).
        Research::internal_log().write_row(row, false);
    }

    // R2 exit-replay diagnostic (bit 1 of PolicyResearchDiagMode): re-run one
    // fresh baseline replay from the frame-exit state (fresh sync, clone, and
    // overlay over the CURRENT base TT) using the entry window captured by
    // the hook. Comparing (hook-time baseline nodes, live subtree nodes,
    // exit-time replay nodes) tells us whether the replay-vs-live node-count
    // mismatches are explained by state the live node's own subtree search
    // changed before it returned (exit replay should then track the live
    // count), by TT content differences at node entry, or by an oracle
    // measurement boundary artifact (exit replay should track the hook-time
    // baseline instead). Audit rows: never consume the decision-row cap.
    if (Research::config().diagMode & 2)
    {
        CandidateProbeRunner runner(*worker_, *pos_, ss_);
        const u64 budget = Research::effective_node_budget(Research::config());
        const ProbeResult exitBase =
          runner.replay_node(e.alpha, e.beta, Depth(e.entryDepth), e.cutNode, budget);
        const DecisionCapture& ec = gDecisionCapture;  // content survives scope teardown
        std::string row;
        row += "{\"schema\":\"internal-counterfactual-diag/1\",\"type\":\"diag_exit_replay\"";
        row += ",\"root_key\":" + jnum(u64(e.rootKey));
        row += ",\"pos_key\":" + jnum(u64(e.posKey));
        row += ",\"ply\":" + jnum(i64(e.ply));
        row += ",\"entry_depth\":" + jnum(i64(e.entryDepth));
        row += ",\"sample_id\":" + jnum(u64(e.sampleId));
        row += ",\"baseline_nodes\":" + jnum(u64(e.hookBaselineNodes));
        row += ",\"live_nodes\":" + jnum(u64(e.liveNodes));
        row += ",\"exit_replay_nodes\":" + jnum(u64(exitBase.nodes));
        row += ",\"exit_completed\":" + jbool(exitBase.completed);
        row += ",\"exit_value\":" + jvalue_or_null(exitBase.score, exitBase.completed);
        row += ",\"exit_tt_hit\":" + jbool(ec.fired && ec.ttHit);
        if (ec.fired && ec.ttMove != Move::none())
            row += ",\"exit_tt_move\":" + jstr(UCIEngine::move(ec.ttMove, pos_->is_chess960()));
        else
            row += ",\"exit_tt_move\":null";
        row += ",\"exit_decision_point_nodes\":" +
               jnum(u64(ec.fired ? ec.decisionPointNodes : 0));
        row += "}";
        Research::internal_log().write_row(row, false);
    }
}

// R2 diagnostic (bit 0 of PolicyResearchDiagMode): see worker_snapshot.h.
// No-ops unless the probed live frame is exactly the innermost tracked
// sampled node (its own post-hook Step-4 TT probe); shadow frames never match
// (frame tokens differ from the stored live token), so this dumps live state
// only. Rows join decision rows by sample_id.
void live_oracle_tt_probe_diag(Key posKey, int ply, bool ttHit, Move ttMove, int ttDepth,
                               int ttBound, int ttValue, int ttEval, bool chess960,
                               int rule50, int ss1CutoffCnt) {
    if (!(Research::config().diagMode & 1) || gLiveOracleCount <= 0
        || !Research::internal_log().active())
        return;
    LiveOracleEntry& top = gLiveOracle[gLiveOracleCount - 1];
    if (top.posKey != posKey || top.ply != ply || top.frameToken != gResearchCurFrame)
        return;
    std::string row;
    row += "{\"schema\":\"internal-counterfactual-diag/1\",\"type\":\"diag_live_tt\"";
    row += ",\"root_key\":" + jnum(u64(top.rootKey));
    row += ",\"sample_id\":" + jnum(u64(top.sampleId));
    row += ",\"ss1_cutoff_cnt\":" + jnum(i64(ss1CutoffCnt));
    row += ",\"tt_hit\":" + jbool(ttHit);
    if (ttMove != Move::none())
        row += ",\"tt_move\":" + jstr(UCIEngine::move(ttMove, chess960));
    else
        row += ",\"tt_move\":null";
    row += ",\"tt_depth\":" + jnum(i64(ttDepth));
    row += ",\"tt_bound\":" + jnum(i64(ttBound));
    row += ",\"tt_value\":" + jnum(i64(ttValue));
    row += ",\"tt_eval\":" + jnum(i64(ttEval));
    row += ",\"rule50\":" + jnum(i64(rule50));
    row += "}";
    Research::internal_log().write_row(row, false);
}

// Measurement-A serialization helpers (schema internal-counterfactual/3).
// attr_json: the slot-1 (first emitted) move attribution. attr_cutoff_json:
// the final move-loop cutoff attribution, with the cutoff move's natural
// ordinal resolved against the row's enumerated candidate list (a cutoff
// move is always a legal emission of the same node, hence always found).
inline std::string attr_json(const MoveAttribution& a, bool c960) {
    std::string s;
    s += "{\"emitted\":" + jbool(a.firstEmitted);
    s += ",\"move\":" + (a.firstMove != Move::none()
                             ? jstr(UCIEngine::move(a.firstMove, c960))
                             : std::string("null"));
    s += ",\"searched\":" + jbool(a.firstSearched);
    if (a.firstSearched)
    {
        s += ",\"child_nodes\":" + jnum(u64(a.firstChildNodes));
        s += ",\"value\":" + jvalue_or_null(a.firstValue, true);
    }
    else
        s += ",\"child_nodes\":null,\"value\":null";
    s += "}";
    return s;
}

inline std::string attr_cutoff_json(const MoveAttribution&  a,
                                    bool                     c960,
                                    const std::vector<CandidateFeature>& candidates) {
    std::string s;
    int ordinal = -1;
    if (a.cutoffSeen && a.cutoffMove != Move::none())
        for (usize i = 0; i < candidates.size(); ++i)
            if (candidates[i].move == a.cutoffMove)
            {
                ordinal = candidates[i].ordinal;
                break;
            }
    s += "{\"cutoff_seen\":" + jbool(a.cutoffSeen);
    s += ",\"cutoff_move\":" + (a.cutoffSeen && a.cutoffMove != Move::none()
                                    ? jstr(UCIEngine::move(a.cutoffMove, c960))
                                    : std::string("null"));
    s += ",\"cutoff_ordinal\":" +
         (a.cutoffSeen && ordinal >= 0 ? jnum(i64(ordinal)) : std::string("null"));
    if (a.cutoffSeen)
        s += ",\"cutoff_value\":" + jvalue_or_null(a.cutoffValue, true);
    else
        s += ",\"cutoff_value\":null";
    s += ",\"cutoff_by_first\":" + jbool(a.cutoffByFirst);
    s += "}";
    return s;
}

// Per-slot records of a permutation replay's root move loop (schema
// internal-counterfactual/4): for every emission at slot 1..MAX_SLOTS, the
// move, its natural ordinal, whether it was searched (false = pruned, or the
// node ended inside its singular probe), and when searched the child subtree
// do_moves and the parent-relative returned value.
inline std::string attr_slots_json(const MoveAttribution&             a,
                                   bool                              c960,
                                   const std::vector<CandidateFeature>& candidates) {
    std::string s = "[";
    for (int slot = 1; slot <= a.slotsUsed && slot <= MoveAttribution::MAX_SLOTS; ++slot)
    {
        const auto& sl = a.slots[slot];
        if (slot > 1)
            s += ',';
        s += "{\"slot\":" + jnum(i64(slot));
        s += ",\"move\":" + (sl.emitted && sl.move != Move::none()
                                    ? jstr(UCIEngine::move(sl.move, c960))
                                    : std::string("null"));
        int ordinal = -1;
        if (sl.move != Move::none())
            for (usize i = 0; i < candidates.size(); ++i)
                if (candidates[i].move == sl.move)
                {
                    ordinal = candidates[i].ordinal;
                    break;
                }
        s += ",\"ordinal\":" + (ordinal >= 0 ? jnum(i64(ordinal)) : std::string("null"));
        s += ",\"searched\":" + jbool(sl.emitted && sl.searched);
        if (sl.emitted && sl.searched)
        {
            s += ",\"child_nodes\":" + jnum(u64(sl.childNodes));
            s += ",\"value\":" + jvalue_or_null(sl.value, true);
        }
        else
            s += ",\"child_nodes\":null,\"value\":null";
        s += "}";
    }
    s += "]";
    return s;
}

void on_internal_node_counterfactual(Search::Worker& liveWorker,
                                     Position&       pos,
                                     Search::Stack*  ss,
                                     Value           alpha,
                                     Value           beta,
                                     Depth           depth,
                                     Depth           rootDepth,
                                     Key             rootKey,
                                     bool            cutNode) {
    // Gate chain, cheapest first. All live state must be untouched on return
    // (asserted at the end); every probe below runs on fully isolated shadow
    // state (CandidateProbeRunner), never on the live position.
    if (!Research::enabled() || Research::config().mode != Research::Mode::InternalCounterfactual
        || !liveWorker.is_mainthread() || Research::is_shadow_probe_active())
        return;

    // Dataset armed and caps allow another row. When the row cap is already
    // reached the sample is skipped cheaply (no replay) but the skip is
    // reported so root_end's overflow flag says the dataset is truncated.
    if (!Research::internal_log().active())
        return;
    if (!Research::internal_log().would_record())
    {
        Research::internal_log().note_row_skipped();
        return;
    }

    // Decision nodes: non-PV null-window (PvNode || alpha == beta - 1 in the
    // search, so this is exactly NonPV's invariant), not in check, no
    // excludedMove (singular-extension re-searches are excluded), positive
    // rootDepth (the search-context fallback contract, worker_snapshot.h).
    if (alpha + 1 != beta || rootDepth <= 0)
        return;
    if (ss->inCheck || ss->excludedMove != Move::none())
        return;

    // Defense in depth (the UCI site already refused unsafe configurations
    // before arming): internal collection only under the one-thread,
    // fixed-depth / fixed-node, offline-style limits.
    if (!IsolatedWorker::is_safe_environment(liveWorker))
        return;
    const auto& lim = liveWorker.research_limits();
    if (!(lim.depth > 0 || lim.nodes > 0) || lim.movetime || lim.infinite
        || lim.time[WHITE] > 0 || lim.time[BLACK] > 0)
        return;
    if (liveWorker.get_threads().stop.load(std::memory_order_relaxed))
        return;

    const Key  liveKeyBefore   = pos.key();
    const u64  liveNodesBefore = liveWorker.get_nodes();
    const bool liveStopBefore  = liveWorker.get_threads().stop.load(std::memory_order_relaxed);
    (void) liveKeyBefore;
    (void) liveNodesBefore;
    (void) liveStopBefore;

    const auto liveStopped = [&liveWorker]() {
        return liveWorker.get_threads().stop.load(std::memory_order_relaxed);
    };

    // Deterministic node sample (pure function of seed + entry keys;
    // data-schema.md). The entry depth is mixed in (the decision-point depth
    // after IIR is recorded from the baseline replay's decision capture).
    // Row caps gate above; sampling stops on live stop.
    u64 h = Research::config().seed;
    h += 0x9E3779B97F4A7C15ULL ^ rootKey;
    ds_mix_step(h);
    h ^= pos.key();
    h += u64(ss->ply) | (u64(int(depth)) << 16) | (u64(int(rootDepth)) << 32);
    ds_mix_step(h);
    const u32 sampleThreshold =
      static_cast<u32>(std::clamp(Research::config().sampleRate, 0.0, 1.0) * 1000000.0 + 0.5);
    if ((h % 1000000ULL) >= sampleThreshold)
        return;

    CandidateProbeRunner runner(liveWorker, pos, ss);
    const u64 budget = Research::effective_node_budget(Research::config());  // always finite

    // Baseline (unforced) whole-node replay first. It is armed with the
    // decision-point capture: if the replay reaches its main move loop (the
    // node truly acts as a decision node from this entry state), the capture
    // records the decision metadata and the full legal candidate enumeration
    // at the replay's own decision point. If the replay returns before the
    // loop (deterministic pre-loop cutoff, which the live node reproduces),
    // the capture never fires and the sample is dropped -- no decision row.
    const ProbeResult baseline = runner.replay_node(alpha, beta, depth, cutNode, budget);
    if (liveStopped())
        return;
    const DecisionCapture& cap = gDecisionCapture;
    if (!cap.fired)
        return;
    // Copy the captured enumeration: selection marks are row-local state and
    // must not leak into the TLS capture (rows are per-sample).
    auto candidates = cap.candidates;
    const int N     = static_cast<int>(candidates.size());
    if (N < 2)
        return;

    // Selection (plan 10.5): 2..8 legal candidates -> probe all (prob 1.0);
    // more -> top-K (K = min(topK option or 4, N)) at prob 1.0 plus 2
    // deterministic hash-sampled from the rest at marginal prob 2/(N-K).
    // Every candidate gets its marginal inclusion probability (1.0 for the
    // probe-all and top-K sets, 2/(N-K) for each member of the hash-sampled
    // rest -- including the members that were not drawn).
    std::vector<SelectedProbe> sel;
    std::string                rule = "probe_all";
    if (N <= 8)
    {
        for (int i = 0; i < N; ++i)
            sel.push_back({i, 1.0});
        for (auto& cf : candidates)
            cf.selectionProb = 1.0;  // marginal inclusion probability
    }
    else
    {
        rule = "topK_plus_hash_sample";
        const int top =
          std::clamp(Research::config().topK > 0 ? Research::config().topK : 4, 1, N - 1);
        for (int i = 0; i < top; ++i)
            sel.push_back({i, 1.0});
        const int    rest     = N - top;
        const double marginal = (rest == 1) ? 1.0 : (2.0 / rest);
        int          b1 = -1, b2 = -1;
        if (rest == 1)
            b1 = top;
        else
        {
            u64 low1 = ~0ULL, low2 = ~0ULL;
            for (int i = top; i < N; ++i)
            {
                const u64 sh = ds_subhash(h, i);
                if (sh < low1)
                {
                    low2 = low1;
                    b2   = b1;
                    low1 = sh;
                    b1   = i;
                }
                else if (sh < low2)
                {
                    low2 = sh;
                    b2   = i;
                }
            }
        }
        if (rest == 1)
            sel.push_back({b1, marginal});
        else
        {
            sel.push_back({b1, marginal});
            sel.push_back({b2, marginal});
        }
        for (int i = 0; i < N; ++i)
        {
            auto& cf       = candidates[i];
            cf.selectionProb = (i < top) ? 1.0 : marginal;
        }
    }
    for (const auto& s : sel)
        candidates[s.idx].selected = true;

    // One forced (slot-1 reorder) whole-node replay per selected candidate.
    // Every replay is fully isolated (fresh sync, clone, stack rebind, empty
    // TT overlay per candidate). Live stop is honored between probes; an
    // abort drops the row (no partial rows). gForceNextBufferPops is reset by
    // each arm (ForceNextScope), so reading it right after each replay yields
    // that replay's prefix-pop count (prefix moves re-searched after the
    // forced candidate failed low).
    std::vector<ProbeResult> forced;
    std::vector<int>         forcedPops;
    forced.reserve(sel.size());
    forcedPops.reserve(sel.size());
    for (const auto& s : sel)
    {
        forced.push_back(
          runner.replay_node_forced(candidates[s.idx].move, alpha, beta, depth, cutNode, budget));
        forcedPops.push_back(gForceNextBufferPops);
        if (liveStopped())
            return;
    }

    // Degenerate-node rule (documented in data-schema.md): a decision row is
    // only meaningful when at least one forced replay actually reached the
    // node's main move loop and consumed the force-next arm. The baseline
    // capture fired, so every forced replay reaches the same decision point
    // unless a live stop intervened (handled above); the check stays as
    // defense in depth.
    bool anyDecision = false;
    for (const auto& r : forced)
        anyDecision |= r.forced_slot1;
    if (!anyDecision)
        return;

    // Plan-11.3 shared-permutation battery (schema internal-counterfactual/4,
    // PolicyResearchPermBattery): re-run the node once per permutation of its
    // natural top-K emissions (K = min(4, N); K >= 2), so each order's cost is
    // measured with TT/history/cutoff context shared across the K candidates
    // inside one continuing replay. The battery is fixed and row-identical:
    // identity [0..K-1] (must equal the baseline), the force-next controls
    // [k, 0..k-1, k+1..K-1] for k = 1..min(3, K-1) (must equal the ordinal-k
    // scalar probe), reverse, one-step rotation, adjacent swaps, and the
    // ex-post cheapest-first order derived from the scalar probe costs above
    // (in-row, so it is always constructible). Duplicates are run once. When
    // any of the top-K scalar probes is censored the battery is skipped for
    // the row (the row is still emitted with an empty permutations array).
    std::vector<std::vector<int>> permOrders;
    std::vector<ProbeResult>      permRes;
    const bool permMode = Research::config().permBattery;
    if (permMode && N >= 2 && !liveStopped())
    {
        const int K = std::min(4, N);
        std::vector<u64> scalarCost(K, 0);
        bool haveCosts = true;
        for (int o = 0; o < K && haveCosts; ++o)
        {
            if (o == 0)
                scalarCost[o] = baseline.nodes;
            else
            {
                bool found = false;
                for (usize si = 0; si < sel.size(); ++si)
                    if (sel[si].idx >= 0 && candidates[sel[si].idx].ordinal == o)
                    {
                        found         = true;
                        haveCosts     = forced[si].completed;
                        scalarCost[o] = forced[si].nodes;
                        break;
                    }
                if (!found)
                    haveCosts = false;
            }
        }
        if (haveCosts && K >= 2)
        {
            auto addOrder = [&permOrders](const std::vector<int>& ords) {
                if (int(ords.size()) < 2)
                    return;
                for (const auto& e : permOrders)
                    if (e == ords)
                        return;
                permOrders.push_back(ords);
            };
            std::vector<int> ident(K);
            for (int i = 0; i < K; ++i)
                ident[i] = i;
            addOrder(ident);  // identity control: must equal the baseline
            for (int k = 1; k <= std::min(3, K - 1); ++k)
            {
                // force-next control [k, 0..k-1, k+1..K-1]
                std::vector<int> ord;
                ord.push_back(k);
                for (int i = 0; i < K; ++i)
                    if (i != k)
                        ord.push_back(i);
                addOrder(ord);
            }
            std::vector<int> rev(ident.rbegin(), ident.rend());
            addOrder(rev);
            std::vector<int> rot;
            for (int i = 1; i < K; ++i)
                rot.push_back(i);
            rot.push_back(0);
            addOrder(rot);
            for (int i = 1; i + 1 < K && i <= 2; ++i)  // adjacent swaps (1,2), (2,3)
            {
                std::vector<int> ord = ident;
                std::swap(ord[i], ord[i + 1]);
                addOrder(ord);
            }
            std::vector<int> cheapest(ident);
            std::stable_sort(cheapest.begin(), cheapest.end(),
                             [&scalarCost](int a, int b) {
                                 return scalarCost[a] < scalarCost[b];
                             });
            addOrder(cheapest);  // ex-post cheapest-first (from the scalar probes)

            permRes.reserve(permOrders.size());
            for (const auto& ord : permOrders)
            {
                std::vector<Move> targets;
                targets.reserve(ord.size());
                for (int o : ord)
                    targets.push_back(candidates[o].move);
                permRes.push_back(
                  runner.replay_node_perm(targets, alpha, beta, depth, cutNode, budget));
                if (liveStopped())
                    return;
            }
        }
    }

    // ---- assemble and write the decision row ----
    // Unique per-visit sample id (monotonic within the run; allocated even if
    // the row is later dropped by a cap race, so ids never collide across the
    // decision and node_exit rows of one visit).
    const u64 sampleId = Research::internal_log().next_sample_id();
    const bool c960 = pos.is_chess960();
    const std::string schemaStr =
      permMode ? "internal-counterfactual/4" : "internal-counterfactual/3";
    std::string row;
    row += "{\"schema\":\"" + schemaStr + "\",\"type\":\"decision\"";
    row += ",\"root_key\":" + jnum(u64(rootKey));
    row += ",\"pos_key\":" + jnum(u64(pos.key()));
    row += ",\"fen\":" + jstr(pos.fen());
    row += ",\"ply\":" + jnum(i64(ss->ply));
    row += ",\"entry_depth\":" + jnum(i64(int(depth)));
    row += ",\"depth\":" + jnum(i64(cap.decisionDepth));
    row += ",\"root_depth\":" + jnum(i64(int(rootDepth)));
    row += ",\"alpha\":" + jnum(i64(int(alpha)));
    row += ",\"beta\":" + jnum(i64(int(beta)));
    row += ",\"static_eval\":" + jvalue_or_null(cap.staticEval, cap.staticEval != VALUE_NONE);
    row += ",\"improving\":" + jbool(cap.improving);
    row += ",\"tt_hit\":" + jbool(cap.ttHit);
    if (cap.ttMove != Move::none())
        row += ",\"tt_move\":" + jstr(UCIEngine::move(cap.ttMove, c960));
    else
        row += ",\"tt_move\":null";
    row += ",\"cut_node\":" + jbool(cutNode);
    row += ",\"rule50\":" + jnum(i64(pos.state()->rule50));
    row += ",\"fullmove\":" + jnum(i64(pos.game_ply() / 2 + 1));
    row += ",\"sample_seed\":" + jnum(u64(h));
    row += ",\"sample_id\":" + jnum(sampleId);
    row += ",\"sample_rate\":" + jdouble(Research::config().sampleRate);
    row += ",\"selection\":" + jstr(rule);
    row += ",\"n_candidates\":" + jnum(i64(N));
    row += ",\"node_budget\":" + jnum(u64(budget));

    // baseline replay (unforced; the natural-order outcome of the same node,
    // measured from its true entry state -- node count includes the replayed
    // pre-loop work; decision_point_nodes splits off the pre-loop cost).
    row += ",\"baseline\":{\"nodes\":" + jnum(u64(baseline.nodes));
    row += ",\"decision_point_nodes\":" + jnum(u64(cap.decisionPointNodes));
    row += ",\"ss1_cutoff_cnt\":" + jnum(i64(cap.nextPlyCutoffCnt));
    row += ",\"completed\":" + jbool(baseline.completed);
    row += ",\"stop\":" + jstr(stop_name(baseline.stopReason));
    row += ",\"budget_hit\":" + jbool(baseline.hit_budget);
    row += ",\"value\":" + jvalue_or_null(baseline.score, baseline.completed);
    row += ",\"fail_high\":" + (baseline.completed
                                      ? jbool(baseline.score >= beta)
                                      : std::string("null"));
    row += ",\"first\":" + attr_json(baseline.attribution, c960);
    row += ",\"cutoff\":" + attr_cutoff_json(baseline.attribution, c960, candidates);
    row += "}";

    row += ",\"candidates\":[";
    for (int i = 0; i < N; ++i)
    {
        const auto& cf = candidates[i];
        if (i > 0)
            row += ',';
        row += "{\"move\":" + jstr(UCIEngine::move(cf.move, c960));
        row += ",\"ordinal\":" + jnum(i64(cf.ordinal));
        row += ",\"stage\":" + jstr(candidate_stage_name(cf.stage));
        if (cf.stage == CandidateStage::TT)
            row += ",\"stage_score\":null";  // TT emissions carry no picker sort value
        else
            row += ",\"stage_score\":" + jnum(i64(cf.stageScore));
        row += ",\"main_hist\":" + jnum(i64(cf.mainHist));
        if (cf.isCapture)
            row += ",\"capture_hist\":" + jnum(i64(cf.captureHist));
        else
            row += ",\"capture_hist\":null";
        if (!cf.isCapture)
        {
            row += ",\"pawn_hist\":" + jnum(i64(cf.pawnHist));
            row += ",\"cont_hist\":" + jnum(i64(cf.contHist));
        }
        else
        {
            row += ",\"pawn_hist\":null";
            row += ",\"cont_hist\":null";
        }
        if (!cf.isCapture && int(ss->ply) < LOW_PLY_HISTORY_SIZE)
            row += ",\"low_ply_hist\":" + jnum(i64(cf.lowPlyHist));
        else
            row += ",\"low_ply_hist\":null";
        row += ",\"see_bucket\":" + jnum(i64(cf.seeScore));
        row += ",\"check\":" + jbool(cf.isCheck);
        row += ",\"capture\":" + jbool(cf.isCapture);
        row += ",\"tt_move\":" + jbool(cf.isTTMove);
        row += ",\"prob\":" + jdouble(cf.selectionProb);
        row += ",\"selected\":" + jbool(cf.selected);
        row += "}";
    }
    row += "]";

    row += ",\"probes\":[";
    for (usize k = 0; k < sel.size(); ++k)
    {
        const auto& pr = forced[k];
        if (k > 0)
            row += ',';
        row += "{\"move\":" + jstr(UCIEngine::move(sel[k].idx >= 0 ? candidates[sel[k].idx].move
                                                                   : Move::none(),
                                                    c960));
        row += ",\"prob\":" + jdouble(sel[k].prob);
        row += ",\"nodes\":" + jnum(u64(pr.nodes));
        row += ",\"completed\":" + jbool(pr.completed);
        row += ",\"stop\":" + jstr(stop_name(pr.stopReason));
        row += ",\"budget_hit\":" + jbool(pr.hit_budget);
        row += ",\"value\":" + jvalue_or_null(pr.score, pr.completed);
        row += ",\"forced_slot1\":" + jbool(pr.forced_slot1);
        row += ",\"fail_high\":" + (pr.completed ? jbool(pr.score >= beta)
                                                   : std::string("null"));
        row += ",\"first\":" + attr_json(pr.attribution, c960);
        row += ",\"cutoff\":" + attr_cutoff_json(pr.attribution, c960, candidates);
        row += ",\"prefix_pops\":" + jnum(i64(k < forcedPops.size() ? forcedPops[k] : 0));
        row += "}";
    }
    row += "]";

    // Shared-permutation battery (schema internal-counterfactual/4): every
    // run carries nodes/completed/stop/budget/value/fail_high, the slot-1 and
    // final-cutoff attribution (same shape as probes), the per-slot records
    // of the served prefix (searched/pruned state and child cost per slot),
    // and the served/complete bookkeeping. Present only in /4 rows; /3 rows
    // keep their exact historical layout.
    if (permMode)
    {
        row += ",\"permutations\":[";
        for (usize k = 0; k < permRes.size(); ++k)
        {
            const auto& pr = permRes[k];
            if (k > 0)
                row += ',';
            row += "{\"order\":[";
            for (usize j = 0; j < permOrders[k].size(); ++j)
            {
                if (j > 0)
                    row += ',';
                row += jnum(i64(permOrders[k][j]));
            }
            row += "]";
            row += ",\"nodes\":" + jnum(u64(pr.nodes));
            row += ",\"completed\":" + jbool(pr.completed);
            row += ",\"stop\":" + jstr(stop_name(pr.stopReason));
            row += ",\"budget_hit\":" + jbool(pr.hit_budget);
            row += ",\"value\":" + jvalue_or_null(pr.score, pr.completed);
            row += ",\"fail_high\":" + (pr.completed ? jbool(pr.score >= beta)
                                                           : std::string("null"));
            row += ",\"first\":" + attr_json(pr.attribution, c960);
            row += ",\"cutoff\":" + attr_cutoff_json(pr.attribution, c960, candidates);
            row += ",\"served\":" + jnum(i64(pr.permServed));
            row += ",\"fully_served\":" +
                   jbool(pr.permComplete && pr.permServed == pr.permK);
            row += ",\"slots\":" + attr_slots_json(pr.attribution, c960, candidates);
            row += "}";
        }
        row += "]";
    }
    row += "}";

    const bool recorded = Research::internal_log().write_row(row);
    if (recorded)
    {
        // Track this node's live subtree cost: when this live node's search
        // frame exits, LiveExitScope writes the node_exit audit row that lets
        // consumers compare the baseline replay node count against the real
        // live subtree (determinism check; TT-eviction/budget truncation can
        // still make them differ).
        Research::live_oracle_push(pos.key(), ss->ply, int(depth), h, sampleId, rootKey,
                                   liveNodesBefore, alpha, beta, cutNode, baseline.nodes);
    }

    // Non-perturbation invariants (also covered by unit tests): the live
    // position, node counter, and stop flag are untouched by sampling.
    assert(pos.key() == liveKeyBefore);
    assert(liveWorker.get_nodes() == liveNodesBefore);
    assert(liveWorker.get_threads().stop.load(std::memory_order_relaxed) == liveStopBefore);
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH
