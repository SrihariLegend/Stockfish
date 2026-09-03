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
event at the recorded child depths), and `ABORTED_STOP` (the single
right-censored event class). Outcome 3 records only the generic `threads.stop`
condition — the schema carries no stop reason — so `SEARCH_ABORTED` vs
`BUDGET_CENSORED` cannot be told apart; both collapse to that generic status
until a stop-reason field is added.

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

## Protocol P3.1 (executed 2026-09-04) — observational dataset v1

Purpose: collect the behavior-conditioned quiet-move survival dataset that
`research-data/1` can produce (per Protocol P3.x above) and generate the
calibration baseline report (plan §8.1/§8.2 derivable subset). Tools:
`tools/policy_research/p3_dataset.py` (`collect`, then `report`). No engine
change; `src/stockfish` stayed macro-off.

1. Executable: research build of committed tree `c9878d11` (banner
   `Stockfish dev-20260904-c9878d11`), `make -C src research-build
   ARCH=x86-64-avx2`; corpus-v1 (SHA-256
   `019667e200c48ea0b69ce374418b4b09634e0c6bc08be6af172d3cad56eab46a`).
2. Settings per (depth, root) run — fresh engine process and log file each:
   fixed `go depth` 14/17/20, hash 16, MultiPV 1, one thread, Syzygy cleared;
   `PolicyResearch on`, mode `observational`, `SampleRate 1.0` (every eligible
   NonPV node sampled), seed 101, `MaxRecords 2147483647` (engine enforces its
   hard cap 4 194 304), policy `baseline-observational-v1`, eval = engine
   default `nn-1a298aa575a0.nnue`.
3. Every log decodes cleanly and cross-checks RUN_START (mode/seed/threshold/
   effective cap/policy) and ROOT_START (root FEN) against the request, per
   manifest conventions. Outputs: `manifest.json` (schema
   `research-dataset/1`, full provenance incl. `excluded_cells`),
   `logs/`, `rows/d<depth>/root-<id>.attempts.jsonl` (one modeling row per
   searched quiet move; decision features copied inline) and `...decisions.jsonl`
   (decision FENs), and `baseline-report.{md,json}`.
4. Volume: 35 (depth, root) runs in ~197 s wall; 4 097 558 decision rows and
   10 469 036 attempt rows (93 409 899 nodes consumed by those attempts).
5. Cell exclusion: `c1-d-004@20` is not in the dataset — its full-rate record
   volume exceeds the engine's per-run hard cap (4 194 304 records / 256 MiB),
   so `research-data/1` cannot hold that run in one file. Recorded in the
   manifest (`excluded_cells`); no other cap hits.
6. Censoring: 0 ABORTED_STOP rows — the corpus protocol's fixed-depth,
   single-`go`, no-external-stop searches never trip the generic stop
   condition, so outcomes 1/2 are exact for every collected row.
7. Headline baseline (behavior-policy-conditioned; not unbiased for unsearched
   moves): overall cutoff rate 11.63% of completed attempts; TT-move cutoff
   success 71.1% vs 7.4% for non-TT moves; re-search rate 2.22%; cutoff rate
   monotone in quiet ordinal (33.1% → 0.48% at ordinals 1 → 12); fail-low mean
   cost 5.80 nodes (median 1), monotone in remaining depth (1.28 at [1,3) →
   22.8 at ≥13). Cutoff calibration (ridge-logistic on the recorded context
   features, then PAV-isotonic on the logistic output; 70/30 split, 3.14M test
   rows): logistic AUC 0.9165 / Brier 0.0621 / log-loss 0.2114 / ECE 0.0111;
   isotonic ECE 0.0005, Brier 0.0616, log-loss 0.2083.
8. Artifacts: `tools/policy_research/runs/
   policy-research-corpus-v1-d14-17-20-h16-rate1.0-dataset-20260904T010204/`
   (git-ignored). Regenerate everything if the executable is ever rebuilt
   (determinism is per executable).
