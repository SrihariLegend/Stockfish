#!/usr/bin/env bash
#
# Landing gate for the policy-research branch: the checks a static audit cannot do.
#
# The upstream rebase behind this branch passed every static check - identical
# guard counts, 202/202 research call sites, every POLICY_RESEARCH block
# byte-identical - and still shipped one compile error and one runtime crash.
# A text diff cannot see either. This script builds both configurations and runs
# them.
#
#   1. plain build (macro off), then the standing bench
#   2. research build (macro on), then both C++ suites, then the same bench
#        what a diff cannot see: compile errors, runtime crashes, and
#        instrumentation that leaks into search when it should be inert
#        (every build here must bench identically)
#   3. tool tests, with STOCKFISH_ENGINE set so the engine-backed tests run
#   4. --with-upstream: the base's bench must equal the plain bench
#        a rebase resolution that corrupted non-research code
#
# The bench is `bench 16 1 10 default depth`, the project's standing
# zero-regression command (docs/policy-research/plan.md section 9). Parity is
# checked against the sibling build, never against a constant, so the gate does
# not go stale when upstream changes search.
#
# Usage:
#   tools/policy_research/verify.sh
#   tools/policy_research/verify.sh --with-upstream
#   ARCH=x86-64-avx2 tools/policy_research/verify.sh
#
# Environment:
#   ARCH       engine arch; default is the Makefile's own detection
#   PYTHON     interpreter for step 3; needs numpy and pandas
#   UPSTREAM_REF   base compared in step 4 (default: origin/master)
#   SKIP_BUILD=1   step 3 only, against an existing src/stockfish
#   POLICY_RESEARCH_ALLOW_VOLATILE=1   permit a volatile (tmpfs) workspace
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
srcdir=$repo/src
python=${PYTHON:-python3}
upstream_ref=${UPSTREAM_REF:-origin/master}
with_upstream=0
if [ "${1:-}" = --with-upstream ]; then with_upstream=1; fi

make_args=()
if [ -n "${ARCH:-}" ]; then make_args+=("ARCH=$ARCH"); fi

die()  { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

# The project's standing zero-regression bench, with the same node-count
# extraction tests/signature.sh uses.
bench_nodes() {
    local src=$1 out
    out=$( cd "$src" && ./stockfish bench 16 1 10 default depth 2>&1 \
        | grep 'Nodes searched' | awk '{print $4}' || true )
    if [ -z "$out" ]; then die "no node count from bench in $src (engine crashed?)"; fi
    printf '%s' "$out"
}

# /tmp is a 2 GB tmpfs on the development box and a reboot wiped a day of
# uncommitted work there. Refuse to build in a workspace that can evaporate.
fstype=$(df --output=fstype "$repo" 2>/dev/null | tail -n1 | tr -d ' ' || true)
if [ "$fstype" = tmpfs ] && [ "${POLICY_RESEARCH_ALLOW_VOLATILE:-0}" != 1 ]; then
    die "$repo is on tmpfs (volatile); work there does not survive a reboot.
      Move the clone to durable disk, or set POLICY_RESEARCH_ALLOW_VOLATILE=1."
fi

if ! "$python" -c 'import numpy, pandas' 2>/dev/null; then
    die "$python cannot import numpy/pandas, which the tool tests need.
      Build a durable environment once, then point PYTHON at it:
        python3 -m venv ~/.cache/policy-research-venv
        ~/.cache/policy-research-venv/bin/pip install numpy pandas
        PYTHON=~/.cache/policy-research-venv/bin/python $0 $*"
fi

if [ "${SKIP_BUILD:-0}" != 1 ]; then
    # The Makefile does not rebuild objects when flags change, so each build
    # starts from objclean. The two benches must agree, which is what makes
    # step 2's parity check meaningful.
    step "1/4  plain build (macro off), then bench"
    make -C "$srcdir" objclean
    make -C "$srcdir" build -j"$(nproc)" "${make_args[@]}"
    plain_sig=$(bench_nodes "$srcdir")
    echo "  plain bench:    $plain_sig"

    step "2/4  research build (macro on), suites, bench"
    make -C "$srcdir" research-build -j"$(nproc)" "${make_args[@]}"

    while read -r cmd marker; do
        out=$( cd "$srcdir" && printf '%s\nquit\n' "$cmd" | ./stockfish 2>&1 )
        case $out in
            *"$marker"*) echo "  $marker" ;;
            *) printf '%s\n' "$out"; die "$cmd did not report $marker" ;;
        esac
    done <<'SUITES'
policy_research_test_overlay OVERLAY_TEST_OK
policy_research_test_sandbox SANDBOX_TEST_OK
SUITES

    research_sig=$(bench_nodes "$srcdir")
    echo "  research bench: $research_sig"
    if [ "$plain_sig" != "$research_sig" ]; then
        die "instrumentation is not inert: plain bench $plain_sig, research bench $research_sig"
    fi
else
    step "1/4, 2/4  skipped (SKIP_BUILD=1)"
fi

step "3/4  tool tests"
( cd "$repo" && STOCKFISH_ENGINE="$srcdir/stockfish" \
    "$python" -m unittest discover -s tools/policy_research/tests -q )
echo "  tool tests OK"

if [ "$with_upstream" = 1 ]; then
    step "4/4  base reference ($upstream_ref)"
    scratch_base=${VERIFY_SCRATCH:-$HOME/.cache}
    mkdir -p "$scratch_base"
    scratch=$(mktemp -d "$scratch_base/policy-research-verify.XXXXXX")
    cleanup() {
        git -C "$repo" worktree remove --force "$scratch/upstream" >/dev/null 2>&1 || true
        rm -rf "$scratch"
    }
    trap cleanup EXIT

    git -C "$repo" worktree add --detach "$scratch/upstream" "$upstream_ref" >/dev/null
    # Reuse the net already on disk; a fresh worktree would fetch 94 MB again.
    cp "$srcdir"/nn-*.nnue "$scratch/upstream/src/" 2>/dev/null || true
    make -C "$scratch/upstream/src" objclean
    make -C "$scratch/upstream/src" build -j"$(nproc)" "${make_args[@]}"
    upstream_sig=$(bench_nodes "$scratch/upstream/src")
    echo "  base bench:     $upstream_sig"
    if [ "$upstream_sig" != "$plain_sig" ]; then
        die "macro-off build diverges from $upstream_ref: plain $plain_sig, base $upstream_sig"
    fi
else
    step "4/4  base reference: skipped (pass --with-upstream)"
fi

step "verify OK"
