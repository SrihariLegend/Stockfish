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

#include <cstdio>
#include <string>
#include <vector>

#include "../types.h"

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
    bool active() const { return active_; }

    // Called from the UCI `go` handler (main thread) before the search starts,
    // only when logging was actually requested for this root.
    void on_go(const std::string& fen, int targetDepth, const std::string& engineInfo);

    // Root lifecycle on the searching thread (1-thread protocol; the recording
    // thread never overlaps the UCI thread: `quit` only arrives after bestmove).
    void on_root_search_start(u64 rootKey);
    void on_root_search_end(u64 rootKey);

    // Decision hook at the moves_loop entry of an eligible node. Returns the
    // per-node sampling context (also assigns the node serial and, when
    // sampled, writes the DECISION_POINT record).
    MovesLoopCtx begin_moves_loop(u64  key,
                                  int  ply,
                                  int  depth,
                                  int  rootIterationDepth,
                                  int  alpha,
                                  int  beta,
                                  int  staticEval,
                                  bool improving,
                                  bool ttHit,
                                  bool ttMovePresent,
                                  int  rule50,
                                  int  sideToMove,
                                  const std::string& fen);

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

   private:
    void open_run();
    void flush();
    void write_frame(RecordType type, const std::vector<u8>& payload);
    void append_str(std::vector<u8>& out, const std::string& s);
    void reset_root();

    bool        requested_ = false;   // this run requested logging
    bool        active_    = false;   // armed and recording
    bool        overflow_  = false;   // collection cap reached
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

// Single process-wide recorder. All engine hooks are macro-gated, so this is
// only ever referenced (and the class instantiated) in POLICY_RESEARCH builds.
Recorder& recorder();

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH

#endif  // #ifndef POLICY_RESEARCH_LOG_H_INCLUDED
