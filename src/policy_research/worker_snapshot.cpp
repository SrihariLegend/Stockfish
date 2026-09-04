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

#include "../engine.h"
#include "../thread.h"
#include "../uci.h"

namespace Stockfish::Research {

IsolatedWorker::IsolatedWorker(const Search::Worker& liveWorker) {
    const usize threadCount =
      std::max(usize(1), liveWorker.sharedHistory.get_size() / CORRHIST_BASE_SIZE);

    privateSharedHists.emplace(0, threadCount);

    privateSharedState = std::make_unique<Search::SharedState>(
      liveWorker.options, liveWorker.threads, liveWorker.tt, privateSharedHists, liveWorker.network);

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

    // Initialize future forward frames
    for (int k = 1; k <= maxDepth + 2 && (7 + targetPly + k < MAX_PLY + 10); ++k)
    {
        Search::Stack* future                 = shadowSS + k;
        future->ply                           = targetPly + k;
        future->pv                            = &(*shadowPV)[7 + targetPly + k];
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
    }

    // Part 2: Worker Isolation and History Deep Copy
    {
        IsolatedWorker shadowOwner(*liveWorker);

        // Seed distinctive values into live worker
        const u16 moveRaw = 100;
        const Square toSq = SQ_E4;

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

        // Clean up live worker
        liveWorker->mainHistory[WHITE][moveRaw] = i16(0);
        liveWorker->captureHistory[W_PAWN][toSq][PAWN] = i16(0);
        liveWorker->continuationCorrectionHistory[NO_PIECE][0][W_PAWN][toSq] = i16(0);
        liveWorker->continuationHistory[0][0][NO_PIECE][0][W_PAWN][toSq] = i16(0);
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

        TranspositionTable& baseTT = engine.get_tt();
        ResearchTTOverlay overlay(baseTT);
        engine.get_threads().stop = false;

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

        // Verification 4.4: Overlay captured writes, base TT untouched
        if (overlay.overlay_cluster_count() == 0)
        {
            std::cerr << "sandbox_test 4.4 failed: overlay recorded 0 cluster copies during search" << std::endl;
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
        if (shadowOwnerNonPV.nodes_searched() == 0)
        {
            std::cerr << "sandbox_test 4.6 failed: NonPV shadow search searched 0 nodes" << std::endl;
            return false;
        }
    }

    std::cout << "info string research: all sandbox unit tests passed successfully." << std::endl;
    return true;
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH
