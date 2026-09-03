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

> **Superseded by P3.2 (same day).** The P3.1 evaluation protocol was reviewed
> and found to (1) randomly split rows across the same roots/search trees and
> include exact iterative-deepening prefix duplicates (~28.9% of rows are
> byte-identical repeats across the 14/17/20 target-depth runs); (2) treat 10M+
> rows as independent observations when only 12 root positions are the sample
> unit; (3) exclude the non-random cell `c1-d-004@20`; (4) use a logistic +
> isotonic fit whose margins are collinear (`margin_alpha == 1 - margin_beta`)
> and whose isotonic calibrator was fit on in-sample logistic predictions;
> and (5) overstate an attempt-cost sum and a 0.0005 isotonic ECE as unique
> node totals / robust calibration. The v1 dataset directory was removed after
> P3.2 landed; the v1 numbers below are preserved as the historical record.

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
   (git-ignored; removed when P3.2 landed). Regenerate everything if the
   executable is ever rebuilt (determinism is per executable).

## Protocol P3.2 (executed 2026-09-04) — prefix-free uniform-rate dataset v2

Purpose: address every methodological finding of the P3.1 review and produce
an honest, root-aware calibration baseline. Tools:
`tools/policy_research/p3_dataset.py` (`collect`, then `report`); no engine
change, `src/stockfish` stayed macro-off.

1. Executable and corpus identical to P3.1: research build of committed tree
   `c9878d11` (banner `Stockfish dev-20260904-c9878d11`), `make -C src
   research-build ARCH=x86-64-avx2`; corpus-v1 (SHA-256
   `019667e200c48ea0b69ce374418b4b09634e0c6bc08be6af172d3cad56eab46a`).
2. Dataset design — **prefix-free and uniform**: one fresh process/log run per
   corpus root at fixed `go depth` 20 and node-sample rate 0.5 (seed 101,
   `PolicyResearchSampleRate 0.5`). A deeper target run replays shallower
   iterations byte-identically, so re-collecting shallow targets (P3.1's
   14/17/20 ladder) only duplicated rows; P3.2 takes the single deepest run
   per root, making every row unique by construction. Rate 0.5 is uniform
   across roots, so `c1-d-004@20` now fits (3 103 654 records < the 4 194 304
   hard cap) — **no excluded cells**. Every row records
   `node_weight = 1 / sample_rate`. Other settings as P3.1 (hash 16, MultiPV
   1, one thread, Syzygy cleared, mode `observational`, `MaxRecords
   2147483647`, policy `baseline-observational-v1`, eval = engine default).
3. Volume: 12 runs in ~111 s wall; 2 135 633 decision rows and 5 607 088
   attempt rows; engine root-search totals 19 720 628 nodes; zero overflow,
   zero exclusions, zero ABORTED_STOP rows (outcomes 1/2 exact). Every log
   decodes and cross-checks RUN_START/ROOT_START as in P3.1. Artifacts:
   `tools/policy_research/runs/
   policy-research-corpus-v1-d20-h16-rate0.5-dataset-20260904T014612/`
   (git-ignored; manifest records `collection_protocol` and `sample_rate`).
4. Report (schema `research-baseline-report/2`; superseded in place by `/3`
   below — the v3 run reproduced every pooled / macro number of the `/2`
   artifact byte-identically) changes that address the review:
   - **Root-aware statistics**: every rate/cost table reports pooled numbers
     alongside between-root macro columns (`n_roots`, `macro_mean`,
     `macro_sd`, `macro_min`, `macro_max`); row-level SE/CI columns are gone.
   - **Grouped calibration**: logistic (ridge on standardized features,
     unpenalized intercept, convergence reported) is fit on the
     development-set roots; PAV-isotonic is fit on the validation-set roots;
     every metric is evaluated only on the test-set roots (n = 3), pooled and
     macro. `margin_alpha` is dropped (exactly `1 - margin_beta` at
     null-window nodes). Standardized coefficients and reliability tables are
     reported.
   - **Node-level section**: attempts are joined onto their decision nodes
     (1 507 083 nodes with ≥1 searched quiet move of 2 135 633 sampled):
     share of nodes whose quiet loop ends in a quiet cutoff, quiet-cutoff
     ordinal distribution, wasted-before-cut and non-cutting-node quiet costs
     by remaining depth.
   - **Honest node-cost wording**: `nodes_consumed` is the *local*, nested
     per-attempt subtree count; the attempt-cost sum is overlapping, not
     unique engine work (engine root-search totals are reported separately).
5. Headline results (behavior-policy-conditioned; not unbiased for unsearched
   moves): statuses OBSERVED_FAIL_HIGH 556 059 (9.92%) and OBSERVED_FAIL_LOW
   5 051 029; quiet-ordinal-1 cutoff 29.2% pooled (macro 31.3 ± 7.8%); quiet
   TT-move cutoff rate 69.7% (macro 70.1 ± 5.1%) vs 6.7% for non-TT quiet
   moves; re-search rate 2.39% (re-searched fail-lows: mean 69.9, median 25
   nodes); node level: 37% of
   nodes with a quiet attempt end in a quiet cutoff (root span 29–56%) and
   79.1% of those cutoffs happen at the first quiet attempt. Test-root
   calibration (847 222 completed attempts): full-model AUC 0.9360 (macro
   0.932 ± 0.011); validation-fitted isotonic gives a small honest gain over
   logistic — Brier 0.0605 → 0.0598, log-loss 0.2030 → 0.2015, ECE10 0.0195
   → 0.0176 — nothing like the v1 in-sample 0.0005 ECE. Ablations: ordinal
   0.865 / tt 0.779 / context 0.812 AUC.
6. Remaining limitation, documented in the report: 12 corpus roots (3 test
   roots) is a small cluster count; the macro columns quantify the between-
   root spread but cannot substitute for a larger root sample. A larger,
   game-diverse corpus (corpus/v2) is a prerequisite before strong
   generalization claims (tracked in plan.md §8.5/§12.6).

**Follow-up review record — report schema `research-baseline-report/3`**
(read-only expert review of the `/2` artifact; addressed with tool/report/
/docs changes only — no engine change, macro-off engine untouched):

1. **Within-node reordering probe** (global AUC is not move-ordering
   quality). For every *late-quiet-cutoff* node on the test roots (quiet
   loop cut off at ordinal > 1) the fitted model is compared with each
   fail-low quiet predecessor the baseline actually searched: 16 061 late
   nodes / 34 933 predecessor pairs / 432 238 local predecessor nodes. Full
   model pair accuracy 0.0289 (ties 0), cost-weighted 0.0114,
   node-above-all 0.0380 (macro pair accuracy 0.044 ± 0.049); ablations:
   ordinal ~0, tt 0.443 (ties 0.780), context 0.500 (ties 0.9999 — all
   node-level features). Root-balanced full model similar. The report
   frames this as a necessary-condition probe, not a counterfactual savings
   estimate (reordering changes search treatment; unsearched candidates are
   absent), and states that the 0.936 test AUC coexists with ~0 within-node
   accuracy when the model mirrors the baseline's own ordering.
2. **Root-balanced sensitivity (fitting objective)**. Row-weighted fits are
   dominated by one development root (c1-d-004 = 67.5% of development
   attempts; the per-root volume table makes this visible); the report
   therefore also fits with every corpus root at equal total full-rate mass
   (row weight = node_weight / root node_weight sum; weighted
   standardization, quantile-bin edges and PAV). Ranking is stable across
   objectives (logistic AUC 0.93598 row-weighted vs 0.93614 balanced) but
   the probability map is not: balanced logistic Brier 0.05903 / log-loss
   0.19964 / ECE10 0.01048 improves on pooled test rows, while the
   balanced isotonic fit (Brier 0.06128 / log-loss 0.20503 / ECE10 0.02612)
   is worse than the row-weighted isotonic — the PAV map is sensitive to
   the arbitrary balance of 3 validation roots, so no canonical
   calibration map is claimed from this corpus. Coefficient deltas:
   quiet_ordinal −2.92 → −3.40, margin_beta −1.15 → −1.87 (both stronger),
   total_attempted −0.43 → −0.18 under equal root mass.
3. **ECE hides local error**: reliability tables now carry a per-bin
   `gap` column and the worst-bin |gap| headline (logistic +0.202 in
   [0.3,0.4), 17 549 rows; isotonic +0.142 in [0.3,0.4), 19 577 rows),
   beside the pooled ECE10 0.0176.
4. **Sampling-weights (IPW) support**: pooled attempt-level rate tables
   (ordinal + context buckets, flag rows) and both calibration objectives
   honor per-row `node_weight` when a dataset is non-uniform (weighted
   quantiles, IRLS observation weights, weighted PAV); sampled row counts
   stay labeled as recorded. The uniform P3.2 dataset takes a fast path
   that reproduces the `/2` numbers exactly. Unit tests cover parity,
   weighted-quantile behaviour, a crafted sign flip under imbalance, and a
   non-uniform report smoke run.
