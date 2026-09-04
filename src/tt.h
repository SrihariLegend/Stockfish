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

#ifndef TT_H_INCLUDED
#define TT_H_INCLUDED

#include <tuple>

#include "misc.h"
#include "memory.h"
#include "types.h"

namespace Stockfish {

class ThreadPool;
struct Cluster;
struct TTWriter;

// There is only one global hash table for the engine and all its threads.
// For chess in particular, we even allow racy updates between threads to and
// from the TT, as taking the time to synchronize access would cost thinking
// time and thus Elo. As a hash table, collisions are possible and may cause
// chess playing issues (bizarre blunders, faulty mate reports, etc). Fixing
// these also loses Elo; however such risk decreases with larger TT size.
//
// We clearly separate TTData, a local copy of an entry, from TTWriter, which
// writes to the global table.


// A copy of the data already in an entry (possibly collided). Probes and reads
// are racy and non-atomic, possibly resulting in inconsistent data.
struct TTData {
    Move  move;
    Value value, eval;
    Depth depth;
    Bound bound;
    bool  is_pv;

    TTData() = delete;

    // clang-format off
    TTData(Move m, Value v, Value ev, Depth d, Bound b, bool pv) :
        move(m),
        value(v),
        eval(ev),
        depth(d),
        bound(b),
        is_pv(pv) {}
    // clang-format on
};

static constexpr u8 GENERATION_BITS = 5;
static constexpr u8 GENERATION_MASK = (1 << GENERATION_BITS) - 1;
static constexpr u8 BOUND_SHIFT     = GENERATION_BITS;
static constexpr u8 BOUND_MASK      = 0b11 << BOUND_SHIFT;
static constexpr u8 PV_SHIFT        = BOUND_SHIFT + 2;
static constexpr u8 PV_MASK         = 1 << PV_SHIFT;

static constexpr int ClusterSize = 3;

struct TTEntry {
    TTData read() const {
        return TTData{Move(move16),
                      Value(value16),
                      Value(eval16),
                      Depth(DEPTH_NONE + depth8),
                      Bound((genBound8 & BOUND_MASK) >> BOUND_SHIFT),
                      bool(genBound8 & PV_MASK)};
    }

    bool is_occupied() const { return bool(depth8); }
    void save(Key k, Value v, bool pv, Bound b, Depth d, Move m, Value ev, u8 curr_generation);
    u8   relative_age(const u8 curr_generation) const;

   private:
    friend class TranspositionTable;
    friend struct TTWriter;
    friend struct Cluster;
    friend std::pair<int, bool> probe_cluster(const Cluster& cluster, u16 key16, u8 generation8);
#ifdef POLICY_RESEARCH
    friend class ResearchTTOverlay;
#endif

    RelaxedAtomic<u16>  key16;
    RelaxedAtomic<u8>   depth8;
    RelaxedAtomic<u8>   genBound8;
    RelaxedAtomic<Move> move16;
    RelaxedAtomic<i16>  value16;
    RelaxedAtomic<i16>  eval16;
};

struct Cluster {
    TTEntry entry[ClusterSize];
    char    padding[2];  // Pad to 32 bytes
};

static_assert(sizeof(Cluster) == 32, "Suboptimal Cluster size");

// Looks up a key inside a cluster or selects the best replacement candidate.
// Returns a pair of:
//   1) cluster entry index (0 <= idx < ClusterSize)
//   2) whether the entry is an exact 16-bit key hit
inline std::pair<int, bool> probe_cluster(const Cluster& cluster, u16 key16, u8 generation8) {
    const TTEntry* const tte = &cluster.entry[0];

    for (int i = 0; i < ClusterSize; ++i)
        if (tte[i].key16 == key16)
            return {i, true};

    // Find an entry to be replaced according to the replacement strategy
    int replace_idx = 0;
    for (int i = 1; i < ClusterSize; ++i)
        if (tte[replace_idx].depth8 - 8 * tte[replace_idx].relative_age(generation8)
            > tte[i].depth8 - 8 * tte[i].relative_age(generation8))
            replace_idx = i;

    return {replace_idx, false};
}

// This is used to make racy, non-atomic writes to the global TT. Writes are
// not "guaranteed": for chess reasons, we may decide the new data is less
// important than the old.
struct TTWriter {
   public:
    void write(Key k, Value v, bool pv, Bound b, Depth d, Move m, Value ev, u8 generation8);
    void penalize(int penalty);  // decrement stored depth by the penalty

    TTWriter(TTEntry* tte);
   private:
    friend class TranspositionTable;
#ifdef POLICY_RESEARCH
    friend class ResearchTTOverlay;
#endif
    TTEntry* entry;
};


class TranspositionTable {

   public:
    ~TranspositionTable() { aligned_large_pages_free(table); }

    // Set TT size in MiB
    void resize(usize mbSize, ThreadPool& threads);

    // Re-initialize memory, multithreaded
    void clear(ThreadPool& threads);

    // Must be called at the beginning of each root search to track entry aging
    void new_search();

    // The current age, used when writing new data to the TT
    u8 generation() const;

    // Approximate what fraction of entries (permille) have been written to
    // during this root search.
    int hashfull(int maxAge = 0) const;

    // `probe(key)` is the primary method: given a board position, we lookup
    //  its entry in the table, and return a tuple of:
    //   1) whether the entry already had data on this position
    //   2) a copy of the prior data, if any (may be self-inconsistent due to races)
    //   3) a writer object to the entry
    std::tuple<bool, TTData, TTWriter> probe(const Key key) const;

    // The hash function; its only external use is memory prefetching
    TTEntry* first_entry(const Key key) const;

#ifdef POLICY_RESEARCH
    usize cluster_index(const Key key) const { return mul_hi64(key, clusterCount); }
    const Cluster& get_cluster(usize idx) const { return table[idx]; }
    usize cluster_count() const { return clusterCount; }
    const Cluster* cluster_data() const { return table; }
    usize byte_size() const { return clusterCount * sizeof(Cluster); }
#endif

   private:
    friend struct TTEntry;

    usize    clusterCount;
    Cluster* table = nullptr;

    u8 generation8 = 0;
};

}  // namespace Stockfish

#endif  // #ifndef TT_H_INCLUDED
