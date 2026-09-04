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

#include "tt_overlay.h"

#ifdef POLICY_RESEARCH

#include <cassert>
#include <iostream>
#include "../thread.h"

namespace Stockfish {

std::tuple<bool, TTData, TTWriter> ResearchTTOverlay::probe(const Key key) {
    const usize c_idx = baseTT.cluster_index(key);
    const u16 key16 = u16(key);
    const u8 gen = baseTT.generation();

    // Check if cluster is already privately copied in the overlay
    auto it = overlayTable.find(c_idx);
    if (it != overlayTable.end()) {
        Cluster& privCluster = it->second;
        auto [entry_idx, hit] = probe_cluster(privCluster, key16, gen);
        TTEntry* const tte = &privCluster.entry[entry_idx];

        if (hit)
            return {tte->is_occupied(), tte->read(), TTWriter(tte)};

        return {false, TTData{Move::none(), VALUE_NONE, VALUE_NONE, DEPTH_NONE, BOUND_NONE, false},
                TTWriter(tte)};
    }

    // Cluster is not yet in overlay; probe immutable base cluster
    const Cluster& baseCluster = baseTT.get_cluster(c_idx);
    auto [entry_idx, hit] = probe_cluster(baseCluster, key16, gen);
    const TTEntry* const baseTte = &baseCluster.entry[entry_idx];

    // Copy-on-write: instantiate the private cluster copy now
    // Copy the complete 32-byte base cluster using copy assignment
    Cluster& privCluster = overlayTable[c_idx];
    privCluster = baseCluster;

    // The returned TTWriter points to the slot in the newly copied private cluster
    TTEntry* const privTte = &privCluster.entry[entry_idx];

    if (hit)
        return {baseTte->is_occupied(), baseTte->read(), TTWriter(privTte)};

    return {false, TTData{Move::none(), VALUE_NONE, VALUE_NONE, DEPTH_NONE, BOUND_NONE, false},
            TTWriter(privTte)};
}

bool run_overlay_unit_tests() {
    ThreadPool threads;
    TranspositionTable base_tt;
    base_tt.resize(16, threads);

    ResearchTTOverlay overlay(base_tt);
    if (overlay.overlay_cluster_count() != 0)
        return false;

    // 1. Base read miss
    Key k1 = 0x123456789abcdef0ULL;
    auto [hit1, data1, writer1] = overlay.probe(k1);
    if (hit1 || overlay.overlay_cluster_count() != 1)
        return false;

    // Write to overlay via writer1
    Move m1 = Move(123);
    writer1.write(k1, Value(500), false, BOUND_EXACT, Depth(4), m1, Value(400), overlay.generation());

    // Verify overlay now has this entry
    auto [hit2, data2, writer2] = overlay.probe(k1);
    if (!hit2 || data2.move != m1 || data2.depth != Depth(4) || data2.bound != BOUND_EXACT ||
        data2.value != Value(500) || data2.eval != Value(400))
        return false;

    // Verify base TT was NOT mutated
    auto [base_hit, base_data, base_writer] = base_tt.probe(k1);
    if (base_hit)
        return false;

    // 2. Base pre-populated entry
    Key k2 = 0xfeedbeefcafebabeULL;
    Move m2 = Move(456);
    auto [bhit_init, bdata_init, bwriter] = base_tt.probe(k2);
    bwriter.write(k2, Value(300), false, BOUND_LOWER, Depth(6), m2, Value(250), base_tt.generation());

    // Overlay probes k2: should see base TT value initially
    if (overlay.overlay_cluster_count() != 1)
        return false;
    auto [ov_hit, ov_data, ov_writer] = overlay.probe(k2);
    if (!ov_hit || ov_data.move != m2 || ov_data.depth != Depth(6) || ov_data.value != Value(300) ||
        overlay.overlay_cluster_count() != 2)
        return false;

    // Overlay mutates k2 entry to something else
    Move m3 = Move(789);
    ov_writer.write(k2, Value(900), false, BOUND_UPPER, Depth(8), m3, Value(800), overlay.generation());

    // Overlay sees mutated value
    auto [ov_hit2, ov_data2, ov_writer2] = overlay.probe(k2);
    if (!ov_hit2 || ov_data2.move != m3 || ov_data2.depth != Depth(8) || ov_data2.value != Value(900))
        return false;

    // Base TT STILL sees original value
    auto [base_hit2, base_data2, base_writer2] = base_tt.probe(k2);
    if (!base_hit2 || base_data2.move != m2 || base_data2.depth != Depth(6) || base_data2.value != Value(300))
        return false;

    // 3. Penalize test
    ov_writer2.penalize(2);
    auto [ov_hit3, ov_data3, ov_writer3] = overlay.probe(k2);
    if (!ov_hit3 || ov_data3.depth != Depth(6))
        return false;
    // Base TT depth still untouched
    auto [base_hit3, base_data3, base_writer3] = base_tt.probe(k2);
    if (base_data3.depth != Depth(6))
        return false;

    // 4. Clear overlay
    overlay.clear_overlay();
    if (overlay.overlay_cluster_count() != 0)
        return false;
    // After clear, overlay probing k2 sees base TT again
    auto [ov_hit4, ov_data4, ov_writer4] = overlay.probe(k2);
    if (!ov_hit4 || ov_data4.move != m2 || ov_data4.depth != Depth(6) || ov_data4.value != Value(300))
        return false;

    return true;
}

}  // namespace Stockfish

#endif  // POLICY_RESEARCH
