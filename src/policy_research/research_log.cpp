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
    put_u16(buf_, static_cast<u16>(type));
    put_u16(buf_, DATA_SCHEMA_VERSION);
    put_u32(buf_, static_cast<u32>(payload.size()));
    buf_.insert(buf_.end(), payload.begin(), payload.end());
}

void Recorder::flush() {
    if (out_ == nullptr || buf_.empty())
        return;
    std::fwrite(buf_.data(), 1, buf_.size(), out_);
    std::fflush(out_);
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
        // Cannot open the log; run proceeds without logging (documented in
        // protocol P2.1). requested_ is cleared so nothing records.
        requested_ = false;
        active_ = false;
        return;
    }

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

    std::fwrite(header.data(), 1, header.size(), out_);

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

void Recorder::on_go(const std::string& fen, int targetDepth, const std::string& engineInfo) {
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
}

MovesLoopCtx Recorder::begin_moves_loop(u64  key,
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
                                        const std::string& fen) {
    if (!active_ || !rootOpen_)
        return MovesLoopCtx{};

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
    put_u16(p, static_cast<u16>(rule50));
    put_u8(p, static_cast<u8>(sideToMove));
    append_str(p, fen);
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
    if (!active_ || !rootOpen_ || !ctx.sampled || ctx.rootKey != rootKey_)
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
    // or was fully searched without raising alpha. Unsearched moves never get
    // an outcome here because they never reach this hook.
    const u8 outcome = stopped ? 3 : valueReturned >= betaBefore ? 1 : 2;
    put_u8(p, outcome);
    write_frame(REC_MOVE_ATTEMPT, p);
    ++rootAttemptCount_;
    ++rootDataRecords_;
}

void Recorder::on_run_end() {
    if (!requested_ && !active_)
        return;
    // Close any open root so the stream always terminates cleanly.
    if (active_ && rootOpen_)
        on_root_search_end(rootKey_);

    std::vector<u8> p;
    put_u64(p, runDecisionTotal_);
    put_u64(p, runAttemptTotal_);
    put_u8(p, runOverflow_ ? 1 : 0);
    put_u8(p, 0);
    write_frame(REC_RUN_END, p);
    flush();

    if (out_ != nullptr)
        std::fclose(out_);
    out_ = nullptr;
    requested_ = false;
    active_ = false;
    runDataRecords_ = 0;
    runDecisionTotal_ = 0;
    runAttemptTotal_ = 0;
    runOverflow_ = false;
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH
