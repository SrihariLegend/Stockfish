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
#include "../movegen.h"
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

// ProbeResult captures the outcome of an isolated shadow search probe.
// Distinguishes completed evaluations from budget-censored or aborted probes.
//
// Censoring contract (docs/policy-research/plan.md section 10.6, data-schema):
// a censored probe is NOT a value: score stays VALUE_NONE (never a search value,
// never negated, never printed as a numeric score), completed == false, and the
// stop reason records whether the node budget or an explicit stop() ended the
// search. nodes always reports the do_moves spent, including censored probes.
struct ProbeResult {
    enum class StopReason : u8 { None = 0, Budget = 1, UserStop = 2 };

    Value      score      = VALUE_NONE;  // valid only when completed
    u64        nodes      = 0;           // shadow do_moves spent by this probe
    bool       completed  = false;       // search returned before any stop
    bool       hit_budget = false;       // == (stopReason == StopReason::Budget)
    StopReason stopReason = StopReason::None;

    // True when an armed force-next scope (whole-node replay) was consumed:
    // the forced candidate was emitted by the real MovePicker and received this
    // node's slot-1 treatment (it may still have been pruned by the ordinary
    // per-move prunes, which is exactly the live slot-1 semantics). False for
    // unforced replays and for replays that returned before the move loop.
    bool forced_slot1 = false;
};

// CandidateStage mirrors the REAL MovePicker emission classes observed by
// driving MovePicker::next_move() to exhaustion in enumerate_candidates() --
// no duplicated scoring or ordering heuristics. Classification rules (they
// replicate the picker's own split conditions on the picker's own scores):
//   TT:         emission 0 == pseudo-legal ttMove (MAIN_TT stage)
//   captures:   picker split on pos.see_ge(move, -stageScore / 18)
//   quiets:     picker split on stageScore > -14000 (goodQuietThreshold)
enum class CandidateStage : u8 {
    TT         = 1,
    GoodCapture = 2,
    BadCapture  = 3,
    GoodQuiet   = 4,
    BadQuiet    = 5
};

// CandidateFeature captures the features of one legal candidate at a decision
// node, in real MovePicker emission order (Phase 3 §8.3/§8.4, Phase 5 §10.4).
struct CandidateFeature {
    Move           move = Move::none();
    u16            ordinal = 0;      // 0-based slot among legal candidates in real
                                     // MovePicker order (search-visible moveCount
                                     // for this move is ordinal + 1)
    CandidateStage stage = CandidateStage::GoodQuiet;
    int            stageScore = 0;   // the real MovePicker sort value at emission
                                     // (select()-based stages only; TT stage has none)
    int            mainHist = 0;     // raw mainHistory[us][move.raw()]
    int            captureHist = 0;  // raw captureHistory[pc][to][captured] (captures only)
    int            pawnHist = 0;     // raw sharedHistory pawn entry [pc][to] (quiets only)
    int            contHist = 0;     // sum of continuation entries at the search's own
                                     // offsets 0,1,2,3,5 (quiets only)
    int            lowPlyHist = 0;   // raw lowPlyHistory[ply][raw] (quiets, ply < 5)
    int            seeScore = 0;     // see_bucket in the dataset: +1 see_ge(m,0), 0 see_ge(m,-100), else -1
    bool           isCheck = false;
    bool           isCapture = false;
    bool           isTTMove = false; // == the pseudo-legal ttMove emitted first
    double         selectionProb = 0.0;  // marginal inclusion probability in the
                                         // probe set (1.0, or 2/(N-K) for the
                                         // deterministic hash-sampled rest)
    bool           selected = false;     // filled by the sampler
};

// Human-readable MovePicker stage name (public helper for diagnostics/UCI).
const char* candidate_stage_name(CandidateStage s);

