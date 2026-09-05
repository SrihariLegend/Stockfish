/*
  Alpha-beta proof-policy research (policy research project).

  Phase 2 recorder implementation. See research_log.h and
  docs/policy-research/data-schema.md. Whole file is compiled out unless
  POLICY_RESEARCH is defined.
*/

#include "research_log.h"

#ifdef POLICY_RESEARCH

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <sstream>

#include "../misc.h"
#include "../position.h"
#include "research_options.h"

namespace Stockfish::Research {

namespace {

constexpr u32 SAMPLE_SPACE = 1000000;  // data-schema.md: sample when hash % 1e6 < threshold

// SplitMix64 finalizer. Deterministic; used for both identity mixing and the
// (thresholded) sample decision.
inline u64 mix(u64 z) {
    z += 0x9E3779B97F4A7C15ULL;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

inline u32 sample_threshold(double rate) {
    rate = std::clamp(rate, 0.0, 1.0);
    return static_cast<u32>(rate * SAMPLE_SPACE + 0.5);
}

inline void put_u8(std::vector<u8>& o, u8 v) { o.push_back(v); }

inline void put_u16(std::vector<u8>& o, u16 v) {
    o.push_back(static_cast<u8>(v & 0xFF));
    o.push_back(static_cast<u8>((v >> 8) & 0xFF));
}

inline void put_u32(std::vector<u8>& o, u32 v) {
    put_u16(o, static_cast<u16>(v & 0xFFFF));
    put_u16(o, static_cast<u16>((v >> 16) & 0xFFFF));
}

inline void put_u64(std::vector<u8>& o, u64 v) {
    put_u32(o, static_cast<u32>(v & 0xFFFFFFFF));
    put_u32(o, static_cast<u32>((v >> 32) & 0xFFFFFFFF));
}

inline void put_i32(std::vector<u8>& o, int v) {
    // Two's complement little-endian, explicit.
    const u32 uv = static_cast<u32>(v);
    put_u32(o, uv);
}

}  // namespace

Recorder& recorder() {
    static Recorder r;
    return r;
}

void Recorder::append_str(std::vector<u8>& out, const std::string& s) {
    put_u16(out, static_cast<u16>(s.size()));
    out.insert(out.end(), s.begin(), s.end());
}

void Recorder::write_frame(RecordType type, const std::vector<u8>& payload) {
    if (ioFailed_)
        return;  // artifact already lost; stop buffering
    put_u16(buf_, static_cast<u16>(type));
    put_u16(buf_, DATA_SCHEMA_VERSION);
    put_u32(buf_, static_cast<u32>(payload.size()));
    buf_.insert(buf_.end(), payload.begin(), payload.end());
}

void Recorder::flush() {
    if (out_ == nullptr || buf_.empty() || ioFailed_)
        return;
    const std::size_t written = std::fwrite(buf_.data(), 1, buf_.size(), out_);
    if (written != buf_.size() || std::fflush(out_) != 0)
    {
        // The on-disk artifact can no longer be trusted; tell the user instead
        // of silently truncating the dataset.
        ioFailed_ = true;
        sync_cout << "info string policy research: log write failed; "
                     "logging disabled for the rest of this run" << sync_endl;
    }
    buf_.clear();
}

void Recorder::reset_root() {
    rootKey_ = 0;
    nodeSerial_ = 0;
    attemptSerial_ = 0;
    rootDataRecords_ = 0;
    rootDecisionCount_ = 0;
    rootAttemptCount_ = 0;
    overflow_ = false;
    buf_.clear();
    rootOpen_ = false;
}

void Recorder::open_run() {
    if (out_ != nullptr)
        return;
    out_ = std::fopen(logPath_.c_str(), "wb");
    if (out_ == nullptr)
    {
        // Logging cannot start; the search itself still proceeds. Surface the
        // failure instead of failing silently.
        sync_cout << "info string policy research: cannot open log file '" << logPath_
                  << "'; logging disabled for this search" << sync_endl;
        requested_ = false;
        active_ = false;
        return;
    }
    ioFailed_ = false;

    std::vector<u8> header;
    // magic "PXRLOG" + version byte + pad
    header.insert(header.end(), {'P', 'X', 'R', 'L', 'O', 'G', 0x01, 0x00});
    put_u32(header, 0x01020304);       // endian marker
    put_u16(header, LOG_FORMAT_VERSION);
    // Run UUID (header only; never compared across runs). Best-effort random.
    static bool seeded = false;
    if (!seeded)
    {
        seeded = true;
        std::srand(static_cast<unsigned>(
          std::chrono::steady_clock::now().time_since_epoch().count()));
    }
    const u32 uuidLen = 8;
    put_u32(header, uuidLen);
    u64 r = (static_cast<u64>(std::rand()) << 32) | static_cast<u32>(std::rand());
    for (u32 i = 0; i < uuidLen; ++i)
        put_u8(header, static_cast<u8>(r >> (8 * i)));

    if (std::fwrite(header.data(), 1, header.size(), out_) != header.size())
    {
        ioFailed_ = true;
        sync_cout << "info string policy research: failed writing log header; "
                     "logging disabled for this search" << sync_endl;
        std::fclose(out_);
        out_ = nullptr;
        requested_ = false;
        active_ = false;
        return;
    }

    std::vector<u8> p;
    put_u8(p, static_cast<u8>(config().mode));
    put_u64(p, seed_);
    put_u32(p, threshold_);
    put_u32(p, cap_);
    append_str(p, policyVersion_);
    append_str(p, engineInfo_);
    buf_.clear();
    write_frame(REC_RUN_START, p);
    flush();
}

void Recorder::finish_run() {
    // Caller holds m_.
    if (out_ == nullptr && !requested_ && !active_)
        return;
    // Close any open root so the stream always terminates cleanly.
    if (active_ && rootOpen_)
        on_root_search_end(rootKey_);

    std::vector<u8> p;
    put_u64(p, runDecisionTotal_);
    put_u64(p, runAttemptTotal_);
    put_u8(p, runOverflow_ ? 1 : 0);
    // error code 1 means "cap overflow" (RUN_END layout); it must agree with
    // the overflow flag so decoders can cross-check the stream.
    put_u8(p, runOverflow_ ? 1 : 0);
    write_frame(REC_RUN_END, p);
    flush();

    if (out_ != nullptr)
    {
        if (std::fclose(out_) != 0)
        {
            ioFailed_ = true;
            sync_cout << "info string policy research: error closing log file" << sync_endl;
        }
        out_ = nullptr;
    }
    requested_ = false;
    active_ = false;
    ioFailed_ = false;
    runDataRecords_ = 0;
    runDecisionTotal_ = 0;
    runAttemptTotal_ = 0;
    runOverflow_ = false;
}

void Recorder::on_go(const std::string& fen, int targetDepth, const std::string& engineInfo) {
    // A `go` may arrive (UCI thread) while the previous root search is still in
    // flight. The engine serializes the searches themselves (start_thinking waits
    // for the running search), but this handler can still run concurrently with
    // the searching thread's on_root_search_end(), so the defer hand-off below is
    // serialized by m_: the root close either observes this deferred request and
    // applies it, or runs first and this path finalizes/arms directly. No
    // torn/lost request either way, and option/path/switch changes made after
    // this `go` apply to the next search. Protocol P2.1 uses a fresh engine
    // process per root, so a process normally sees one `go` and this path is
    // only reached interactively.
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (active_)
    {
        defer_ = true;
        deferFen_ = fen;
        deferDepth_ = targetDepth > 0 ? targetDepth : 0;
        deferEngineInfo_ = engineInfo;
        return;
    }
    // A previous `go` may have left its run file open (interactive sessions can
    // issue several `go` commands, change options, or disable research between
    // searches). Finalize it so every `go` produces a self-contained, decodable
    // run and option/path/seed changes take effect immediately.
    if (out_ != nullptr)
        finish_run();
    arm_run(fen, targetDepth, engineInfo);
}

// Caller holds m_ (on_go or on_root_search_end).
void Recorder::arm_run(const std::string& fen, int targetDepth,
                       const std::string& engineInfo) {
    requested_ = false;
    active_ = false;
    if (!Research::enabled() || config().mode != Research::Mode::Observational
        || config().logPath.empty())
        return;

    logPath_ = config().logPath;
    seed_ = config().seed;
    threshold_ = sample_threshold(config().sampleRate);
    cap_ = config().maxRecords > 0 ? std::min(static_cast<u32>(config().maxRecords),
                                              HARD_MAX_RECORDS)
                                   : HARD_MAX_RECORDS;
    policyVersion_ = config().policyVersion;
    engineInfo_ = engineInfo;
    pendingFen_ = fen;
    pendingDepth_ = targetDepth > 0 ? static_cast<u64>(targetDepth) : 0;

    requested_ = true;
    open_run();  // may clear requested_ on failure
    active_ = requested_;
}

void Recorder::on_root_search_start(u64 rootKey) {
    // Takes m_ so arming can never interleave with a concurrent on_go()/
    // on_run_end() on the UCI thread (e.g. the next `go` arriving while the
    // previous root is closing).
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (!requested_ || pendingFen_.empty())
        return;
    reset_root();
    rootKey_ = rootKey;
    active_ = true;
    rootOpen_ = true;

    std::vector<u8> p;
    put_u64(p, rootKey_);
    put_u32(p, static_cast<u32>(pendingDepth_));
    append_str(p, pendingFen_);
    write_frame(REC_ROOT_START, p);
    pendingFen_.clear();
    pendingDepth_ = 0;
}

void Recorder::on_root_search_end(u64 rootKey) {
    // Takes m_: a concurrent on_go() may be deferring the next request while this
    // root closes; the lock orders the two so the defer is either consumed here
    // (finalize + re-arm under the current options) or handled by on_go() after
    // the close. finish_run() may re-enter on_root_search_end() (shutdown path),
    // hence the recursive mutex.
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (!active_ || !rootOpen_ || rootKey != rootKey_)
        return;
    std::vector<u8> p;
    put_u64(p, rootKey_);
    put_u64(p, rootDecisionCount_);
    put_u64(p, rootAttemptCount_);
    write_frame(REC_ROOT_END, p);

    runDataRecords_ += rootDataRecords_;
    runDecisionTotal_ += rootDecisionCount_;
    runAttemptTotal_ += rootAttemptCount_;
    runOverflow_ = runOverflow_ || overflow_;
    flush();
    reset_root();
    active_ = false;  // next root re-arms via on_go

    // A `go` arrived while this root was still searching; the engine has now
    // finished this search and will start the deferred one. Finalize this run
    // and arm for the deferred request under the *current* options.
    if (defer_)
    {
        defer_ = false;
        if (out_ != nullptr)
            finish_run();
        arm_run(deferFen_, deferDepth_, deferEngineInfo_);
        deferFen_.clear();
        deferDepth_ = 0;
        deferEngineInfo_.clear();
    }
}

MovesLoopCtx Recorder::begin_moves_loop(const Position& pos,
                                        int  ply,
                                        int  depth,
                                        int  rootIterationDepth,
                                        int  alpha,
                                        int  beta,
                                        int  staticEval,
                                        bool improving,
                                        bool ttHit,
                                        bool ttMovePresent) {
    if (!active() || !rootOpen_)
        return MovesLoopCtx{};

    const u64 key = pos.key();
    const u64 serial = ++nodeSerial_;

    // Deterministic sample identity (data-schema.md): ordered mix of search
    // state; no mutable RNG anywhere on this path.
    u64 h = seed_;
    h = mix(h ^ rootKey_);
    h = mix(h ^ key);
    h = mix(h ^ static_cast<u64>(static_cast<u32>(ply)));
    h = mix(h ^ static_cast<u64>(static_cast<u32>(depth)));
    h = mix(h ^ static_cast<u64>(static_cast<u32>(rootIterationDepth)));
    h = mix(h ^ serial);

    MovesLoopCtx ctx;
    ctx.rootKey = rootKey_;
    ctx.nodeSerial = serial;

    const bool sampled = !overflow_ && rootDataRecords_ < cap_
                      && buf_.size() < HARD_MAX_BYTES && (h % SAMPLE_SPACE) < threshold_;
    if (!sampled)
        return ctx;

    std::vector<u8> p;
    put_u64(p, rootKey_);
    put_u64(p, serial);
    put_u64(p, key);
    put_i32(p, ply);
    put_i32(p, depth);
    put_i32(p, rootIterationDepth);
    put_i32(p, alpha);
    put_i32(p, beta);
    put_i32(p, staticEval);
    u8 flags = static_cast<u8>((improving ? 1 : 0) | (ttHit ? 2 : 0) | (ttMovePresent ? 4 : 0));
    put_u8(p, flags);
    put_u16(p, static_cast<u16>(pos.rule50_count()));
    put_u8(p, static_cast<u8>(pos.side_to_move()));
    append_str(p, pos.fen());  // lazy: only sampled nodes pay FEN formatting
    write_frame(REC_DECISION_POINT, p);
    ++rootDecisionCount_;
    ++rootDataRecords_;

    if (rootDataRecords_ >= cap_ || buf_.size() >= HARD_MAX_BYTES)
    {
        overflow_ = true;
        std::vector<u8> e;
        put_u16(e, 1);  // code: collection cap reached
        const std::string msg = "research record cap reached (maxRecords or byte budget)";
        put_u16(e, static_cast<u16>(msg.size()));
        e.insert(e.end(), msg.begin(), msg.end());
        write_frame(REC_ERROR_RECORD, e);
    }

    ctx.sampled = true;
    return ctx;
}

void Recorder::log_move_attempt(const MovesLoopCtx& ctx,
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
                                bool stopped) {
    if (!active() || !rootOpen_ || !ctx.sampled
        || ctx.rootKey != rootKey_)
        return;

    if (overflow_ || rootDataRecords_ >= cap_ || buf_.size() >= HARD_MAX_BYTES)
    {
        if (!overflow_)
        {
            overflow_ = true;
            std::vector<u8> e;
            put_u16(e, 1);
            const std::string msg = "research record cap reached (maxRecords or byte budget)";
            put_u16(e, static_cast<u16>(msg.size()));
            e.insert(e.end(), msg.begin(), msg.end());
            write_frame(REC_ERROR_RECORD, e);
        }
        return;
    }

    const u64 attemptSerial = ++attemptSerial_;

    std::vector<u8> p;
    put_u64(p, rootKey_);
    put_u64(p, ctx.nodeSerial);
    put_u64(p, attemptSerial);
    put_u16(p, moveRaw);
    put_u16(p, static_cast<u16>(std::max(quietOrdinal, 0)));
    put_u16(p, static_cast<u16>(std::max(totalAttempted, 0)));
    put_u8(p, givesCheck ? 1 : 0);
    put_u8(p, isTTmove ? 1 : 0);
    put_u8(p, static_cast<u8>(std::clamp(childCount, 0, 255)));
    put_i32(p, firstDepth);
    put_i32(p, reSearchDepth);
    put_i32(p, alphaBefore);
    put_i32(p, betaBefore);
    put_i32(p, valueReturned);
    put_u64(p, nodesConsumed);
    // Outcome: 1 fail-high cutoff, 2 fail-low, 3 aborted. A quiet attempt at a
    // non-root NonPV null-window node either proves (value >= beta, loop ends)
    // or was searched without reaching beta (exact negative event at the
    // recorded child depths; data-schema.md). Unsearched moves never get an
    // outcome here because they never reach this hook.
    const u8 outcome = stopped ? 3 : valueReturned >= betaBefore ? 1 : 2;
    put_u8(p, outcome);
    write_frame(REC_MOVE_ATTEMPT, p);
    ++rootAttemptCount_;
    ++rootDataRecords_;
}

void Recorder::on_run_end() {
    std::lock_guard<std::recursive_mutex> lk(m_);
    finish_run();
}

// ---------------------------------------------------------------------------
// InternalDatasetLog (internal-counterfactual/3 or /4, JSONL)
//
// One self-contained dataset file per root. Row content is JSONL: numeric and
// constrained string fields only (FEN and UCI move strings never contain
// control characters, quotes, or backslashes), so json_quote_append below only
// needs to guard against those three characters for robustness.
// ---------------------------------------------------------------------------

// Schema version of the internal counterfactual dataset rows for the current
// run: "internal-counterfactual/3" (force-next form) or "/4" when
// PolicyResearchPermBattery is on (rows additionally carry the plan-11.3
// shared-permutation battery). All rows of one run share the schema string so
// validators can accept a whole file uniformly.
const char* internal_dataset_schema() {
    return config().permBattery ? "internal-counterfactual/5"
                                : "internal-counterfactual/3";
}

InternalDatasetLog& internal_log() {
    static InternalDatasetLog log;
    return log;
}

namespace {

inline void json_quote_append(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s)
    {
        if (c == '"' || c == '\\')
            out += '\\';
        if (c == '\n')
            out += "\\n";
        else if (c == '\r')
            out += "\\r";
        else
            out += c;
    }
    out += '"';
}

inline std::string json_num(u64 v) {
    return std::to_string(v);
}

inline std::string json_double(double v) {
    // Fixed 6 decimals: probabilities and rates serialize exactly and
    // deterministically across platforms.
    std::ostringstream os;
    os.setf(std::ios::fixed);
    os.precision(6);
    os << v;
    return os.str();
}

inline std::string json_bool(bool b) {
    return b ? std::string("true") : std::string("false");
}

}  // namespace

// Pure close/reset used when a previous run left its file open (interactive
// sessions): no rows are emitted here; an open root would already have been
// closed by its own on_root_search_end().
void InternalDatasetLog::finish_run() {
    if (out_ != nullptr)
    {
        std::fclose(out_);
        out_ = nullptr;
    }
    requested_ = false;
    active_    = false;
    rootOpen_  = false;
}

void InternalDatasetLog::on_go(const std::string& fen, int targetDepth, u64 targetNodes,
                               const std::string& engineInfo) {
    // Same deferral model as Recorder::on_go: a `go` may arrive on the UCI
    // thread while the previous root search is still closing; the lock orders
    // the hand-off so no request is lost or torn.
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (active_)
    {
        defer_           = true;
        deferFen_        = fen;
        deferDepth_      = targetDepth > 0 ? targetDepth : 0;
        deferNodes_      = targetNodes > 0 ? targetNodes : 0;
        deferEngineInfo_ = engineInfo;
        return;
    }
    if (out_ != nullptr)
        finish_run();
    arm_run(fen, targetDepth, targetNodes, engineInfo);
}

void InternalDatasetLog::arm_run(const std::string& fen, int targetDepth, u64 targetNodes,
                                 const std::string& engineInfo) {
    requested_ = false;
    active_    = false;
    if (!Research::enabled() || config().mode != Research::Mode::InternalCounterfactual
        || config().logPath.empty())
        return;

    logPath_      = config().logPath;
    seed_         = config().seed;
    sampleRate_   = config().sampleRate;
    topK_         = config().topK;
    nodeBudget_   = effective_node_budget(config());
    effCap_       = config().maxRecords > 0
                      ? std::min(static_cast<u32>(config().maxRecords), HARD_MAX_RECORDS)
                      : HARD_MAX_RECORDS;
    engineInfo_   = engineInfo;
    pendingFen_   = fen;
    pendingDepth_ = targetDepth > 0 ? targetDepth : 0;
    pendingNodes_ = targetNodes > 0 ? targetNodes : 0;
    overflow_     = false;
    ioFailed_     = false;
    rows_         = 0;
    bytes_        = 0;

    requested_ = true;
    open_run();  // may clear requested_ on failure
    active_    = requested_;
}

void InternalDatasetLog::open_run() {
    if (out_ != nullptr)
        return;
    out_ = std::fopen(logPath_.c_str(), "wb");
    if (out_ == nullptr)
    {
        // Collection cannot start; the search itself still proceeds. Surface
        // the failure instead of failing silently.
        sync_cout << "info string policy research: cannot open dataset file '" << logPath_
                  << "'; internal counterfactual collection disabled for this search"
                  << sync_endl;
        requested_ = false;
        active_    = false;
        return;
    }
}

void InternalDatasetLog::on_root_search_start(u64 rootKey) {
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (!requested_ || pendingFen_.empty())
        return;
    rootKey_  = rootKey;
    sampleId_ = 0;  // sample ids are run-scoped: reset at every root start
    rootOpen_ = true;
    active_   = true;

    // run_start row: provenance + sampler configuration for this file.
    {
        std::string row;
        row += "{\"schema\":\"" + std::string(internal_dataset_schema())
              + "\",\"type\":\"run_start\"";
        row += ",\"engine\":";
        json_quote_append(row, engineInfo_);
        row += ",\"seed\":" + json_num(seed_);
        row += ",\"sample_rate\":" + json_double(sampleRate_);
        row += ",\"top_k\":" + json_num(topK_ > 0 ? u64(topK_) : u64(0));
        row += ",\"node_budget\":" + json_num(nodeBudget_);
        row += ",\"max_records\":" + json_num(effCap_);
        row += "}";
        emit_row_locked(row, false);  // lifecycle row: bypasses the decision cap
    }
    // root_start row: root identity + start position + collection targets.
    {
        std::string row;
        row += "{\"schema\":\"" + std::string(internal_dataset_schema())
              + "\",\"type\":\"root_start\"";
        row += ",\"root_key\":" + json_num(rootKey_);
        row += ",\"target_depth\":" + json_num(u64(pendingDepth_));
        row += ",\"target_nodes\":" + json_num(pendingNodes_);
        row += ",\"fen\":";
        json_quote_append(row, pendingFen_);
        row += "}";
        emit_row_locked(row, false);  // lifecycle row: bypasses the decision cap
    }
    // The collection targets (pendingDepth_/pendingNodes_) are intentionally
    // kept: the always-written root_end row reports them when the root
    // finishes. Only the start position is consumed here.
    pendingFen_.clear();
}

void InternalDatasetLog::on_root_search_end(u64                 rootKey,
                                            u64                 achievedDepth,
                                            u64                 searchedNodes,
                                            const std::string&  bestMove,
                                            int                 bestValueRaw) {
    std::lock_guard<std::recursive_mutex> lk(m_);
    // root_end is always written for an opened root -- including after the
    // decision-row or byte caps raised overflow_ (overflow_ no longer clears
    // active_, so the final accounting row always survives). The record and
    // byte caps apply to decision rows only, and overflow/io-failure state is
    // reported inside this very row.
    if (!rootOpen_ || rootKey != rootKey_ || out_ == nullptr)
        return;

    {
        const u64 targetDepth = pendingDepth_;
        const u64 targetNodes = pendingNodes_;
        const bool completed =
          targetDepth > 0 ? achievedDepth >= targetDepth
                          : (targetNodes > 0 ? searchedNodes >= targetNodes : true);

        // "bytes" is defined as the file size AFTER this row (the last row of
        // the file), so it is computed by fixed-point iteration: the row
        // length depends on the digit count of the claim, and the claim must
        // equal bytes_ + row length + 1. The iteration is monotone and
        // converges within 2-3 passes (digit-count changes at most once per
        // pass); the loop guard is pure defensive.
        std::string row;
        const auto build = [&](u64 bytesClaim) {
            row = "{\"schema\":\"" + std::string(internal_dataset_schema())
                  + "\",\"type\":\"root_end\"";
            row += ",\"root_key\":" + json_num(rootKey_);
            row += ",\"rows\":" + json_num(rows_);
            row += ",\"bytes\":" + json_num(bytesClaim);
            row += ",\"overflow\":" + json_bool(overflow_);
            row += ",\"io_failed\":" + json_bool(ioFailed_);
            row += ",\"target_depth\":" + json_num(targetDepth);
            row += ",\"achieved_depth\":" + json_num(achievedDepth);
            row += ",\"target_nodes\":" + json_num(targetNodes);
            row += ",\"searched_nodes\":" + json_num(searchedNodes);
            row += ",\"completed\":" + json_bool(completed);
            row += ",\"best_move\":";
            if (bestMove.empty())
                row += "null";
            else
                json_quote_append(row, bestMove);
            row += ",\"best_value\":"
                   + (bestValueRaw == VALUE_NONE
                        ? std::string("null")
                        : (bestValueRaw >= 0 ? json_num(u64(bestValueRaw))
                                             : "-" + json_num(u64(-i64(bestValueRaw)))));
            row += "}";
        };
        u64 claim = bytes_;
        for (int pass = 0; pass < 4; ++pass)
        {
            build(claim);
            const u64 next = bytes_ + row.size() + 1;  // + newline
            if (next == claim)
                break;
            claim = next;
        }
        emit_row_locked(row, false);  // lifecycle row: always written
    }

    rootOpen_  = false;
    active_    = false;
    requested_ = false;
    pendingFen_.clear();
    pendingDepth_ = 0;
    pendingNodes_ = 0;
    if (out_ != nullptr)
    {
        if (std::fflush(out_) != 0)
        {
            ioFailed_ = true;
            sync_cout << "info string policy research: error flushing dataset file" << sync_endl;
        }
        if (std::fclose(out_) != 0)
        {
            ioFailed_ = true;
            sync_cout << "info string policy research: error closing dataset file" << sync_endl;
        }
        out_ = nullptr;
    }

    // A `go` arrived while this root was still searching; apply it now under
    // the current options.
    if (defer_)
    {
        defer_ = false;
        arm_run(deferFen_, deferDepth_, deferNodes_, deferEngineInfo_);
        deferFen_.clear();
        deferDepth_      = 0;
        deferNodes_      = 0;
        deferEngineInfo_.clear();
    }
}

void InternalDatasetLog::on_run_end() {
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (active_ && rootOpen_)
        on_root_search_end(rootKey_);
    if (out_ != nullptr)
    {
        std::fclose(out_);
        out_ = nullptr;
    }
    requested_ = false;
    active_    = false;
    rootOpen_  = false;
}

bool InternalDatasetLog::write_row(const std::string& jsonLine, bool decisionRow) {
    std::lock_guard<std::recursive_mutex> lk(m_);
    if (!active_ || ioFailed_ || overflow_)
        return false;
    emit_row_locked(jsonLine, decisionRow);
    return !ioFailed_ && !overflow_;
}

void InternalDatasetLog::emit_row_locked(const std::string& line, bool countsAgainstCap) {
    // Caller holds m_. The decision-row cap (effCap_) and the hard byte budget
    // apply to decision rows only; hitting either stops further decision rows
    // (would_record()/write_row() see overflow_) and raises overflow_, which
    // the always-written root_end row reports. Lifecycle rows
    // (run_start/root_start/root_end) and node_exit audit rows are rare and
    // tiny, so they bypass the caps; root_end in particular must never be
    // dropped, because it carries the overflow/io_failed accounting for the
    // whole file. Note that overflow_ deliberately does NOT clear active_: the
    // collection is still "open" so the root lifecycle (including the final
    // root_end row) completes normally; only the sampling gates go quiet.
    if (countsAgainstCap
        && (ioFailed_ || rows_ >= effCap_ || bytes_ + line.size() + 1 > HARD_MAX_BYTES))
    {
        if (!overflow_)
            sync_cout << "info string policy research: internal counterfactual dataset cap "
                         "reached; collection disabled for the rest of this search"
                      << sync_endl;
        overflow_ = true;
        return;
    }

    if (out_ == nullptr)
    {
        ioFailed_ = true;
        sync_cout << "info string policy research: dataset file not open; collection disabled"
                  << sync_endl;
        return;
    }
    const std::size_t written = std::fwrite(line.data(), 1, line.size(), out_);
    const int         newline = std::fputc('\n', out_);
    if (written != line.size() || newline == EOF)
    {
        ioFailed_ = true;
        sync_cout << "info string policy research: dataset write failed; collection disabled "
                     "for the rest of this run"
                  << sync_endl;
        return;
    }
    if (countsAgainstCap)
        rows_ += 1;
    bytes_ += line.size() + 1;
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH
