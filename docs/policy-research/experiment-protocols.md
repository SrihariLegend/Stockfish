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

Phase 3 scope with the committed `research-data/1` schema: one analysis row per
**searched quiet move** in a sampled eligible node (DECISION_POINT + the
MOVE_ATTEMPT records that reference it), behavior-conditioned on the node and
attempt context recorded at decision time. Derivable statuses:
`OBSERVED_FAIL_HIGH` (cutoff, survival), `OBSERVED_FAIL_LOW` (exact negative
event at the recorded child depths), and `SEARCH_ABORTED`/`BUDGET_CENSORED`
(only right-censored event class).

Not derivable from `research-data/1`, and therefore deferred to the
counterfactual phases (which need a new schema with full candidate enumeration
and MovePicker baseline features):

- `UNOBSERVED_AFTER_CUTOFF`: `research-data/1` records only moves actually
  attempted. A quiet move that was never searched because an earlier move cut
  off is indistinguishable from a pruned/skipped move — both simply have no
  MOVE_ATTEMPT. Candidate-set denominators and per-candidate baselines
  (MovePicker stage/score, history components) require the counterfactual
  candidate-enumeration record.
- `INVALID_OR_SKIPPED`: illegal/unsafe moves and moves skipped before search
  are not enumerated either.
- `OBSERVED_ALPHA_RAISE` / `OBSERVED_EXACT`: sampled nodes are non-root NonPV
  null-window nodes (`beta == alpha + 1`), where a move cannot raise alpha
  without also reaching `value >= beta` (which is a cutoff). Exact bound values
  and alpha-raise-without-cutoff are only observable at PV/full-window nodes,
  which this schema does not record.

The Phase 3 analysis design in plan §8 stays as the target; the dataset this
schema can actually produce is the behavior-conditioned survival rows above.
When candidate enumeration lands (counterfactual phases), extend the schema and
re-run collection with the extended record types.
