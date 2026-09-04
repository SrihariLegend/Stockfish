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

#ifndef POLICY_RESEARCH_WORKER_SNAPSHOT_H_INCLUDED
#define POLICY_RESEARCH_WORKER_SNAPSHOT_H_INCLUDED

#ifdef POLICY_RESEARCH

#include <array>
#include <cassert>
#include <cstddef>
#include <map>
#include <memory>
#include <vector>

#include "../history.h"
#include "../memory.h"
#include "../numa.h"
#include "../position.h"
#include "../search.h"
#include "scoped_probe.h"
#include "tt_overlay.h"

namespace Stockfish {
class Engine;
class ThreadPool;
}

namespace Stockfish::Research {

// IsolatedWorker owns an isolated Search::Worker and its private SharedHistories.
// It allows running isolated shadow searches (e.g. via ResearchTTOverlay) without
// mutating the real live worker, search tree, histories, or base TT.
class IsolatedWorker {
   public:
    explicit IsolatedWorker(const Search::Worker& liveWorker);
    ~IsolatedWorker();

    IsolatedWorker(const IsolatedWorker&)            = delete;
    IsolatedWorker& operator=(const IsolatedWorker&) = delete;

    Search::Worker&       worker() { return *shadowWorker; }
    const Search::Worker& worker() const { return *shadowWorker; }

    u64 nodes_searched() const { return shadowWorker->get_nodes(); }

    // Controls shadow worker stop state independently from the live engine thread pool.
    void stop();
    void reset_stop();
    bool is_stopped() const;

    // Precondition verification: safe isolation requires Threads == 1
    static bool is_safe_environment(const Search::Worker& liveWorker);

    // Evaluates a position using shadowWorker's private accumulatorStack and network
    Value evaluate(const Position& pos);

    // Friend-access helper to evaluate on a live worker for fidelity comparisons
    static Value evaluate_worker(Search::Worker& w, const Position& pos);

    // Synchronizes/copies all mutable history tables and search context from liveWorker.
    void sync_from(const Search::Worker& liveWorker);

    // Clones the stack window from liveSS into private shadowStack, rebinding all
    // continuationHistory and continuationCorrectionHistory pointers to point into
    // shadowWorker's private storage, and sets each frame's pv pointer to private shadowPV.
    // Returns the pointer to shadowSS corresponding to liveSS.
    Search::Stack* clone_and_rebind_stack(const Search::Worker& liveWorker,
                                          const Search::Stack*  liveSS,
                                          int                   windowBefore = 7,
                                          int                   maxDepth     = MAX_PLY);

    // Executes a shadow search using ResearchTTOverlay without mutating live state.
    // If nodeBudget > 0, the shadow search automatically halts once nodeBudget nodes
    // have been searched.
    template<NodeType NT = NonPV>
    Value search(Position&          pos,
                 Search::Stack*     ss,
                 Value              alpha,
                 Value              beta,
                 Depth              depth,
                 bool               cutNode,
                 ResearchTTOverlay& overlay,
                 u64                nodeBudget = 0) {
        ScopedShadowProbe guard;
        if (nodeBudget > 0)
            shadowWorker->limits.nodes = shadowWorker->get_nodes() + nodeBudget;
        else
            shadowWorker->limits.nodes = 0;
        return shadowWorker->search<NT, ResearchTTOverlay>(pos, ss, alpha, beta, depth, cutNode,
                                                           overlay);
    }

   private:
    std::map<NumaIndex, SharedHistories>                       privateSharedHists;
    std::unique_ptr<ThreadPool>                                privateThreadPool;
    std::unique_ptr<Search::SharedState>                       privateSharedState;
    LargePagePtr<Search::Worker>                               shadowWorker;
    std::unique_ptr<std::array<Search::Stack, MAX_PLY + 10>>   shadowStack;
    std::unique_ptr<std::array<Search::PVMoves, MAX_PLY + 10>> shadowPV;
};

// Unit tests verifying Phase 5 §§4-5 requirements:
// 1. Position deep cloning (StateInfo chain, repetition detection, independent moves/undo)
// 2. IsolatedWorker history isolation (private shared history, deep copy, mutation isolation)
// 3. Stack cloning and pointer rebinding (continuation history points to shadow tables)
// 4. Real shadow search execution with ResearchTTOverlay (deterministic score, base TT untouched,
//    live position untouched, live worker untouched, overlay captures writes)
bool run_sandbox_unit_tests(Engine& engine);

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH

#endif  // POLICY_RESEARCH_WORKER_SNAPSHOT_H_INCLUDED