// ---------------------------------------------------------------------------
// Force-next (slot-1) machinery for whole-node counterfactual replays
// (plan §10.6, review fix: the candidate is forced into the node's exact
// slot-1 treatment, i.e. full decision-node replay with the candidate emitted
// first. Emissions the real MovePicker would have produced BEFORE the forced
// candidate are swallowed without search or moveCount but buffered; once the
// forced move has been searched and fails low, they are served back in their
// original natural order before the picker's suffix resumes. The replay
// therefore implements a genuine slot-1 REORDER -- [forced, original prefix,
// original suffix] -- not a deletion of the earlier candidates).
//
// The armed request is thread-local and RAII-scoped to one synchronous replay
// (ForceNextScope); the live move loop consumes it with force_next_step()
// when it reaches a node whose (position key, ply) matches. Only the main
// search move loop consumes; the qsearch loop and all other nodes never match
// unless armed. Nested arming is a programming error (assert).
//
// Frame-token binding (audit item #4): arms are additionally bound to the
// exact search frame that owns the target node. Same-(posKey, ply) frames can
// legitimately re-enter the node (singular-extension re-search at depth >= 6
// with ttPv, null-move verification re-search at depth >= 16); without a
// frame identity those frames could swallow the prefix, consume the forced
// emission, or drain the buffer in the wrong context. research_frame_enter()
// (called at the top of every search() frame in POLICY_RESEARCH builds) binds
// a pending arm to the FIRST frame that enters its (posKey, ply) -- the
// replay root itself -- and every arm protocol step requires the current
// innermost frame token to match.
// ---------------------------------------------------------------------------
// Monotonic TLS frame sequence and innermost-frame token. Every search()
// frame (research builds) constructs a ResearchFrameScope at entry:
// gResearchCurFrame then names the innermost live search frame while its
// body runs, and is restored to the parent frame's token on exit.
inline thread_local u64 gResearchFrameSeq = 0;
inline thread_local u64 gResearchCurFrame = 0;

inline u64 current_frame_token() { return gResearchCurFrame; }

class ResearchFrameScope {
   public:
    ResearchFrameScope() {
        prev_ = gResearchCurFrame;
        gResearchCurFrame = ++gResearchFrameSeq;
    }
    ~ResearchFrameScope() { gResearchCurFrame = prev_; }

    ResearchFrameScope(const ResearchFrameScope&)            = delete;
    ResearchFrameScope& operator=(const ResearchFrameScope&) = delete;

   private:
    u64 prev_;
};

struct ForceNextState {
    bool armed = false;
    bool consumed = false;
    Key  posKey = 0;
    int  ply = -1;
    u64  frameToken = 0;  // bound at replay-root frame entry; 0 = not yet bound
    Move forced = Move::none();
    // Prefix buffer: legal natural-order emissions that precede the forced
    // candidate in MovePicker order. Filled while armed (swallowed without
    // search or moveCount); drained by force_next_buffer_pop() after the arm
    // is consumed. A legal position has at most MAX_MOVES legal moves, so a
    // fixed-size buffer is exact.
    std::array<Move, MAX_MOVES> prefix = {};
    int                         prefixCount = 0;
    int                         prefixIdx = 0;
};

inline thread_local ForceNextState      gForceNext;
inline thread_local u64                 gForceNextConsumedCount = 0;
// Test-observable counters, reset per arm by ForceNextScope: the largest
// prefix depth the buffer reached while armed, and the number of buffered
// prefix moves actually served back by force_next_buffer_pop() (i.e. moves
// the node RE-searched after the forced candidate failed low).
inline thread_local int  gForceNextMaxPrefixSeen = 0;
inline thread_local int  gForceNextBufferPops = 0;

class ForceNextScope {
   public:
    ForceNextScope(Key posKey, int ply, Move forced) {
        assert(!gForceNext.armed && !gForceNext.consumed);  // never nest
        gForceNext              = {};
        gForceNext.armed        = true;
        gForceNext.posKey       = posKey;
        gForceNext.ply          = ply;
        gForceNext.forced       = forced;
        gForceNextConsumedCount = 0;
        gForceNextMaxPrefixSeen = 0;
        gForceNextBufferPops    = 0;
    }
    ~ForceNextScope() { gForceNext = {}; }

    ForceNextScope(const ForceNextScope&)            = delete;
    ForceNextScope& operator=(const ForceNextScope&) = delete;
};

