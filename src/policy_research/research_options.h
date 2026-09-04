/*
  Alpha-beta proof-policy research (policy research project).

  Compile-time research gating and UCI option scaffolding. This header is only
  compiled when POLICY_RESEARCH is defined (e.g.
  `make build EXTRACXXFLAGS=-DPOLICY_RESEARCH`). With the macro undefined the
  file defines nothing and no production behavior or compile cost exists.

  Commit #3 of the research plan: "research compile flag and options with no
  behavior". The options below are registered and validated but nothing in the
  search reads them yet; later commits consume Research::config() under the same
  macro. See docs/policy-research/plan.md sections 2.2 and 7.1.

  Option semantics (documented):
  - PolicyResearch: master switch ('on'/'off'). 'off' disables all research
    behavior regardless of PolicyResearchMode.
  - PolicyResearchMode: active mode; meaningful only when PolicyResearch is
    'on'. 'off' disables research even when the master switch is 'on'.
  - Canonical predicate: Research::enabled() == (switchOn && mode != Mode::Off).
  - PolicyResearchMaxRecords: 0 means 'no explicit cap set'; the recorder MUST
    still enforce a finite internal hard cap (Phase 2) so collection can never
    be unbounded. The same rule applies to PolicyResearchTopK and
    PolicyResearchNodeBudget (0 = not set, not literally unlimited).
  - Validation errors are surfaced as 'info string' diagnostics AND the option
    value is rolled back to its previous value, so the option map and
    Research::config() never disagree (ucioption.cpp, POLICY_RESEARCH builds).
*/

#ifndef POLICY_RESEARCH_OPTIONS_H_INCLUDED
#define POLICY_RESEARCH_OPTIONS_H_INCLUDED

#ifdef POLICY_RESEARCH

#include <optional>
#include <string>
#include <cctype>
#include <cmath>
#include <cstdlib>

#include "../types.h"
#include "../ucioption.h"

namespace Stockfish::Research {

enum class Mode : u8 {
    Off,
    Observational,
    RootCounterfactual,
    InternalCounterfactual,
    SharedPermutation,
    JacobianDump,
    ReductionShadow
};

struct Config {
    bool         switchOn       = false;  // PolicyResearch master switch.
    Mode         mode           = Mode::Off;
    std::string  logPath;                  // ResearchLogPath.
    u64          seed           = 0;       // ResearchSeed.
    double       sampleRate     = 1.0;     // ResearchSampleRate (0 < r <= 1).
    int          maxRecords     = 0;       // ResearchMaxRecords (0 = unlimited).
    int          topK           = 0;       // ResearchTopK (0 = unlimited).
    u64          nodeBudget     = 0;       // ResearchNodeBudget (0 = unlimited).
    std::string  policyVersion;            // ResearchPolicyVersion.
    std::string  forceFirstUci;            // PolicyResearchForceFirstMove.
    int          forceFirstDepth = 0;      // PolicyResearchForceFirstDepth (0 = all depths / persistent).
    bool         preserveAspiration = false;  // PolicyResearchPreserveAspiration (keep baseline avg/delta).
    bool         disableFailHighReduction = false; // PolicyResearchDisableFailHighReduction (fixed nominal depth).
};

// Parsing helpers. All return an error string on invalid input; Stockfish
// builds with -fno-exceptions, so errors are reported through option callbacks.

inline std::optional<std::string> parse_mode(const std::string& token, Mode& out) {
    // Case-insensitive token comparison.
    std::string t;
    t.reserve(token.size());
    for (char c : token)
        t.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));

    if (t == "off")
        out = Mode::Off;
    else if (t == "observational")
        out = Mode::Observational;
    else if (t == "root_counterfactual")
        out = Mode::RootCounterfactual;
    else if (t == "internal_counterfactual")
        out = Mode::InternalCounterfactual;
    else if (t == "shared_permutation")
        out = Mode::SharedPermutation;
    else if (t == "jacobian_dump")
        out = Mode::JacobianDump;
    else if (t == "reduction_shadow")
        out = Mode::ReductionShadow;
    else
        return "invalid research mode '" + token + "' (expected one of: off, "
               "observational, root_counterfactual, internal_counterfactual, "
               "shared_permutation, jacobian_dump, reduction_shadow)";
    return std::nullopt;
}

