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

#ifndef POLICY_RESEARCH_SCOPED_PROBE_H_INCLUDED
#define POLICY_RESEARCH_SCOPED_PROBE_H_INCLUDED

#ifdef POLICY_RESEARCH

namespace Stockfish::Research {

// ScopedShadowProbe is an RAII guard active during internal shadow searches.
// While active, it suppresses observational logging, telemetry recording, and
// recursive sampling to guarantee that shadow probes never corrupt live datasets.
class ScopedShadowProbe {
   public:
    ScopedShadowProbe() { ++shadow_probe_depth_; }
    ~ScopedShadowProbe() { --shadow_probe_depth_; }

    ScopedShadowProbe(const ScopedShadowProbe&)            = delete;
    ScopedShadowProbe& operator=(const ScopedShadowProbe&) = delete;

    static bool is_active() { return shadow_probe_depth_ > 0; }
    static int  depth() { return shadow_probe_depth_; }

   private:
    static inline thread_local int shadow_probe_depth_ = 0;
};

inline bool is_shadow_probe_active() {
    return ScopedShadowProbe::is_active();
}

}  // namespace Stockfish::Research

#endif  // POLICY_RESEARCH

#endif  // POLICY_RESEARCH_SCOPED_PROBE_H_INCLUDED