// Called by the main search move loop on every emitted move while a shadow
// probe is active (research builds only; the call site is macro-gated).
// Returns 1 when this emission must be SWALLOWED: the replay is armed for
// this exact node and the forced candidate has not been emitted yet, so the
// emission is buffered without a search and without incrementing moveCount
// (the forced candidate keeps the node's exact slot-1 treatment). Returns 0
// otherwise; when the emitted move IS the forced candidate the arm is cleared
// and marked consumed, and the move proceeds as the node's slot-1 move.
inline int force_next_step(Key posKey, int ply, Move move) {
    if (!gForceNext.armed || gForceNext.posKey != posKey || gForceNext.ply != ply
        || gForceNext.frameToken != gResearchCurFrame)
        return 0;
    if (move == gForceNext.forced)
    {
        gForceNext.armed    = false;
        gForceNext.consumed = true;
        ++gForceNextConsumedCount;
        return 0;  // forced move proceeds as slot 1
    }
    // Natural-order emission that precedes the forced candidate: swallow it
    // into the prefix buffer (it keeps slot-1 semantics for the forced move;
    // it is searched after the forced move fails low).
    assert(gForceNext.prefixCount < MAX_MOVES);
    if (gForceNext.prefixCount < MAX_MOVES)
        gForceNext.prefix[gForceNext.prefixCount++] = move;
    if (gForceNext.prefixCount > gForceNextMaxPrefixSeen)
        gForceNextMaxPrefixSeen = gForceNext.prefixCount;
    return 1;
}

// Serves buffered pre-forced moves in their original natural order. Only
// active AFTER the arm was consumed (forced candidate emitted as slot 1) AND
// at the exact node that was armed (same position key and ply), so the
// forced move is never displaced and the prefix can never leak into a
// descendant or transpositional node's move loop (which would emit a stale
// move whose from-square is empty or enemy-occupied in that node's position).
// Returns false when nothing is buffered, the arm is still waiting for its
// forced emission, or the caller is not the armed node; callers then pull the
// next real MovePicker emission.
inline bool force_next_buffer_pop(Key posKey, int ply, Move& move) {
    if (gForceNext.armed || gForceNext.posKey != posKey || gForceNext.ply != ply
        || gForceNext.frameToken != gResearchCurFrame || gForceNext.prefixIdx >= gForceNext.prefixCount)
        return false;
    move = gForceNext.prefix[gForceNext.prefixIdx++];
    ++gForceNextBufferPops;
    return true;
}

// ---------------------------------------------------------------------------
// Decision-point capture channel for the isolated BASELINE replay of a
// sampled node. The orchestrator hook now fires at the sampled node's search
// ENTRY (search.cpp), where TT/static-eval/improving state does not exist
// yet; the baseline replay instead re-executes the node from that entry state
// and, at its own main move loop, fires decision_capture_step() when the
// (position key, ply) matches. The capture therefore records decision-point
// metadata and the full legal candidate enumeration from the replay's OWN
// freshly constructed decision state -- the state the slot-1 forced replays
// (same entry state, same deterministic pre-loop) also operate on, and the
// state the live node is guaranteed to reach whenever the replay does.
// Rows whose replay never fires the capture (deterministic pre-loop cutoffs)
// are dropped: no decision point, no row.
// ---------------------------------------------------------------------------
struct DecisionCapture {
    bool  armed = false;       // armed for one baseline replay
    bool  fired = false;       // the target decision point was reached
    Key   posKey = 0;
    int   ply = -1;
    u64   frameToken = 0;  // bound at replay-root frame entry; 0 = not yet bound
    bool  ttHit = false;
    bool  improving = false;
    Value staticEval = VALUE_NONE;
    Move  ttMove = Move::none();
    int   decisionDepth = 0;   // search depth at the decision point (post-IIR)
    u64   decisionPointNodes = 0;  // shadow do_moves spent up to the decision point
    std::vector<CandidateFeature> candidates;  // full legal denominator, emission order
};

inline thread_local DecisionCapture gDecisionCapture;

// Node-counter epoch of the innermost running probe (set at probe start); the
// decision capture reports do_moves spent INSIDE the replay only, since the
// shadow worker's counter is re-synced from the live worker at sync_from().
inline thread_local u64 gProbeStartNodes = 0;

class DecisionCaptureScope {
   public:
    DecisionCaptureScope(Key posKey, int ply) {
        gDecisionCapture         = DecisionCapture{};
        gDecisionCapture.armed   = true;
        gDecisionCapture.posKey  = posKey;
        gDecisionCapture.ply     = ply;
    }
    ~DecisionCaptureScope() { gDecisionCapture.armed = false; }

    DecisionCaptureScope(const DecisionCaptureScope&)            = delete;
    DecisionCaptureScope& operator=(const DecisionCaptureScope&) = delete;
};

