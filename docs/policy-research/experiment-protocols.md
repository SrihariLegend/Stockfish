# Policy Research — Experiment protocols

Run bookkeeping: every experiment records, per Phase 1 manifest conventions,
the exact executable (`engine_executable_sha256`, `engine_banner`,
`build_command`), the corpus checksum, all pinned and research UCI options,
and the research policy/seed/rate/version. Determinism is per executable:
rebuilds require regenerating all artifacts (see
`tools/policy_research/README.md`).

## Protocol P2.1 — versioned logging gate (Phase 2 exit gate, plan §7.8)

Purpose: prove that observational logging is behavior-transparent, select its
samples deterministically, and that logs decode cleanly.

1. Build one research executable: `make -C src research-build ARCH=x86-64-avx2`.
   Record the binary SHA-256 and embedded commit. `src/stockfish` must remain a
   macro-off build afterwards (rebuild with `make -C src build`).
2. Run the logging gate on the corpus:

   ```bash
   python3 tools/policy_research/run_corpus.py verify-research \
       --engine src/stockfish --depth 11 --hash 16 \
       --build-command 'make -C src research-build ARCH=x86-64-avx2'
   ```

   The command executes three full corpus passes:
   - pass 0 `off`: research options at defaults (logging disabled),
   - passes 1 and 2 `on`: `PolicyResearch on`, `PolicyResearchMode
     observational`, `PolicyResearchLogPath <run dir>/root-<id>.bin`,
     `PolicyResearchPolicyVersion baseline-observational-v1`,
     `PolicyResearchSampleRate 0.05`, `PolicyResearchMaxRecords 250000`,
     one fresh engine process per root, log per root.
3. Checks that must all pass:
   - best move / normalized info rows / node counts identical across the three
     passes for every root (logging has zero search effect),
   - decoded logs for passes 1 and 2 decode without validation errors and are
     payload-identical record streams (deterministic sample selection),
   - every ROOT_START FEN matches the corpus root FEN,
   - no MOVE_ATTEMPT records an outcome for an unsearched move (schema
     invariant, enforced by the decoder),
   - ERROR_RECORD absent unless `PolicyResearchMaxRecords` was hit; a hit must
     be reported, never silent.
4. Artifacts land under `tools/policy_research/runs/<label>` (git-ignored):
   pass manifests/results, per-root logs, decoded JSONL, comparison summary.

Rates/caps in the gate are chosen so sample identity can be demonstrated over
meaningful volume without unbounded memory; they are not dataset settings.

## Protocol P3.x (planned) — observational dataset
Dataset construction (rows per candidate decision, budget-conditioned
survival of quiet moves) is Phase 3; statuses
`OBSERVED_FAIL_HIGH / OBSERVED_FAIL_LOW / OBSERVED_ALPHA_RAISE / OBSERVED_EXACT /
UNOBSERVED_AFTER_CUTOFF / SEARCH_ABORTED / BUDGET_CENSORED / INVALID_OR_SKIPPED`
will be derived from `research-data/1` DECISION_POINT + MOVE_ATTEMPT streams.