5. **Observational opportunity accounting** (local nested costs, sampled
   rows): late-cutoff nodes 116 295 = 7.72% of nodes with a quiet attempt
   (20.9% of quiet-cutoff nodes); wasted-before-cut 3 552 841 local nodes =
   12.3% of fail-low local cost; no-quiet-cut loop cost 25 426 146 =
   87.7%; quiet TT-move fail-lows 9 169 463 = 31.6%; re-searched fail-lows
   2 617 206 = 9.0%. Quiet reordering alone has sparse headroom under this
   policy; the numbers are descriptive, not causal savings bounds.
6. **Provenance**: manifest provenance now records the research binary's
   embedded source commit parsed from its version banner (`c9878d11` — repo
   HEAD at a dirty worktree is no longer presented as the binary's commit)
   plus the report tool's git commit and worktree-dirty file count. Report
   artifacts are regenerated after the tooling commit so the recorded tool
   commit is clean and matches the committed reporter.

## Protocol P4.1 (executed 2026-09-04) — root force-first smoke pilot

Purpose: verify the §9.1 research-only root-order override mechanism
(determinism, parity, diagnostic) and take a first, honestly-framed look at
the magnitude of fixed-depth node-cost ordering sensitivity before committing
to the full §9.3–9.6 oracle-gap study.

Engine build: `make research-build ARCH=x86-64-avx2` on commit `b5ab17f7`;
banner `dev-20260904-b5ab17f7` (clean worktree). Tool:
`tools/policy_research/p4_force_first.py`. Common state per intervention:
fresh engine process, same FEN, Hash 16, Threads 1, MultiPV 1, fixed depth,
no time limit (plan §9.2). Candidate set: engine's own top-4 MultiPV listing
at depth 10 (self-referential — an upper bound on ordering opportunity).

Verification results:

1. **Determinism**: baseline and forced-first repeat runs are node/bestmove
   identical (startpos depth 8 baseline 2 712 nodes; forced `d2d4` first
   8 466, both repeated exactly).
2. **Effect**: forcing a non-best first move changes fixed-depth node cost;
   forcing the baseline best first does *not* reproduce baseline cost
   (startpos depth 14: 25 317 vs 43 275) because the always-first semantics
   removes baseline's best-move-switch churn.
3. **Parity gates**: observational-mode runs and master-switch-off runs with
   the option set are node-identical to baseline; an illegal force move
   (`e2e5` from startpos) leaves the search unchanged and emits exactly one
   `info string` diagnostic; macro-off `bench 16 1 10 default depth` =
   453 169 nodes (standing zero-regression gate unchanged).
4. **Fixed-depth-14 node costs** (forced-first candidates; the final best
   move matched baseline in every run):

   | root | C_base (best) | forced-first nodes | R_norm |
   |---|---|---|---|
   | c1-d-001 | 43 275 (e2e4) | e2e4 25 317, d2d4 49 065, g1f3 38 603, c2c4 21 146 | 0.511 |
   | c1-v-001 | 20 956 (d4c5) | d4c5 29 994, c4f7 2 233, e1g1 2 482, b1c3 1 600 | 0.924 |
   | c1-t-001 | 26 292 (f1e2) | f1e2 24 839, f1g2 31 976, f3d1 320 781 | 0.055 |

   Read: at these roots a different first move can be ~10× cheaper (c1-v-001)
   or ~12× more expensive (c1-t-001 `f3d1`) than the baseline ordering at
   the same depth; the baseline's own best move is not always the cheapest
   first move (c1-d-001, c1-v-001), and where the baseline lead move is
   already cheapest the gap is small (c1-t-001).

Limitations (no over-claiming): fixed-depth node cost only — no wall-time
measurement, no §9.4 reference-quality confirmation (depth-14 results could
be re-checked by deeper or reduced-selectivity reference before any run is
called oracle), 3 roots cannot support the §9.6 bootstrap-CI aggregates, and
the candidate set is engine-self-referential. Integration tests
(`TestForceFirstRootOrder`, 5 cases, engine-gated) keep the mechanism
regression-covered. P4.2 must add the §9.3 measurement set (alpha raise,
re-search, wall time), a §9.4 reference result, and a larger root sample.