// Called at the top of every search() frame (right after the frame scope).
// Binds a pending baseline-capture or force-next arm to this frame when it is
// the first search frame entering the armed (posKey, ply) -- which by
// construction is the isolated replay's root frame, since arms are only ever
// created immediately before a probe starts its own synchronous search.
inline void research_frame_enter(Key posKey, int ply) {
    if (gDecisionCapture.armed && gDecisionCapture.frameToken == 0
        && gDecisionCapture.posKey == posKey && gDecisionCapture.ply == ply)
        gDecisionCapture.frameToken = gResearchCurFrame;
    if (gForceNext.armed && gForceNext.frameToken == 0 && gForceNext.posKey == posKey
        && gForceNext.ply == ply)
        gForceNext.frameToken = gResearchCurFrame;
}

// Fired by the main move loop (search.cpp, POLICY_RESEARCH builds) on every
// node; cheaply no-ops unless a baseline replay armed this exact node.
void decision_capture_step(Search::Worker& worker, Position& pos, Search::Stack* ss,
                           Key posKey, int ply, bool ttHit, Move ttMove, bool improving,
                           Value staticEval, int decisionDepth);

// ---------------------------------------------------------------------------
// Live-subtree node oracle. The sampling hook records, per sampled decision
// row, the live node's real subtree cost measured at the node's frame exit
// (live do_moves from the sampling hook to the node's return). This is
// written as a separate JSONL node_exit audit row when the node's search frame
// exits, and is the per-node check that the isolated baseline replay's node
// count corresponds to the live node's real subtree (the two should match
// under determinism, modulo TT-eviction and budget-truncation differences).
// A thread-local LIFO mirrors the synchronous nesting of sampled frames.
// ---------------------------------------------------------------------------
struct LiveOracleEntry {
    Key  posKey = 0;
    int  ply = -1;
    int  entryDepth = 0;
    u64  sampleSeed = 0;
    u64  sampleId = 0;  // per-visit id, joined with the decision row
    u64  frameToken = 0;  // search-frame token of the sampled node's frame
    Key  rootKey = 0;
    u64  entryNodes = 0;
    u64  liveNodes = 0;  // filled at pop: live do_moves from entry to exit
};

// One slot per ply plus slack: sampled live frames nest along the search
// path (every eligible ancestor node pushes), and same-(posKey, ply) frame
// re-entries (singular-extension / null-move-verification re-searches) are
// separate frames whose tokens never pop a sampled entry, so the tracked
// stack cannot exceed the node path length plus a small constant.
inline thread_local std::array<LiveOracleEntry, MAX_PLY + 4> gLiveOracle{};
inline thread_local int                                      gLiveOracleCount = 0;

inline bool live_oracle_push(Key posKey, int ply, int entryDepth, u64 sampleSeed, u64 sampleId,
                             Key rootKey, u64 entryNodes) {
    if (gLiveOracleCount >= int(gLiveOracle.size()))
        return false;
    gLiveOracle[gLiveOracleCount++] =
      LiveOracleEntry{posKey, ply, entryDepth, sampleSeed, sampleId, gResearchCurFrame, rootKey,
                      entryNodes, 0};
    return true;
}

// Called from a node frame's exit (RAII scope in search.cpp). Pops and fills
// out (when non-null) with a copy of the entry plus its live subtree cost
// when the exiting frame is the innermost tracked sampled node; returns false
// otherwise (e.g. shadow-probe frames, which never push, a non-sampled twin,
// or a same-(posKey, ply) re-entry frame -- singular-extension or null-move
// verification -- whose frame token differs from the sampled node's).
inline bool live_oracle_pop(Key posKey, int ply, u64 exitNodes, LiveOracleEntry* out) {
    if (gLiveOracleCount <= 0)
        return false;
    LiveOracleEntry& top = gLiveOracle[gLiveOracleCount - 1];
    if (top.posKey != posKey || top.ply != ply || top.frameToken != gResearchCurFrame)
        return false;
    if (out != nullptr)
    {
        *out            = top;
        out->liveNodes  = exitNodes - top.entryNodes;
    }
    top = LiveOracleEntry{};
    --gLiveOracleCount;
    return true;
}

// RAII scope constructed in every search() frame (search.cpp, POLICY_RESEARCH
// builds): when the frame exits and it is the innermost tracked sampled node,
// the live subtree node cost is popped and a node_exit audit row is written
// (joined to its decision row by root_key + pos_key + ply + entry_depth +
// sample_seed). Definition in worker_snapshot.cpp.
class LiveExitScope {
   public:
    LiveExitScope(const Search::Worker& worker, const Position& pos, const Search::Stack* ss);
    ~LiveExitScope();

