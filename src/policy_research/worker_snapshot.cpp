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
#include <cstring>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include "../engine.h"
#include "../misc.h"
#include "../movegen.h"
#include "../thread.h"
#include "../uci.h"
#include "research_log.h"
#include "research_options.h"

namespace Stockfish::Research {

bool IsolatedWorker::is_safe_environment(const Search::Worker& liveWorker) {
    return int(liveWorker.options["Threads"]) == 1 && liveWorker.threads.num_threads() == 1;
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
    if (!is_safe_environment(liveWorker))
    {
        std::cerr << "IsolatedWorker error: safe isolation requires Threads == 1 and single-thread pool" << std::endl;
        throw std::runtime_error("IsolatedWorker requires Threads == 1 for safe isolation");
    }

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
        future->cutoffCnt                     = 0;
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

        // 7.1: Normal unbudgeted search produces completed ProbeResult
        ProbeResult resNormal = worker7.probe<PV>(shadowPos, shadowSS, -100, 100, Depth(3), false, overlay7, 0);
        if (!resNormal.completed || resNormal.hit_budget || resNormal.nodes == 0)
        {
            std::cerr << "sandbox_test 7.1 failed: normal probe not completed" << std::endl;
            return false;
        }

        // 7.2: Budgeted search with tight budget sets hit_budget and completed == false
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
    }

    // Part 8: Inter-Candidate Order Invariance via CandidateProbeRunner
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p8Pos;
        p8Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        Move m = UCIEngine::to_move(p8Pos, "e2e4");
        states->emplace_back();
        p8Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply = 1;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory = &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        Move candA = UCIEngine::to_move(p8Pos, "c7c5");
        Move candB = UCIEngine::to_move(p8Pos, "e7e5");

        // Run sequence A: probe candA then candB
        CandidateProbeRunner runnerA(*liveWorker, p8Pos, dummySS);
        ProbeResult resA1 = runnerA.probe_candidate(candA, -50, -49, Depth(3), 0);
        ProbeResult resB1 = runnerA.probe_candidate(candB, -50, -49, Depth(3), 0);

        // Run sequence B: probe candB then candA on fresh runner
        CandidateProbeRunner runnerB(*liveWorker, p8Pos, dummySS);
        ProbeResult resB2 = runnerB.probe_candidate(candB, -50, -49, Depth(3), 0);
        ProbeResult resA2 = runnerB.probe_candidate(candA, -50, -49, Depth(3), 0);

