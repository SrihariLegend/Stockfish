# Canonical Phase 4 Experimental Evidence

This directory contains tracked canonical experimental outputs for Phase 4 of the Stockfish Policy Research project. Unlike the exploratory run workspace (`tools/policy_research/runs/`, which is git-ignored), these artifacts are permanently tracked under version control to provide complete provenance, reproducible baselines, and permanent auditable evidence.

## Provenance Standard
Every artifact in this directory was generated with:
- **Engine Binary**: Built from commit `3d2e669c` with `make -C src -j ARCH=x86-64-avx2 EXTRACXXFLAGS="-DPOLICY_RESEARCH -DSTATIC_BIG_PAGE" build`
- **Engine Embedded Commit**: `3d2e669c`
- **Tool Commit**: `3d2e669cfacb`
- **Tool Worktree Status**: Clean (`tool_dirty: False`, `tool_dirty_file_count: 0`)
- **Corpus**: `tools/policy_research/corpora/corpus-v3.json` (SHA256: `c7c7d47c558f8d23b6d3a726ebda886a98633f39d5d9749b153e052259a5b944`)
- **Quarantined Test Set**: Test roots `c3-t-001` through `c3-t-008` were strictly quarantined and never evaluated in any Phase 4 artifact.

## Artifact Inventory

| File | Size (bytes) | SHA256 Checksum | Generation Command | Description |
|---|---:|---|---|---|
| `p4-isolated.json` | 537,720 | `6191a8d30af83040c069c9cc6007e1461c3a6de04ef758ac291b602297f40a60` | `python3 tools/policy_research/p4_force_first.py --engine src/stockfish --corpus tools/policy_research/corpora/corpus-v3.json --depth 14 --depth-mode isolated --out docs/policy-research/evidence/p4-canonical/p4-isolated.json` | Full 18-root dev+val canonical counterfactual evaluation at depth 14 with pre-iteration candidate shortlist |
| `depth-ladder-dev-val.json` | 2,194,326 | `5e6d502e022ab271fd39cb5f5080b083788230f46a32d51a11fef929442f342f` | `python3 tools/policy_research/p4_depth_ladder.py --engine src/stockfish --corpus tools/policy_research/corpora/corpus-v3.json --depths 12,14,16 --out docs/policy-research/evidence/p4-canonical/depth-ladder-dev-val.json` | Multi-depth ladder across depths 12, 14, and 16 with fixed pre-iteration candidate sets |
| `causal-decomposition.json` | 50,860 | `86b8d5bc63696bf57284f01cc789b533ccb03da9c638656e3d3303c02ddc64be` | `python3 tools/policy_research/p4_causal_decomp.py --engine src/stockfish --corpus tools/policy_research/corpora/corpus-v3.json --depth 14 --ref-depth 18 --out docs/policy-research/evidence/p4-canonical/causal-decomposition.json` | Full 2³ factorial causal decomposition (8 forced conditions + baseline = 9 total) across 4 representative roots |
| `timing-benchmark-trials12.json` | 608,928 | `de49c538f3e695926e6f809e9ba06fff7cb3da4c031a3f71a9c9e8889b0411ff` | `python3 tools/policy_research/p4_force_first.py --engine src/stockfish --corpus tools/policy_research/corpora/corpus-v3.json --depth 14 --depth-mode isolated --trials 12 --out docs/policy-research/evidence/p4-canonical/timing-benchmark-trials12.json` | 12-trial balanced Latin-square counterbalanced multi-trial timing benchmark |
