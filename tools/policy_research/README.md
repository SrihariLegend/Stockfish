# Deterministic research harness (policy research, Phase 1)

Reproducible fixed-depth search runs over a versioned root corpus, with a run
manifest and an enforced determinism gate. Part of the alpha-beta proof-policy
project; see `docs/policy-research/plan.md` (§6) and `architecture-inventory.md`.

## Layout

```text
tools/policy_research/
  run_corpus.py                 deterministic runner + manifest writer + verifier
  corpora/corpus-v1.json        versioned root corpus (schema corpus/v1)
  tests/test_run_corpus.py      unit tests (stdlib unittest)
  runs/                         per-run artifacts (git-ignored, never committed)
```

## Requirements

- Python >= 3.10 (standard library only).
- A Stockfish executable built from the repository (default
  `<repo>/src/stockfish`), plus the value-network `.nnue` file next to it
  (`make net` in `src/` if missing).

## Research configuration (fixed for every run)

Single thread, fixed hash, fixed `go depth`, MultiPV 1, ponder off, full strength
(`Skill Level 20`, `UCI_LimitStrength false`), tablebases disabled (SyzygyPath left
at the empty default), no external stop, and a **fresh engine process per root**.
The value network is pinned to an explicit file whose SHA-256 is recorded in the
manifest.

## Usage

```bash
# Determinism gate: run the corpus twice, require identical results
python3 tools/policy_research/run_corpus.py verify --depth 12 --hash 16

# Single run (no gate)
python3 tools/policy_research/run_corpus.py run --depth 12

# Smoke test a subset of roots
python3 tools/policy_research/run_corpus.py verify --only c1-d-001,c1-v-003 --depth 8

# Custom engine / eval file
python3 tools/policy_research/run_corpus.py verify \
    --engine /path/to/stockfish --eval-file /path/to/net.nnue --depth 12
```

`verify` exits 0 only when run A and run B agree exactly on best move, normalized
`info` rows, and node counts for every root. Wall time (`time`) and its derived
`nps` are excluded by design (`docs/policy-research/plan.md` §6.4); everything else
must match.

## Artifacts per run

Written under `tools/policy_research/runs/<corpus>-d<depth>-h<hash>-<stamp>/`:

- `run-<uuid>/manifest.json` — schema `research-run/1`: run UUID, timestamp, engine
  commit, dirty-worktree status, compiler/arch/settings, host CPU/OS, thread count,
  hash, search depth, value-network path + SHA-256, corpus path + SHA-256, applied
  UCI options, engine-declared UCI defaults, random seed (n/a).
- `run-<uuid>/results.json` — schema `research-result/1`: per-root best move,
  normalized info rows, parsed summary (score/bound/nodes/PV), wall ms.
- `comparison.json` (verify only) — schema `research-compare/1`: per-root PASS/FAIL
  and first divergence.

`runs/` is git-ignored: only manifests, scripts, schemas, and the corpus are
committed.

## Unit tests

```bash
python3 -m unittest discover -s tools/policy_research/tests -v
```

Integration (double-run determinism on `c1-d-001` at depth 6) requires the engine:

```bash
STOCKFISH_ENGINE=$PWD/src/stockfish \
  python3 -m unittest tools.policy_research.tests.test_run_corpus.TestIntegration -v
```

## Extending the corpus

Positions are versioned by corpus file. Do not edit `corpus-v1.json` in place once
it is committed and used; create `corpus-v2.json` instead. Rules:

- One FEN per root; six FEN fields; unique stable ids.
- Sets: `development`, `validation`, `test`. Split by root (or by source game);
  positions from one game never span sets.
- Record `source` and `license`; record `source_line_moves` (UCI) when the FEN was
  derived by replaying a game.
- Game-derived FENs should be produced by replaying the move list through the
  pinned engine (`position startpos moves ...` then `d`, parse the `Fen:` line),
  which guarantees legality.