    LiveExitScope(const LiveExitScope&)            = delete;
    LiveExitScope& operator=(const LiveExitScope&) = delete;

   private:
    const Search::Worker* worker_;
    const Position*       pos_;
    const Search::Stack*  ss_;
};

// Enumerates all legal candidate moves at the given position by driving the
// REAL MovePicker (same construction parameters as the step-14 move loop in
// search.cpp: live worker histories, ss continuation window, search depth) to
// exhaustion, filtering pos.legal() exactly like the search does. The returned
// vector is therefore in exact search-visible emission order with search-visible
// ordinals and per-candidate features; stageScore is the picker's own sort
// value (research accessor, no formula duplication). ttMove is the node's TT
// move. depth must be the node's search depth (affects the picker's quiet
// partial sort cutoff). contHist, when non-null, must be the search's own
// continuation-history window ((ss-1)..(ss-6) pointers); when null it is
// derived from ss exactly like search.cpp does.
std::vector<CandidateFeature> enumerate_candidates(const Position&            pos,
                                                   const Search::Worker&      worker,
                                                   const Search::Stack*       ss,
                                                   Move                        ttMove,
                                                   Depth                       depth,
                                                   const PieceToHistory**      contHist = nullptr);

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

    // Pure configuration predicate (no engine state touched): safe isolation
    // requires the Threads option value AND the live pool size to both be 1.
    // Kept separate so unit tests can exercise both halves without resizing the
    // live pool (resizing replaces Thread objects and would dangle pointers).
    static bool is_safe_configuration(int optionThreads, usize poolThreads) {
        return optionThreads == 1 && poolThreads == 1;
    }

    // Friend-access wrappers to do/undo move with accumulatorStack push/pop on shadowWorker
    void do_move(Position& pos, Move m, StateInfo& st, Search::Stack* ss);
    void undo_move(Position& pos, Move m);

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
    //
    // Node-budget semantics: with nodeBudget > 0 the shadow search is limited to
    // nodeBudget shadow do_moves counted from the start of this probe; when the
    // cap is exhausted the private pool stop flag is raised by the shadow search
    // entry checks (search.cpp, POLICY_RESEARCH builds) and the probe is reported
    // censored (completed == false, score == VALUE_NONE, stopReason == Budget).
    // A probe that returns naturally without the cap being exhausted is completed
    // even when it consumed exactly nodeBudget-1 do_moves; the boundary case
    // spent == nodeBudget is only reachable through the entry check and therefore
    // always censors, which makes the reported budget a strict, safe upper bound.
    //
    // nodeBudget == 0 runs an unlimited probe. Internal sampling never does this:
    // the hook applies Research::effective_node_budget(), whose finite default is
    // documented in research_options.h; unlimited probes exist only for unit
    // tests and explicitly unbudgeted diagnostic callers. A probe can also be
    // censored by stop() raised before/while searching (stopReason == UserStop).
    template<NodeType NT = NonPV>
    ProbeResult probe(Position&          pos,
                      Search::Stack*     ss,
                      Value              alpha,
                      Value              beta,
                      Depth              depth,
                      bool               cutNode,
                      ResearchTTOverlay& overlay,
                      u64                nodeBudget = 0) {
        ScopedShadowProbe guard;
        const u64 startNodes = shadowWorker->get_nodes();
        gProbeStartNodes     = startNodes;  // decision-capture epoch
        shadowWorker->limits.nodes = (nodeBudget > 0) ? (startNodes + nodeBudget) : 0;

        const u64 fnBefore = gForceNextConsumedCount;
        Value val = shadowWorker->search<NT, ResearchTTOverlay>(pos, ss, alpha, beta, depth, cutNode,
                                                               overlay);

        const u64 endNodes = shadowWorker->get_nodes();
        const bool stopped = is_stopped();

        ProbeResult result;
        result.nodes      = (endNodes >= startNodes) ? (endNodes - startNodes) : 0;
        result.completed  = !stopped;
        result.stopReason = stopped
                              ? (nodeBudget > 0 ? ProbeResult::StopReason::Budget
                                                : ProbeResult::StopReason::UserStop)
                              : ProbeResult::StopReason::None;
        result.hit_budget = result.stopReason == ProbeResult::StopReason::Budget;
        // Censored probes carry no value (never a numeric score, never a draw):
        result.score      = result.completed ? val : VALUE_NONE;
        result.forced_slot1 = gForceNextConsumedCount != fnBefore;
        return result;
    }

    // Convenience wrapper returning only the score (VALUE_NONE when censored)
    template<NodeType NT = NonPV>
    Value search(Position&          pos,
                 Search::Stack*     ss,
                 Value              alpha,
                 Value              beta,
                 Depth              depth,
                 bool               cutNode,
                 ResearchTTOverlay& overlay,
                 u64                nodeBudget = 0) {
        return probe<NT>(pos, ss, alpha, beta, depth, cutNode, overlay, nodeBudget).score;
    }

   private:
    std::map<NumaIndex, SharedHistories>                       privateSharedHists;
    std::unique_ptr<ThreadPool>                                privateThreadPool;
    std::unique_ptr<Search::SharedState>                       privateSharedState;
    LargePagePtr<Search::Worker>                               shadowWorker;
    std::unique_ptr<std::array<Search::Stack, MAX_PLY + 10>>   shadowStack;
    std::unique_ptr<std::array<Search::PVMoves, MAX_PLY + 10>> shadowPV;
};

// CandidateProbeRunner executes isolated whole-node counterfactual replays for
// multiple candidate moves at a specific internal decision state. It guarantees
// complete inter-candidate isolation: every replay (baseline and per-candidate)
// uses an independent ResearchTTOverlay, a fresh position clone, a rebound
// search stack, and a re-synchronized worker snapshot, eliminating
// candidate-order contamination.
//
// The force-next estimand (plan §10.6, internal-counterfactual/2):
// replay_node_forced() re-runs the whole decision node's search from its
// captured entry state with the candidate armed as slot-1 move (see
// ForceNextScope); the replay applies the node's exact step-1..step-13
// pipeline and the candidate receives the exact slot-1 treatment (TT
// handling, razoring/futility gates already passed at hook time, small
// probCut, per-move prunes, LMR, re-search, history updates). If the forced
// move fails low the replay continues over the buffered natural-order prefix
// and then the picker suffix -- a genuine [forced, prefix, suffix] reorder --
// so the measured outcome (fail-low/fail-high value, node cost, censoring)
// is the decision-node outcome with the candidate forced first. Nodes whose
// replay returns before the move loop never consume the arm (forced_slot1 ==
// false) and are not decision nodes: the sampler drops those rows.
class CandidateProbeRunner {
   public:
    CandidateProbeRunner(const Search::Worker& liveWorker,
                         const Position&       livePos,
                         const Search::Stack*  liveSS,
                         int                   windowBefore = 7,
                         int                   maxDepth     = MAX_PLY);

    // Whole-node replay WITHOUT forcing: the measurement-B baseline replay.
    ProbeResult replay_node(Value alpha, Value beta, Depth depth, bool cutNode,
                            u64 nodeBudget = 0);

    // Whole-node replay WITH the candidate forced into slot 1.
    ProbeResult replay_node_forced(Move forced, Value alpha, Value beta, Depth depth,
                                   bool cutNode, u64 nodeBudget = 0);

    IsolatedWorker& worker() { return isolatedWorker; }

   private:
    ProbeResult replay_impl(Move forced, Value alpha, Value beta, Depth depth, bool cutNode,
                            u64 nodeBudget);

    const Search::Worker& liveWorker;
    const Position&       livePos;
    const Search::Stack*  liveSS;
    int                   windowBefore;
    int                   maxDepth;
    IsolatedWorker        isolatedWorker;
};

// Internal-node counterfactual hook invoked from Search::Worker::search at
// the ENTRY of eligible NonPV null-window nodes (search.cpp, after the
// pre-node early returns, before the node's own TT probe/static eval/pre-loop
// work, which the whole-node replays re-execute from the same state).
// Sampling, full-denominator enumeration (captured by the baseline replay at
// its own decision point), baseline + per-candidate whole-node replays, and
// dataset row emission are implemented in worker_snapshot.cpp; all live state
// must be untouched on return (asserted in unit tests).
void on_internal_node_counterfactual(Search::Worker& liveWorker,
                                     Position&       pos,
                                     Search::Stack*  ss,
                                     Value           alpha,
                                     Value           beta,
                                     Depth           depth,
                                     Depth           rootDepth,
                                     Key             rootKey,
                                     bool            cutNode);

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
