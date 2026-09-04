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

#ifndef POLICY_RESEARCH_TT_OVERLAY_H_INCLUDED
#define POLICY_RESEARCH_TT_OVERLAY_H_INCLUDED

#ifdef POLICY_RESEARCH

#include <cstdint>
#include <tuple>
#include <unordered_map>
#include <utility>

#include "../misc.h"
#include "../tt.h"
#include "../types.h"

namespace Stockfish {

// ResearchTTOverlay provides a copy-on-write transposition table view.
// It wraps an immutable reference to the live TranspositionTable (base TT)
// and maintains a private map of copied 32-byte clusters.
//
// Reads from unwritten clusters fall through directly to the base TT.
// Writes or penaltizations to a cluster instantiate a private copy of that cluster,
// so subsequent reads and writes within the shadow probe operate on the private cluster.
// The base TT is NEVER mutated.
class ResearchTTOverlay {
   public:
    explicit ResearchTTOverlay(const TranspositionTable& base_tt) :
        baseTT(base_tt) {}

    // probe adheres to the compile-time TTAccess concept:
    // returns std::tuple<bool, TTData, TTWriter>
    std::tuple<bool, TTData, TTWriter> probe(const Key key);

    // generation forwards to the base TT's current generation
    u8 generation() const { return baseTT.generation(); }

    // hashfull forwards to base TT
    int hashfull(int maxAge = 0) const { return baseTT.hashfull(maxAge); }

    // Size of the copy-on-write overlay (number of copied clusters)
    std::size_t overlay_cluster_count() const { return overlayTable.size(); }

    // Clear overlay state (resetting back to pristine base TT view)
    void clear_overlay() { overlayTable.clear(); }

    // Read-only reference to base TT
    const TranspositionTable& base_table() const { return baseTT; }

   private:
    const TranspositionTable& baseTT;
    // Map cluster index -> copied 32-byte cluster
    std::unordered_map<usize, Cluster> overlayTable;
};

// Unit test function for ResearchTTOverlay verifying:
// 1. Base miss -> read miss, private cluster creation, non-mutation of base
// 2. Base hit -> initial value equivalence, private cluster copy, isolated write
// 3. Private write isolation -> base TT remains pristine
// 4. Penalize isolation -> overlay penalizes depth without altering base TT
// 5. Clear overlay -> reverts overlay view to pristine base TT state
bool run_overlay_unit_tests();

}  // namespace Stockfish

#endif  // POLICY_RESEARCH
#endif  // POLICY_RESEARCH_TT_OVERLAY_H_INCLUDED
