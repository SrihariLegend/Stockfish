# Deterministic research harness (policy research, Phase 1)

Reproducible fixed-depth search runs over a versioned root corpus, with a run
manifest and an enforced determinism gate. Part of the alpha-beta proof-policy
project; see `docs/policy-research/plan.md` (§6) and `architecture-inventory.md`.

## Layout

```text
tools/policy_research/
  run_corpus.py                 deterministic runner + manifest writer + verifier
  decode_research_log.py        binary log decoder / validator / compare / JSONL
  p3_dataset.py                 Phase 3 dataset collector + baseline reporter
  corpora/corpus-v1.json        versioned root corpus (schema corpus/v1)
  tests/test_run_corpus.py      unit tests (stdlib unittest)
  tests/test_p3_dataset.py      unit tests (stdlib unittest)
  runs/                         per-run artifacts (git-ignored, never committed)
```

## Requirements

- Python >= 3.10 (standard library only).
- A Stockfish executable built from the repository (default
  `<repo>/src/stockfish`), plus the value-network `.nnue` file next to it
  (`make net` in `src/` if missing).

## Research configuration (fixed for every run)

Single thread, fixed hash, fixed `go depth`, MultiPV 1, ponder off, full strength
(`Skill Level 20`, `UCI_LimitStrength false`), `UCI_Chess960 false`, tablebases
disabled (SyzygyPath **explicitly** cleared to `<empty>` on every process), no
external stop, and a **fresh engine process per root**. The value network is
pinned to an explicit file whose SHA-256 is recorded in the manifest; the run
refuses to start when the network cannot be located.

## Engine binaries and build provenance

The repository HEAD alone does not identify the executable. Every run manifest
records:

- `engine_executable_sha256` — SHA-256 of the binary that actually ran,
- `engine_banner` — the engine's identity line, which embeds the source commit
  the binary was built from,
- `build_command` — passed with `--build-command '...'` and recorded verbatim
  (recommended: record the exact make invocation and whether PGO was used).

Build a research-enabled binary (compiles `-DPOLICY_RESEARCH`) with the
canonical target, which cleans stale objects first (the Makefile does not
rebuild objects when flags change, so `make build EXTRACXXFLAGS=...` alone is
**not** safe):

```bash
make -C src research-build ARCH=x86-64-avx2          # plain, macro on
make -C src research-profile-build ARCH=x86-64-avx2   # PGO, macro on (GCC)
```

Production builds stay macro-free: `make -C src build ARCH=x86-64-avx2`.

## Usage

```bash
# Determinism gate: run the corpus twice, require identical results
python3 tools/policy_research/run_corpus.py verify --depth 12 --hash 16

# Single run (no gate)
python3 tools/policy_research/run_corpus.py run --depth 12

# Smoke test a subset of roots
python3 tools/policy_research/run_corpus.py verify --only c1-d-001,c1-v-003 --depth 8

# Custom engine / eval file, recording the build command
python3 tools/policy_research/run_corpus.py verify \
    --engine /path/to/stockfish --eval-file /path/to/net.nnue --depth 12 \
    --build-command 'make -C src research-build ARCH=x86-64-avx2'
```

`verify` exits 0 only when run A and run B agree exactly on best move, normalized
`info` rows, and node counts for every root. Wall time (`time`) and its derived
`nps` are excluded by design (`docs/policy-research/plan.md` §6.4); everything else
must match. Before comparing, every root result is validated as a completed
fixed-depth search (final `info` depth reaches the requested depth, or a mate was
announced; positive nodes; score and non-empty PV present), so two identically
aborted searches can never satisfy the gate.

## Research logging gate (Phase 2, protocol P2.1)

Requires a `POLICY_RESEARCH` executable (`make -C src research-build`; do not use a
production binary). Runs the corpus three times and enforces the Phase 2 exit gate:

```bash
python3 tools/policy_research/run_corpus.py verify-research \
    --engine /path/to/research/stockfish --depth 11 --hash 16 \
    --build-command 'make -C src research-build ARCH=x86-64-avx2' \
    --research-sample-rate 0.05 --research-seed 101 \
    --research-max-records 250000 --research-policy-version baseline-observational-v1
```

Passes: `off` (research options at defaults, no logs) and `on` #1 / #2 (identical
`PolicyResearch*` settings, one fresh engine process per root, one log file per
root). Checks that must all hold (see
`docs/policy-research/experiment-protocols.md`):

1. best move / normalized info rows identical across **all three** passes (logging
   has zero search effect),
2. every log decodes without validation errors; its `ROOT_START` FEN equals the
   corpus root FEN, and its `RUN_START` (mode/seed/threshold/cap/policy version)
   plus the engine identity line match the pass manifest — a silently unapplied
   option (e.g. an out-of-range seed) cannot pass. Malformed input (truncated
   header, string length prefix, or declared span) is always reported as a
   decoder `ValidationError`, never a native crash/`struct.error`,
3. decoded record streams (payloads only) are identical between the `on` passes
   (deterministic sample selection),
4. no `MOVE_ATTEMPT` labels an unsearched move (child searches ≥ 1); outcome
   agrees with the returned value; ROOT_END/RUN_END totals match counted records;
   RUN_END `overflow`/`error_code` carry only their 0/1 wire values; a
   collection-cap hit is recorded (overflow ⇔ error_code 1 ⇔ code-1
   ERROR_RECORD), never silent.

The runner also fails fast (before any search) when the engine does not declare
every required `PolicyResearch*` option (master switch, mode, seed, sample rate,
max records, policy version, and log path), or when a `--research-seed` /
`--research-max-records` value lies outside the range the engine itself declares
for that spin option.

Artifacts land under `runs/<corpus>-d<d>-h<h>-research-<stamp>/` (run-off,
run-on-1, run-on-2, per-root logs, `research_summary.json`). Decode/validate any
log file independently:

```bash
python3 tools/policy_research/decode_research_log.py path/to/root-x.bin
python3 tools/policy_research/decode_research_log.py --compare a.bin b.bin
python3 tools/policy_research/decode_research_log.py --jsonl out.jsonl log.bin
```

## Artifacts per run

Written under `tools/policy_research/runs/<corpus>-d<depth>-h<hash>-<stamp>/`:

- `run-<uuid>/manifest.json` — schema `research-run/1`: run UUID, timestamp, engine
  commit, dirty-worktree status, **executable SHA-256**, **engine banner (embedded
  source commit)**, **build command**, compiler/arch/settings, host CPU/OS, thread
  count, hash, search depth, value-network path + SHA-256, corpus path + SHA-256,
  applied UCI options (incl. SyzygyPath cleared and UCI_Chess960 false),
  engine-declared UCI defaults, random seed; the research `on` passes add
  `random_seed`, `research_data_schema: research-data/1`, and
  `research_log_container: research-log/1`.
- `run-<uuid>/results.json` — schema `research-result/1`: per-root best move,
  normalized info rows, parsed summary (score/bound/nodes/PV), wall ms.
- `comparison.json` (verify only) — schema `research-compare/1`: per-root PASS/FAIL
  and first divergence.

`runs/` is git-ignored: only manifests, scripts, schemas, and the corpus are
committed.

## Phase 3 dataset collection and baseline report

`p3_dataset.py` turns uniform-rate observational logs into one modeling row
per **searched quiet move** at sampled eligible nodes, then produces the
calibration-baseline report. Requires `numpy` and `pandas` (no sklearn).

```bash
# collect one fresh-process run per (depth, root); dataset protocol P3.2:
# prefix-free and uniform — a single target depth per root (iterative-
# deepening prefixes are not re-collected) at one node-sample rate
python3 tools/policy_research/p3_dataset.py collect \
    --engine src/stockfish --depths 20 --rate 0.5 --seed 101 \
    --hash 16
# aggregate into baseline-report.{md,json} in the dataset dir
python3 tools/policy_research/p3_dataset.py report <dataset-dir>
```

Collection details:

- every run decodes cleanly and cross-checks RUN_START/ROOT_START against the
  request (same conventions as `verify-research`);
- rows split per root into `rows/d<depth>/root-<id>.attempts.jsonl`
  (self-contained modeling rows) and `...decisions.jsonl` (the decision FENs);
  every row records `node_weight = 1/sample_rate` so non-uniform rate designs
  can be aggregated without bias;
- collecting two target depths for the same root re-collects byte-identical
  iterative-deepening prefixes (a deeper run replays shallower iterations) and
  is warned against; the P3.2 report assumes prefix-free rows;
- a run whose record volume would exceed the engine's per-run hard cap
  (4 194 304 records / 256 MiB) cannot be held by `research-data/1`; lower the
  uniform sample rate rather than excluding a cell (P3.2 has no exclusions). A
  cap hit inside a run is always an ERROR_RECORD and is never silently
  accepted as a dataset row;
- `report` aggregates root-aware statistics (pooled numbers plus between-root
  `macro_*` columns; roots, not rows, are the sample unit), a decision-joined
  node-level section, and grouped cutoff calibration (ridge-IRLS logistic on
  standardized features fit on development-set roots, PAV-isotonic fit on
  validation-set roots, metrics evaluated on test-set roots only; numpy only).
  History-score calibration is out of scope for `research-data/1` (see
  `docs/policy-research/experiment-protocols.md` Protocol P3.x).

Artifacts from a run must be regenerated whenever the executable is rebuilt
(determinism is per executable).

## Unit tests

```bash
python3 -m unittest discover -s tools/policy_research/tests -v
```

Integration (double-run determinism on `c1-d-001` at depth 6) requires the engine:

```bash
STOCKFISH_ENGINE=$PWD/src/stockfish \
  python3 -m unittest tools.policy_research.tests.test_run_corpus.TestIntegration -v
```

The decoder tests are engine-free (synthetic logs); the full `verify-research`
gate doubles as the end-to-end engine test for Phase 2.

## Extending the corpus

Positions are versioned by corpus file. Do not edit `corpus-v1.json` in place once
it is committed and used; create `corpus-v2.json` instead. (One documented
metadata-only exception exists for `corpus-v1.json`, recorded in its `history`
array: the castling-tag correction, made before any logging existed and before
any committed artifact referenced the old checksum; gate artifacts were
regenerated.) Rules:

- One FEN per root; six FEN fields; unique stable ids.
- Sets: `development`, `validation`, `test`. Split by root (or by source game);
  positions from one game never span sets.
- Record `source` and `license`; record `source_line_moves` (UCI) when the FEN was
  derived by replaying a game.
- Game-derived FENs should be produced by replaying the move list through the
  pinned engine (`position startpos moves ...` then `d`, parse the `Fen:` line),
  which guarantees legality.
- Tag semantics are documented in the corpus `tag_semantics` field. Note that
  `castling` means "the FEN declares castling rights for either side" (a tag of
  `true` does not mean the side to move can castle). History of edits to the file
  is recorded in the `history` array.