        if (resA1.nodes != resA2.nodes || resA1.score != resA2.score)
        {
            std::cerr << "sandbox_test 8.1 failed: candidate A non-deterministic across evaluation orders ("
                      << resA1.nodes << ":" << resA1.score << " vs " << resA2.nodes << ":" << resA2.score << ")" << std::endl;
            return false;
        }
        if (resB1.nodes != resB2.nodes || resB1.score != resB2.score)
        {
            std::cerr << "sandbox_test 8.1 failed: candidate B non-deterministic across evaluation orders ("
                      << resB1.nodes << ":" << resB1.score << " vs " << resB2.nodes << ":" << resB2.score << ")" << std::endl;
            return false;
        }
    }

    // Part 9: Full Candidate Denominator Enumeration (Phase 3 §8.3/§8.4)
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p9Pos;
        p9Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7];
        dummySS->ply = 0;

        auto candidates = enumerate_candidates(p9Pos, *liveWorker, dummySS, Move::none());

        // Startpos has exactly 20 legal moves
        if (candidates.size() != 20)
        {
            std::cerr << "sandbox_test 9.1 failed: candidate count != 20 (" << candidates.size() << ")" << std::endl;
            return false;
        }
        for (const auto& cf : candidates)
        {
            if (!p9Pos.legal(cf.move))
            {
                std::cerr << "sandbox_test 9.2 failed: non-legal candidate emitted" << std::endl;
                return false;
            }
        }
    }

    // Part 10: Real Search Hook Invariant & Non-Mutation Verification
    {
        StateListPtr states = std::make_unique<std::deque<StateInfo>>(1);
        Position p10Pos;
        p10Pos.set("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", false, &states->back());
        Move m = UCIEngine::to_move(p10Pos, "e2e4");
        states->emplace_back();
        p10Pos.do_move(m, states->back());

        auto dummyStack = std::make_unique<std::array<Search::Stack, MAX_PLY + 10>>();
        Search::Stack* dummySS = &(*dummyStack)[7 + 1];
        dummySS->ply = 1;
        for (int i = 7; i > 0; --i)
        {
            (dummySS - i)->continuationHistory = &liveWorker->continuationHistory[0][0][NO_PIECE][0];
            (dummySS - i)->continuationCorrectionHistory = &liveWorker->continuationCorrectionHistory[NO_PIECE][0];
            (dummySS - i)->staticEval = VALUE_NONE;
        }

        const Key keyBefore = p10Pos.key();
        const u64 nodesBefore = liveWorker->get_nodes();
        TranspositionTable& baseTT = engine.get_tt();
        std::vector<u8> ttSnapshot(baseTT.byte_size());
        std::memcpy(ttSnapshot.data(), baseTT.cluster_data(), baseTT.byte_size());

        // Configure internal counterfactual mode
        Research::config().switchOn = true;
        Research::config().mode = Research::Mode::InternalCounterfactual;
        Research::config().sampleRate = 1.0;
        Research::config().topK = 2;
        Research::config().nodeBudget = 50;

        on_internal_node_counterfactual(*liveWorker, p10Pos, dummySS, -50, -49, Depth(3), Depth(3), keyBefore, Move::none());

        // Reset research config
        Research::config().mode = Research::Mode::Off;
        Research::config().switchOn = false;

        if (p10Pos.key() != keyBefore)
        {
            std::cerr << "sandbox_test 10.1 failed: on_internal_node_counterfactual mutated position key" << std::endl;
            return false;
        }
        if (liveWorker->get_nodes() != nodesBefore)
        {
            std::cerr << "sandbox_test 10.1 failed: on_internal_node_counterfactual mutated live worker nodes" << std::endl;
            return false;
        }
        if (std::memcmp(ttSnapshot.data(), baseTT.cluster_data(), baseTT.byte_size()) != 0)
        {
            std::cerr << "sandbox_test 10.1 failed: on_internal_node_counterfactual mutated base TT" << std::endl;
            return false;
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

ProbeResult CandidateProbeRunner::probe_candidate(Move  candidate,
                                                  Value alpha,
                                                  Value beta,
                                                  Depth depth,
                                                  u64   nodeBudget) {
    if (!livePos.legal(candidate))
        return ProbeResult{VALUE_NONE, 0, false, false};

    isolatedWorker.sync_from(liveWorker);
    isolatedWorker.reset_stop();

    Position shadowPos;
    std::vector<StateInfo> shadowStates;
    livePos.clone_to(shadowPos, shadowStates);

    Search::Stack* shadowSS =
      isolatedWorker.clone_and_rebind_stack(liveWorker, liveSS, windowBefore, maxDepth);

    StateInfo newSt;
    isolatedWorker.do_move(shadowPos, candidate, newSt, shadowSS);

    Search::Stack* childSS = shadowSS + 1;

    ResearchTTOverlay overlay(liveWorker.get_tt());

    const Depth childDepth = (depth > 1) ? (depth - 1) : Depth(0);

    ProbeResult res;
    if (beta == alpha + 1)
        res = isolatedWorker.probe<NonPV>(
          shadowPos, childSS, -beta, -alpha, childDepth, false, overlay, nodeBudget);
    else
        res = isolatedWorker.probe<PV>(
          shadowPos, childSS, -beta, -alpha, childDepth, false, overlay, nodeBudget);

    isolatedWorker.undo_move(shadowPos, candidate);

    if (res.completed && res.score != VALUE_NONE && std::abs(res.score) < VALUE_INFINITE)
        res.score = -res.score;

    return res;
}

std::vector<CandidateFeature> enumerate_candidates(const Position&       pos,
                                                   const Search::Worker& worker,
                                                   const Search::Stack*  ss,
                                                   Move                  ttMove) {
    std::vector<CandidateFeature> result;
    const Color us = pos.side_to_move();

    for (const auto& m : MoveList<LEGAL>(pos))
    {
        CandidateFeature cf;
        cf.move = m;
        cf.isCapture = pos.capture_stage(m);
        cf.isCheck = pos.gives_check(m);
        cf.isTTMove = (ttMove != Move::none() && m == ttMove);

        const Piece pc = pos.moved_piece(m);
        const Square to = m.to_sq();

        cf.mainHist = int(worker.mainHistory[us][m.raw()]);
        cf.captureHist = cf.isCapture ? int(worker.captureHistory[pc][to][type_of(pos.piece_on(to))]) : 0;

        cf.contHist = 0;
        if (ss != nullptr)
        {
            if (ss->ply >= 1)
                cf.contHist += int((*(ss - 1)->continuationHistory)[pc][to]);
            if (ss->ply >= 2)
                cf.contHist += int((*(ss - 2)->continuationHistory)[pc][to]);
            if (ss->ply >= 4)
                cf.contHist += int((*(ss - 4)->continuationHistory)[pc][to]);
        }

        cf.seeScore = pos.see_ge(m, 0) ? 1 : (pos.see_ge(m, -100) ? 0 : -1);

        if (cf.isTTMove)
        {
            cf.stage = 1;
            cf.stageScore = 1000000;
        }
        else if (cf.isCapture && pos.see_ge(m, 0))
        {
            cf.stage = 2;
            cf.stageScore = 500000 + cf.captureHist;
        }
        else if (!cf.isCapture)
        {
            cf.stage = 4;
            cf.stageScore = 2 * cf.mainHist + cf.contHist;
        }
        else
        {
            cf.stage = 5;
            cf.stageScore = -100000 + cf.captureHist;
        }

        result.push_back(cf);
    }

    std::stable_sort(result.begin(), result.end(), [](const CandidateFeature& a, const CandidateFeature& b) {
        return a.stageScore > b.stageScore;
    });

    return result;
}

void on_internal_node_counterfactual(Search::Worker& liveWorker,
                                     Position&       pos,
                                     Search::Stack*  ss,
                                     Value           alpha,
                                     Value           beta,
                                     Depth           depth,
                                     Depth           rootDepth,
                                     Key             rootKey,
                                     Move            ttMove) {
    if (!Research::enabled() || Research::config().mode != Research::Mode::InternalCounterfactual
        || Research::is_shadow_probe_active() || !liveWorker.is_mainthread())
        return;

    u64 h = Research::config().seed;
    h += 0x9E3779B97F4A7C15ULL ^ rootKey;
    h = (h ^ (h >> 30)) * 0xBF58476D1CE4E5B9ULL ^ pos.key();
    h = (h ^ (h >> 27)) * 0x94D049BB133111EBULL
        ^ (static_cast<u64>(ss->ply) | (static_cast<u64>(depth) << 16) | (static_cast<u64>(rootDepth) << 32));
    h ^= (h >> 31);
    const u32 sampleThreshold =
      static_cast<u32>(std::clamp(Research::config().sampleRate, 0.0, 1.0) * 1000000.0 + 0.5);
    if ((h % 1000000ULL) >= sampleThreshold)
        return;

    const Key  liveKeyBefore   = pos.key();
    const u64  liveNodesBefore = liveWorker.get_nodes();
    const bool liveStopBefore  = liveWorker.get_threads().stop.load(std::memory_order_relaxed);

    auto candidates = enumerate_candidates(pos, liveWorker, ss, ttMove);
    if (candidates.size() < 2)
        return;

    int probeK = (Research::config().topK > 0) ? Research::config().topK : 4;
    probeK     = std::min(probeK, int(candidates.size()));

    struct CandidateProbeSummary {
        Move        move;
        ProbeResult result;
    };
    std::vector<CandidateProbeSummary> probeResults;
    probeResults.reserve(probeK);

    CandidateProbeRunner runner(liveWorker, pos, ss);

    for (int i = 0; i < probeK; ++i)
    {
        Move        m   = candidates[i].move;
        ProbeResult res = runner.probe_candidate(m, alpha, beta, depth, Research::config().nodeBudget);
        probeResults.push_back({m, res});
    }

    std::stringstream ss_tel;
    ss_tel << "info string research internal_counterfactual root " << std::hex << rootKey << std::dec
           << " ply " << ss->ply << " depth " << int(depth) << " alpha " << int(alpha)
           << " beta " << int(beta) << " candidates " << probeResults.size();
    for (const auto& pr : probeResults)
    {
        ss_tel << " " << UCIEngine::move(pr.move, pos.is_chess960()) << ":" << pr.result.nodes
               << ":" << int(pr.result.score) << ":"
               << (pr.result.completed ? "ok" : (pr.result.hit_budget ? "budget" : "stopped"));
    }
    sync_cout << ss_tel.str() << sync_endl;

    assert(pos.key() == liveKeyBefore);
    assert(liveWorker.get_nodes() == liveNodesBefore);
    assert(liveWorker.get_threads().stop.load(std::memory_order_relaxed) == liveStopBefore);
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH
