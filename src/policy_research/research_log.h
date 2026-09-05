/*
  Alpha-beta proof-policy research (policy research project).

  Phase 2: versioned research logging (record-only). Compiled only when
  POLICY_RESEARCH is defined; with the macro undefined this header defines
  nothing and no production behavior or compile cost exists.

  Recorder + deterministic sampling + length-prefixed binary serialization for
  observational data on the survival outcomes of quiet moves. Consumes
  Research::config() (research_options.h). Hot-path hooks live in
  search.cpp (moves loop) and uci.cpp (root lifecycle); collection is
  in-memory only and flushed after each root search. See
  docs/policy-research/data-schema.md for the authoritative layout.

  Determinism contract: sampling uses no mutable RNG; sample identities are a
  pure function of (rootKey, positionKey, ply, depth, rootIterationDepth,
  nodeSerial, seed). Logging never reads back into search decisions, so a
  logging-enabled run and a logging-disabled run of the same executable search
  identically (see experiment-protocols.md P2.1).
*/

#ifndef POLICY_RESEARCH_LOG_H_INCLUDED
#define POLICY_RESEARCH_LOG_H_INCLUDED

#ifdef POLICY_RESEARCH

#include <atomic>
#include <cstdio>
#include <mutex>
#include <string>
#include <vector>

#include "../types.h"
#include "research_options.h"
#include "scoped_probe.h"

namespace Stockfish {
class Position;  // used by Recorder::begin_moves_loop (fen only when sampled)
}

namespace Stockfish::Research {

// Record types (research-data/1). Stable ids; see data-schema.md.
enum RecordType : u16 {
    REC_RUN_START             = 1,
    REC_ROOT_START            = 2,
    REC_DECISION_POINT        = 3,
    REC_MOVE_ATTEMPT          = 4,
    REC_COUNTERFACTUAL_RESULT = 5,  // reserved for later phases; never emitted yet
    REC_ROOT_END              = 6,
    REC_RUN_END               = 7,
    REC_ERROR_RECORD          = 8
};

constexpr u16 LOG_FORMAT_VERSION = 1;  // research-log/1 (file container)
constexpr u16 DATA_SCHEMA_VERSION = 1;  // research-data/1 (record payloads)

// Finite guard rails: collection can never be unbounded, even when
// PolicyResearchMaxRecords is 0 ("not set"). The option value caps the record
// count; the byte budget caps in-memory use per root independently.
constexpr u32 HARD_MAX_RECORDS = 1u << 22;           // ~4.2M records
constexpr u64 HARD_MAX_BYTES   = 256u << 20;         // 256 MiB per root buffer

// Per-node sampling context carried in the search stack frame so recursive
// children can never clobber it.
struct MovesLoopCtx {
    bool sampled = false;
    u64   nodeSerial = 0;
    u64   rootKey = 0;
};

// Accumulates what a single attempted move actually spent in the search
// (child search calls at this engine's non-PV move loop). Value semantics;
// declared per move attempt on the search stack.
struct AttemptAccum {
    u64   startNodes  = 0;
    int   childCount  = 0;
    int   firstDepth  = 0;
    int   reSearchDepth = -1;

    void note(int childDepth) {
        if (childCount == 0)
            firstDepth = childDepth;
        else
            reSearchDepth = childDepth;
        ++childCount;
    }
};

class Recorder {
   public:
    bool active() const {
        return active_.load(std::memory_order_relaxed) && !is_shadow_probe_active();
    }

    // Called from the UCI `go` handler (main thread) before the search starts,
    // only when logging was actually requested for this root. Finalizes any
    // still-open run from an earlier `go` first, so every `go` produces its own
    // self-contained run file (protocol P2.1 uses one fresh process per root).
    void on_go(const std::string& fen, int targetDepth, const std::string& engineInfo);

    // Root lifecycle on the main searching thread. The UCI thread (on_go /
    // on_run_end) can run concurrently while a root is in flight, so state
    // transitions shared with on_go (active_, defer_*) are serialized by m_.
    void on_root_search_start(u64 rootKey);
    void on_root_search_end(u64 rootKey);

    // Decision hook at the moves_loop entry of an eligible node. Returns the
    // per-node sampling context (also assigns the node serial and, when
    // sampled, writes the DECISION_POINT record). The position is only read for
    // key/rule50/stm (cheap); the FEN string is formatted lazily, exclusively
    // for sampled nodes, so un-sampled nodes never pay string building.
    MovesLoopCtx begin_moves_loop(const Position& pos,
                                  int  ply,
                                  int  depth,
                                  int  rootIterationDepth,
                                  int  alpha,
                                  int  beta,
                                  int  staticEval,
                                  bool improving,
                                  bool ttHit,
                                  bool ttMovePresent);

    // Attempt hook after a searched quiet move returns (before the stop check).
    void log_move_attempt(const MovesLoopCtx& ctx,
                          u16 moveRaw,
                          bool givesCheck,
                          bool isTTmove,
                          int  quietOrdinal,
                          int  totalAttempted,
                          int  childCount,
                          int  firstDepth,
                          int  reSearchDepth,
                          int  alphaBefore,
                          int  betaBefore,
                          int  valueReturned,
                          u64  nodesConsumed,
                          bool stopped);

    // Engine shutdown (UCI `quit`). Writes RUN_END and closes the file.
    void on_run_end();

    // Inspection accessors for testing and diagnostics
    u64  get_run_data_records() const { return runDataRecords_; }
    u64  get_run_decision_total() const { return runDecisionTotal_; }
    u64  get_run_attempt_total() const { return runAttemptTotal_; }
    bool is_root_open() const { return rootOpen_; }

    // Test hooks to verify scoped suppression under simulated active recording
    void test_arm_for_unit_tests(u64 dummyRootKey) {
        std::lock_guard<std::recursive_mutex> lock(m_);
        active_.store(true, std::memory_order_relaxed);
        rootOpen_ = true;
        rootKey_  = dummyRootKey;
    }
    void test_disarm_for_unit_tests() {
        std::lock_guard<std::recursive_mutex> lock(m_);
        active_.store(false, std::memory_order_relaxed);
        rootOpen_ = false;
        rootKey_  = 0;
    }

   private:
    void open_run();
    void finish_run();
    void arm_run(const std::string& fen, int targetDepth, const std::string& engineInfo);
    void flush();
    void write_frame(RecordType type, const std::vector<u8>& payload);
    void append_str(std::vector<u8>& out, const std::string& s);
    void reset_root();

    // Thread safety: on_go()/on_run_end() run on the UCI thread; the root
    // lifecycle and per-node hooks run on the main searching thread. A `go` can
    // arrive (UCI thread) while the searching thread is closing the previous
    // root, so every transition touching the shared run/defer state below takes
    // m_. std::recursive_mutex because finish_run() may close a still-open root
    // by calling on_root_search_end() on the shutdown path. active_ is
    // additionally atomic because the per-node hooks read it without taking the
    // lock on the hot path -- safe, since no other thread writes active_ while a
    // root is searching (on_go only defers then, it never re-arms).
    std::recursive_mutex m_;

    bool        requested_ = false;   // this run requested logging
    std::atomic<bool> active_ = false;  // armed and recording (lock-free hot reads)
    bool        overflow_  = false;   // collection cap reached
    bool        ioFailed_  = false;   // a write failed; the artifact is dead

    // Deferred re-arm: a `go` that arrived while the previous root search was
    // still active. The engine serializes searches (start_thinking waits for the
    // running search to finish), so the deferred request is applied when the
    // current root closes.
    bool        defer_             = false;
    std::string deferFen_;
    int         deferDepth_        = 0;
    std::string deferEngineInfo_;
    std::string logPath_;
    std::string pendingFen_;
    u64         pendingDepth_ = 0;
    std::string engineInfo_;

    u64  seed_     = 0;
    u32  threshold_ = 0;              // sample when hash % 1_000_000 < threshold_
    u32  cap_      = HARD_MAX_RECORDS;
    std::string policyVersion_;

    std::FILE*      out_ = nullptr;
    std::vector<u8> buf_;

    u64 rootKey_   = 0;
    u64 nodeSerial_ = 0;
    u64 attemptSerial_ = 0;
    u64 rootDataRecords_ = 0;
    u64 rootDecisionCount_ = 0;
    u64 rootAttemptCount_ = 0;
    u64 runDataRecords_ = 0;
    u64 runDecisionTotal_ = 0;
    u64 runAttemptTotal_ = 0;
    bool runOverflow_ = false;
    bool rootOpen_ = false;
};

// Internal counterfactual dataset (JSONL, schema internal-counterfactual/3).
//
// Phase 5: whole-node decision replays with slot-1 forced candidates are
// emitted as versioned JSONL rows. This artifact is SEPARATE from the
// observational binary Recorder above (which stays observational-only); the
// two can never both be armed because PolicyResearchMode selects exactly one
// mode per search. One self-contained dataset file per root (a `go` produces
// one file; interactive sessions finalize the previous file per root, exactly
// like the Recorder's run semantics). Row content is built by the sampling
// hook (worker_snapshot.cpp) and written here through write_row(); root and
// run rows are emitted here from the saved go-time provenance.
//
// Guard rails (finite defaults doctrine): collection stops at the effective
// record cap (PolicyResearchMaxRecords, else HARD_MAX_RECORDS) or the hard
// byte budget (HARD_MAX_BYTES); overflow is reported in the root_end row and
// sampling stops. write failures deactivate collection with an info string
// (never a silent truncation).
class InternalDatasetLog {
   public:
    bool active() const {
        return active_.load(std::memory_order_relaxed) && !is_shadow_probe_active();
    }

    // Hot-path pre-check used by the sampling hook before any probe work:
    // a decision row would actually be recorded (armed, caps not reached).
    bool would_record() const {
        return active_.load(std::memory_order_relaxed) && !ioFailed_ && !overflow_
               && rows_ < effCap_ && bytes_ < HARD_MAX_BYTES;
    }

    // Called when the sampling hook skipped a candidate because the row or
    // byte cap was already reached: the dataset is truncated relative to what
    // the sampler would have produced, and root_end must say so (overflow_).
    void note_row_skipped() {
        std::lock_guard<std::recursive_mutex> lk(m_);
        if (active_.load(std::memory_order_relaxed))
            overflow_ = true;
    }

    // UCI `go` handler (main/UCI thread), before the search starts. Only arms
    // when PolicyResearch is enabled with mode internal_counterfactual and a
    // non-empty log path; otherwise stays inactive (the UCI site prints the
    // diagnostic). Mirrors Recorder::on_go deferral so a `go` arriving while a
    // root is closing can never lose or tear a request. targetNodes (limits.nodes)
    // is 0 for depth-limited searches and vice versa; the root_end completion
    // accounting uses whichever target the armed run had.
    void on_go(const std::string& fen, int targetDepth, u64 targetNodes,
               const std::string& engineInfo);

    // Root lifecycle on the main searching thread (next to the Recorder calls
    // in search.cpp). Emits the run_start and root_start rows when armed.
    void on_root_search_start(u64 rootKey);

    // Emits the always-written root_end row with the run's final accounting.
    // achievedDepth is the root depth reached by the search, searchedNodes the
    // pool's total node count, bestMove/bestValue the final live root move and
    // raw internal Value (VALUE_NONE / empty when the root had no moves).
    void on_root_search_end(u64           rootKey,
                            u64           achievedDepth = 0,
                            u64           searchedNodes = 0,
                            const std::string& bestMove  = {},
                            int           bestValueRaw  = VALUE_NONE);

    // Engine shutdown (UCI `quit`). Closes any still-open root/file.
    void on_run_end();

    // Writes one complete JSONL row (hook-built content). Applies the record
    // and byte caps and the io-failure policy. decisionRow=true rows are
    // decision rows and consume the decision-row cap (rows_); node_exit and
    // lifecycle rows are audit rows that bypass the decision cap. Returns
    // false when the row was dropped (caps or io failure); sampling must stop
    // then.
    bool write_row(const std::string& jsonLine, bool decisionRow = true);

    // Inspection accessors for tests and diagnostics
    u64  rows_written() const { return rows_; }
    u64  bytes_written() const { return bytes_; }
    bool overflowed() const { return overflow_; }
    bool root_open() const { return rootOpen_; }
    u64  current_root_key() const { return rootKey_; }

    // Unique per-visit sample id: monotonic within a run, reset at every
    // root start. Sample ids are allocated by the sampling hook and written
    // to both the decision row and its node_exit audit row; they join a
    // visit even when the same (root, pos_key, ply, entry_depth, sample_seed)
    // identity recurs (repeated visits or collision), which the hash-based
    // seed cannot guarantee.
    u64 next_sample_id() {
        std::lock_guard<std::recursive_mutex> lock(m_);
        return ++sampleId_;
    }

    // Test hooks to exercise the real pipeline without UCI
    void test_arm_for_unit_tests(const std::string& path, u64 dummyRootKey) {
        std::lock_guard<std::recursive_mutex> lock(m_);
        logPath_ = path;
        out_     = std::fopen(path.c_str(), "wb");
        if (out_ == nullptr)
        {
            active_   = false;
            requested_ = false;
            return;
        }
        requested_ = true;
        rootKey_   = dummyRootKey;
        sampleId_  = 0;
        rootOpen_  = true;
        overflow_  = false;
        ioFailed_  = false;
        rows_      = 0;
        bytes_     = 0;
        effCap_    = HARD_MAX_RECORDS;
        active_.store(true, std::memory_order_relaxed);
    }
    void test_disarm_for_unit_tests() {
        std::lock_guard<std::recursive_mutex> lock(m_);
        active_.store(false, std::memory_order_relaxed);
        requested_ = false;
        rootOpen_  = false;
        rootKey_   = 0;
        if (out_ != nullptr)
        {
            std::fclose(out_);
            out_ = nullptr;
        }
    }

   private:
    void open_run();
    void finish_run();
    void arm_run(const std::string& fen, int targetDepth, u64 targetNodes,
                 const std::string& engineInfo);
    void emit_row_locked(const std::string& line, bool countsAgainstCap);

    // Same threading model as the Recorder: on_go()/on_run_end() run on the
    // UCI thread, root lifecycle and decision rows on the main searching
    // thread; every transition touching shared state takes m_ (recursive for
    // finish_run() -> on_root_search_end() on the shutdown path). active_ is
    // atomic for lock-free hot-path reads.
    std::recursive_mutex m_;
    std::atomic<bool> active_ = false;

    bool        requested_ = false;
    bool        overflow_  = false;
    bool        ioFailed_  = false;
    bool        rootOpen_  = false;
    bool        defer_     = false;
    std::string deferFen_, deferEngineInfo_;
    int         deferDepth_ = 0;
    u64         deferNodes_ = 0;
    std::string logPath_, pendingFen_, engineInfo_;
    int         pendingDepth_ = 0;   // target search depth (0 in node-limited runs)
    u64         pendingNodes_ = 0;   // target node count (0 in depth-limited runs)
    u64         seed_ = 0;
    double      sampleRate_ = 1.0;
    int         topK_ = 0;
    u64         nodeBudget_ = DEFAULT_NODE_BUDGET;
    u32         effCap_ = HARD_MAX_RECORDS;  // effective decision-row cap
    u64         rows_ = 0;                   // rows written in this file
    u64         bytes_ = 0;                  // bytes written in this file
    u64         sampleId_ = 0;               // per-visit id counter (reset per root)
    u64         rootKey_ = 0;
    std::FILE*  out_ = nullptr;
};

// Single process-wide internal dataset log (macro-gated builds only).
InternalDatasetLog& internal_log();

// Single process-wide recorder. All engine hooks are macro-gated, so this is
// only ever referenced (and the class instantiated) in POLICY_RESEARCH builds.
Recorder& recorder();

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH

#endif  // #ifndef POLICY_RESEARCH_LOG_H_INCLUDED