inline std::optional<std::string> parse_sample_rate(const std::string& token, double& out) {
    char* end = nullptr;
    double v  = std::strtod(token.c_str(), &end);
    if (end == token.c_str() || *end != '\0' || !std::isfinite(v) || v <= 0.0 || v > 1.0)
        return "invalid sample rate '" + token + "' (expected 0 < rate <= 1)";
    out = v;
    return std::nullopt;
}

inline std::optional<std::string> parse_u64(const std::string& token, u64& out) {
    char* end = nullptr;
    unsigned long long v = std::strtoull(token.c_str(), &end, 10);
    if (end == token.c_str() || *end != '\0')
        return "invalid integer value '" + token + "'";
    out = v;
    return std::nullopt;
}

inline std::optional<std::string> parse_switch(const std::string& token, bool& out) {
    // Case-insensitive like parse_mode().
    std::string t;
    t.reserve(token.size());
    for (char c : token)
        t.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    if (t == "true" || t == "on" || t == "yes")
        out = true;
    else if (t == "false" || t == "off" || t == "no")
        out = false;
    else
        return "invalid value '" + token + "' (expected on/off)";
    return std::nullopt;
}

// Single configuration instance shared by all translation units (C++17 inline).
inline Config& config() {
    static Config cfg;
    return cfg;
}

// Canonical activation predicate: the master switch AND a non-off mode.
inline bool enabled() {
    return config().switchOn && config().mode != Mode::Off;
}

inline void register_options(OptionsMap& options) {
    options.add("PolicyResearch", Option("off", [](const Option& o) {
                    return parse_switch(std::string(o), config().switchOn);
                }));

    options.add("PolicyResearchMode", Option("off", [](const Option& o) {
                    return parse_mode(std::string(o), config().mode);
                }));

    options.add("PolicyResearchLogPath", Option("", [](const Option& o) {
                    config().logPath = std::string(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchSeed", Option(0, 0, 2147483647, [](const Option& o) {
                    config().seed = static_cast<u64>(static_cast<int>(o));
                    return std::nullopt;
                }));

    options.add("PolicyResearchSampleRate", Option("1.0", [](const Option& o) {
                    return parse_sample_rate(std::string(o), config().sampleRate);
                }));

    options.add("PolicyResearchMaxRecords", Option(0, 0, 2147483647, [](const Option& o) {
                    config().maxRecords = static_cast<int>(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchTopK", Option(0, 0, 256, [](const Option& o) {
                    config().topK = static_cast<int>(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchNodeBudget", Option(0, 0, 2147483647, [](const Option& o) {
                    config().nodeBudget = static_cast<u64>(static_cast<int>(o));
                    return std::nullopt;
                }));

    options.add("PolicyResearchPolicyVersion", Option("", [](const Option& o) {
                    config().policyVersion = std::string(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchForceFirstMove", Option("", [](const Option& o) {
                    config().forceFirstUci = std::string(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchForceFirstDepth", Option(0, 0, 256, [](const Option& o) {
                    config().forceFirstDepth = static_cast<int>(o);
                    return std::nullopt;
                }));

    options.add("PolicyResearchPreserveAspiration", Option("off", [](const Option& o) {
                    return parse_switch(std::string(o), config().preserveAspiration);
                }));

    options.add("PolicyResearchDisableFailHighReduction", Option("off", [](const Option& o) {
                    return parse_switch(std::string(o), config().disableFailHighReduction);
                }));
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH

#endif  // #ifndef POLICY_RESEARCH_OPTIONS_H_INCLUDED
