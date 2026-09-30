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

## Protocol P4.1 (executed 2026-09-04, remediated) — root force-first override and counterfactual evaluation

Purpose: verify the §9.1 research-only root-order override mechanism
(determinism, parity, diagnostic) and conduct both isolated and persistent
evaluations addressing the expert review findings:

1. **MovePicker root mechanics**: documented that Stockfish sets
   `ttData.move = rootMoves[0]` and passes it to MovePicker, which emits it
   first in `MAIN_TT`. Subsequent moves follow MovePicker's normal stage
   progression (captures, quiets scored by history).
2. **Separation of Experiment A vs B**:
   - `PolicyResearchForceFirstDepth == 0`: persistent schedule (Experiment A),
     overriding at every root depth 1..D.
   - `PolicyResearchForceFirstDepth > 0`: isolated target depth (Experiment B),
     overriding ONLY at target depth D. Depths 1..D-1 run identical to baseline,
     so TT and history state at the decision boundary are identical across all
     candidate interventions.
3. **Tablebase rank safety**: overrides verify
   `rootMoves[i].tbRank == rootMoves[0].tbRank` before rotation.
4. **Tool robustness**: `tools/policy_research/p4_force_first.py` verifies engine
   banner and option presence at startup (fails fast on macro-off builds);
   enforces `Threads 1`, `Hash 16`, `MultiPV 1`; token-based UCI parsing asserts
   target depth was reached; parses wall time (ms), nodes, score (cp/mate and bound),
   bestmove, and PV.
5. **Plan §9.4 reference-quality gate**: runs a deeper reference search (D16)
   and verifies that a candidate agrees with reference best move and score
   tolerance (<= 50 cp) before being declared quality-valid.
6. **Test root reservation**: `c1-t-001` was inspected and is marked burned/exploratory.

Engine build: `make research-build ARCH=x86-64-avx2` on clean commit `23a3efdb`;
banner `dev-20260904-23a3efdb`. Full test suite: 92 tests green (7 engine-gated
in `TestForceFirstRootOrder`). Standing zero-regression gate: macro-off
`bench 16 1 10 default depth` = **453 169 nodes**.
Artifacts: `tools/policy_research/runs/policy-research-p4-kickoff-d14-h16-20260904/`
(`p4-isolated.json`, `p4-persistent.json`, schema `policy-research-p4-counterfactual/2`).

### Empirical Results (depth 14, reference depth 16, candidate depth 10, k=4)

**Experiment B: Isolated Target-Depth Override (Pure Counterfactual at Depth 14)**
- `c1-d-001` (development, startpos):
  - Baseline: 43 275 cumulative nodes, 2 680 incremental D14 nodes, 39 ms, score cp 27 (exact), best e2e4.
  - Ref @ D16: 75 655 nodes, 60 ms, score cp 39, best e2e4.
  - Forced `e2e4` (best): 43 275 cumulative / 2 680 incremental nodes (**exact 100% no-op parity**), 35 ms, score cp 27 (−12 cp vs ref).
  - Forced `d2d4`: 45 375 cumulative (+4.8%), 4 780 incremental (+78.4%), 36 ms, score cp 30 (+3 vs base / −9 vs ref).
  - Forced `g1f3`: 45 831 cumulative (+5.9%), 5 236 incremental (+95.4%), 36 ms, score cp 28 (+1 vs base / −11 vs ref).
  - Forced `c2c4`: 43 481 cumulative (+0.5%), 2 886 incremental (+7.7%), 34 ms, score cp 26 (−1 vs base / −13 vs ref).
  - All candidates quality-valid. Baseline best was already optimal: `R_norm (cumul) = +0.000`, `R_norm (incr) = +0.000` across all gates $\pm 25$ to $\pm 100$ cp.
- `c1-v-001` (validation, tactical):
  - Baseline: 20 956 cumulative nodes, 17 285 incremental D14 nodes, 18 ms, score cp 654 (exact), best d4c5.
  - Ref @ D16: 123 243 nodes, 102 ms, score cp 645, best d4c5.
  - Forced `d4c5` (best): 20 956 cumulative / 17 285 incremental nodes (**exact 100% no-op parity**), 19 ms, score cp 654 (+0 vs base / +9 vs ref).
  - Forced `b1c3`: **5 643 cumulative nodes, 1 972 incremental D14 nodes, 5 ms**, score cp 642 (−12 vs base / −3 vs ref).
  - Forced `e1g1`: 5 752 cumulative, 2 081 incremental, 5 ms, score cp 610 (−44 vs base / −35 vs ref).
  - Forced `c4f7`: 6 187 cumulative, 2 516 incremental, 6 ms, score cp 683 (+29 vs base / +38 vs ref).
  - All candidates pass quality gates. Oracle headroom on this tactical root:
    **`R_norm (cumul) = +0.731` (−73.1% cumulative nodes), `R_norm (incr) = +0.886` (−88.6% incremental D14 nodes)**.
    Wall time dropped from 18 ms to 5 ms (tied for fastest). The gain is invariant across all gates $\pm 25$ to $\pm 100$ cp.
- `c1-t-001` (test, burned):
  - Baseline: 26 292 cumulative nodes, 7 742 incremental D14 nodes, 18 ms, score cp 133, best f1e2.
  - Ref @ D16: 128 139 nodes, 95 ms, score cp 168, best f1e2.
  - Forced `f1e2` (best): 26 292 cumulative / 7 742 incremental nodes (**exact 100% no-op parity**), 19 ms, score cp 133 (+0 vs base / −35 vs ref).
  - Forced `f1g2`: 21 802 cumulative (3 252 incremental), score cp 115 (−18 vs base / −53 vs ref; mild drift beyond $\pm 50$ cp gate).
  - Forced `f3d1`: 20 229 cumulative (1 679 incremental), score cp 57 (−76 vs base / −111 vs ref; severe score collapse).
  - Sensitivity analysis across tolerance gates reveals a cost-quality frontier:
    - Gate $\pm 25$ cp: NONE VALID (ref baseline differs by 35 cp).
    - Gate $\pm 50$ cp: only baseline best `f1e2` passes ($R_{\text{norm}} = 0.000$).
    - Gate $\pm 75$ cp: candidate `f1g2` passes ($R_{\text{norm}} = +0.171$ cumul, $+0.580$ incr).
    - Gate $\pm 100$ cp: candidate `f1g2` passes ($R_{\text{norm}} = +0.171$ cumul, $+0.580$ incr).
    - `f3d1` collapses at all gates up to $\pm 100$ cp.

**Experiment A: Persistent Schedule (Overriding at Depths 1..14)**
- Demonstrates iterative-deepening trajectory churn:
  - On `c1-d-001`: forcing `e2e4` drops nodes to 25 317 (−41.5%) because always-first suppresses best-move switches across depths.
  - On `c1-v-001`: `b1c3` took 1 600 nodes, but score drifted to cp 719 (+74 cp vs reference) and fails quality.
  - On `c1-t-001`: `f3d1` exploded to 320 781 nodes (+1120%) and score collapsed to 0 cp.
  - Incremental counts reflect divergent trajectory steps, not common-prefix causal comparisons.

Limitations: n=3 roots is too small for aggregate bootstrap CIs; top-k is a search-informed empirical shortlist; `c1-t-001` is burned. P4.2 must scale to corpus/v2.

---

## Protocol P4.2: Scaled Root Counterfactuals, Depth Ladders, and Search Telemetry

**Objective**: Address all methodological recommendations from the expert review:
1. Scale from n=3 to expanded `corpus/v2` (26 roots with clean unburned test split).
2. Evaluate depth ladders (D12, D14, D16) to establish depth stability.
3. Replace single-shot millisecond timing with multi-trial measurements.
4. Establish the causal search mechanism explaining the large tactical savings.

**Environment & Invariants**:
- Clean research build of Stockfish with SHA256 logged in JSON provenance.
- Fresh engine process per candidate intervention.
- Pinned UCI options: `Threads 1`, `Hash 16`, `MultiPV 1`.
- Dynamic reference depth: strictly greater than target depth (default $D + 2$).
- Schema: `policy-research-p4-counterfactual/4`, `policy-research-causal-decomposition/3`, `policy-research-depth-ladder/2`.

**Corpus Expansion (`corpus/v3`)**:
- 26 positions across 3 strict splits:
  - `development` (10 roots: `c3-d-001`..`c3-d-010`): startpos, Kiwipete, CPW 3/4, Morphy Opera 10...cxb5 and 7...Qe7, Sicilian Najdorf, King's Indian, WAC 001, Lucena position.
  - `validation` (8 roots: `c3-v-001`..`c3-v-008`): Giuoco Piano blunder, CPW 5, Scandinavian check, French Winawer, Tal-Larsen 1965, Petrosian Hedgehog, Philidor 6th-rank defense (Philidor 1777), Bratko-Kopec 02.
  - `test` (8 roots: `c3-t-001`..`c3-t-008`): Kasparov-Topalov 1999, Ruy Lopez Closed, Byrne-Fischer 1956, QGD Classical, Carlsen-Aronian 2015, WAC 002, Bratko-Kopec 01, King and pawns.
  - Every position with `source_line_moves` is 100% byte-for-byte verified via replay from startpos against Stockfish's board parser.
  - Burned exploratory root `c1-t-001` is completely excluded from the test set; all 8 test roots are strictly quarantined.

**Multi-Depth Ladder Results (Full 18-Root Population Evaluation with Fixed D10 Candidates)**:
- Evaluated all 18 development and validation roots across plies D12, D14, D16 using a fixed candidate set generated at D10 MultiPV-4 (`depth-ladder-dev-val.json` via committed runner `tools/policy_research/p4_depth_ladder.py`).
- Aggregate positive opportunities expand monotonically with ply depth: **5/18 (27.8%) at D12**, **9/18 (50.0%) at D14**, and **12/18 (66.7%) at D16**.
- Pooled cumulative savings increase monotonically: **12.1% at D12** (254,354 $\to$ 223,530 nodes), **22.1% at D14** (908,892 $\to$ 707,917 nodes), and **36.9% at D16** (5,060,198 $\to$ 3,194,088 nodes).
- On `c3-d-004` (CPW4, tactical middlegame):
  - D12 (Ref D14): Baseline 8,269 nodes; forced `b4c5` takes 4,082 nodes (**50.6% cumulative reduction**).
  - D14 (Ref D16): Baseline 69,878 nodes (47,379 incr); forced `g1h1` takes 24,972 nodes (2,473 incr), **64.3% cumulative reduction, 94.8% incremental reduction**.
  - D16 (Ref D18): Baseline 1,369,213 nodes; forced `b4c5` takes 265,584 nodes (**80.6% cumulative reduction, saving 1,103,629 nodes**).
- On `c3-v-001` (Giuoco Piano):
  - D12: Baseline optimal (1,948 nodes).
  - D14 (Ref D16): Baseline 20,956 nodes (17,285 incr); forced `b1c3` takes 5,643 nodes (1,972 incr), **73.1% cumulative reduction, 88.6% incremental reduction**.
  - D16 (Ref D18): Baseline 123,243 nodes; forced `b1c3` takes 68,467 nodes (**44.4% cumulative reduction**).

**Common-Prefix Causal Decomposition (Full 2³ Factorial Design: Root Order vs PV-Follow vs Aspiration Depth Control)**:
Evaluated across 9 conditions (8 forced conditions + baseline, schema version 4 in `tools/policy_research/p4_causal_decomp.py`) with verified common prefix (`prev_depth_nodes` 100% byte-for-byte identical across all conditions: 3,671 on `c3-v-001`, 22,499 on `c3-d-004`, 16,131 on `c3-d-002`, 63,292 on `c3-d-007`):
- Orthogonal $2^3$ Factorial Conditions:
  - Condition 1 (`baseline`): Untreated baseline search (all mechanisms operate naturally without forcing).
  - Condition 2 (`joint_intervention`): Forced candidate into `rootMoves[0]`, simultaneously updating root ordering, setting `lastIterationIdxPV = candidate`, and enabling normal aspiration window adjustments and fail-high depth reductions.
  - Condition 3 (`preserve_aspiration`): Forced move with baseline aspiration window center and width (asp=1, pv=0, fhr=0).
  - Condition 4 (`preserve_previous_pv`): Forced move with baseline previous PV reference maintained (`lastIterationIdxPV = baselinePreviousPV`; asp=0, pv=1, fhr=0).
  - Condition 5 (`disable_fail_high_reduction`): Forced move at unreduced nominal depth (disabling `failedHighCnt` reductions; asp=0, pv=0, fhr=1).
  - Condition 6 (`preserve_asp_and_pv`): Forced move with baseline aspiration AND baseline previous PV reference maintained (asp=1, pv=1, fhr=0).
  - Condition 7 (`preserve_asp_and_fhr`): Forced move with baseline aspiration AND unreduced nominal depth (asp=1, pv=0, fhr=1).
  - Condition 8 (`preserve_pv_and_fhr`): Forced move with baseline previous PV reference maintained AND unreduced nominal depth (asp=0, pv=1, fhr=1).
  - Condition 9 (`full_control_nominal_depth`): Forced move with baseline aspiration center, baseline previous PV reference maintained, AND unreduced nominal depth (full orthogonal control; asp=1, pv=1, fhr=1).
- In-Search Mechanisms:
  - *Aspiration-Optimism Coupling*: Stockfish initializes aspiration window bounds and player optimism simultaneously from `avg = rootMoves[pvIdx].averageScore` in `Search::Worker::iterative_deepening()`, computing `optimism[us] = 114 * avg / (abs(avg) + 85)`. Under unconstrained forcing, resetting `avg = rootMoves[0].averageScore` shifts both the window center and the player's optimism factor. Preserving baseline aspiration locks both window center and optimism.
  - *PV-Follow Decoupling*: Downstream PV-following requires `(ss - 1)->currentMove == lastIterationIdxPV[0]` at ply 1 to maintain `ss->followPV = true`. Setting `lastIterationIdxPV = baselinePreviousPV` ensures that if the candidate move differs from the baseline previous PV leader, `ss->followPV` immediately evaluates to `false` at ply 1. This decouples candidate search from the previous PV line through intentional mismatch, suppressing candidate-biased PV tracking without mutating root move ordering (it does not set `rootMoves[0].pv` to baseline PV).
- Empirical Results & Quality Gate Analysis:
  - On `c3-d-004` (CPW4, tactical middlegame): All forced conditions reduce search effort by 63–65%. However, preserving baseline aspiration (Conditions 3, 6, 7, 9) produces score -628 cp. Relative to the D18 reference score of -692 cp, $|\Delta| = 64\text{ cp} > 50\text{ cp}$, which fails the reference quality gate (`quality_valid_ref: false`). Conditions without baseline aspiration preservation (Conditions 2, 4, 5, 8) produce score -654 cp ($|\Delta| = 38\text{ cp} \le 50\text{ cp}$), passing quality validation. Condition 4 (`preserve_previous_pv`) saves **63.64% cumulative / 93.86% incremental nodes** (25,410 vs 69,878 nodes) while passing all quality criteria, demonstrating that significant node reductions persist when candidate PV-following is decoupled.
  - On `c3-v-001` (Giuoco Piano blunder): Under full control at nominal depth 14 with previous PV preserved and fail-high reduction disabled (Condition 9), the intervention saves 36.71% cumulative / 44.51% incremental nodes (13,263 vs 20,956 nodes) while strictly passing quality validation. Joint intervention with fail-high depth reduction (Condition 2) saves 73.07% cumulative (5,643 nodes at D11).
  - On `c3-d-002` (Kiwipete): Disabling fail-high depth reductions inflates search to 78,030 nodes (Condition 7 vs 25,490 baseline; Condition 9 inflates to 74,594), confirming that fail-high depth reductions are essential for controlling alpha-beta branching in sharp tactical trees, and showing that node impacts are strongly root-dependent.
  - On `c3-d-007` (strict candidate `a2a3`): Joint intervention saves 46.56% cumulative (69,237 vs 129,556 nodes); under full control (Condition 9), savings are 34.00% cumulative (85,508 vs 129,556 nodes).
  - *Baseline vs Factorial Comparison*: Baseline (Condition 1) is an untreated reference search where all three mechanisms operate naturally without forcing. Comparing Condition 9 to baseline bundles first-slot move ordering with whatever residual search-path differences exist, rather than isolating an orthogonal move-ordering main effect independent of the forcing context.
  - True no-op control: forcing the **D-1 lead move** (`d_minus_1_lead_move`) is an exact byte-for-byte match to baseline (demonstrated on `c3-d-007`: baseline 129,556 nodes == forced `c1e3` 129,556 nodes). Forcing untreated final best `f2f3` is an active intervention (66,704 nodes) because `f2f3` was not the leader at the start of iteration D14.

**Balanced Latin-Square Multi-Trial Timing Benchmarks**:
- Tooling supports `--trials 12` running a cyclic shift Latin square design where each treatment appears in each position slot across trials (balanced when `trials % num_actions == 0`), mitigating thermal and first-runner cache warming biases (though cyclic designs do not balance pairwise carryover effects).
- Distinct Timing Metrics (Canonical 12-Trial Evidence in `docs/policy-research/evidence/p4-canonical/timing-benchmark-trials12.json`):
  - *Search-Kernel Time Reduction Percentage*: $(T_{\text{base}} - T_{\text{cand}}) / T_{\text{base}}$
  - *Search-Kernel Multiplicative Speedup Factor*: $T_{\text{base}} / T_{\text{cand}}$
  - *End-to-End Wall Time Reduction Percentage*: $(W_{\text{base}} - W_{\text{cand}}) / W_{\text{base}}$ (including process startup, UCI handshake, and ~300 ms fixed memory allocation).
  - *Search Scope Boundary*: Search-kernel timings measure Stockfish's internal search loop (including Stockfish's native NNUE evaluation and MovePicker generation/scoring), but exclude any *future policy network inference and policy move scoring/sorting overhead*.
- On `c3-v-001` (12 counterbalanced runs):
  - Baseline search time: median 19.0 ms (IQR 0.0 ms, mean 18.9±0.5 ms), wall time median 325.4 ms.
  - Candidate `b1c3` search time: median 5.0 ms (IQR 1.0 ms, mean 5.4±0.5 ms), wall time median 311.7 ms.
  - **Search-kernel time reduction: 73.7%** (a **3.80× speedup factor**). Wall time reduction: 4.2%.
- On `c3-d-004` (12 counterbalanced runs):
  - Baseline search time: median 65.5 ms (IQR 3.0 ms, mean 65.6±2.0 ms), wall time median 371.3 ms.
  - Candidate `g1h1` search time: median 23.0 ms (IQR 1.0 ms, mean 23.3±1.1 ms), wall time median 329.1 ms.
  - **Search-kernel time reduction: 64.9%** (a **2.85× speedup factor**). Wall time reduction: 11.4%.

**18-Root Canonical Population Statistics (`corpus/v3` Dev + Val, Schema v4)**:
- **Abstaining Oracle Definition**: The 22.1% pooled cumulative (42.3% target-iteration) savings metric represents an *abstaining oracle* that intervenes only when an evaluated candidate passes quality gates and reduces search cost below baseline, and otherwise abstains (reverting to baseline cost and regret 0.0), even when the untreated baseline itself misses the reference quality gate (as in `c3-v-003` and `c3-v-004`). It does not represent the cost of an oracle constrained to achieve quality-validity on every root.
- 7 Result-preserving savings (38.9%)
- 2 Convergence corrections (11.1%) (`c3-d-007`, `c3-v-006`)
- 6 Baseline optimal (33.3%) (including `c3-d-009` mate in 2)
- 2 Harmful interventions (11.1%) (`c3-v-003`, `c3-v-004`)
- 1 No valid candidate (5.6%) (`c3-d-005`)
- Total positive opportunity: 9 / 18 (50.0%)
- Macro mean savings: 18.3% cumulative, 34.6% target-iteration.
- Macro median savings: 1.8% cumulative, 7.8% target-iteration.
- Pooled savings across 18 roots: 22.1% cumulative (908,892 $\to$ 707,917 nodes), 42.3% target-iteration (475,490 $\to$ 274,515 nodes).
- *Savings Concentration & Heterogeneity*: At D16, two roots (`c3-d-004` saving 1.10M nodes and `c3-d-006` saving 0.38M nodes) account for 79.2% of all pooled savings across the 18 roots; the macro median saving is 13.6% (compared to 36.9% pooled). This demonstrates that depth scaling is heterogeneous and position-dependent across tactical/endgame structures, rather than uniform monotonic scaling across the entire population.

## Protocol P6.3 — corrected scheduled-prefix interaction

- Schema `/5`; `/4` is audit-only.
- Threads 1, Hash 16, fixed depth, fresh process/root, node budget 5,000.
- Reserve the static requested prefix from the replay's real MovePicker before
  any target child search; emit targets in request order; resume at the natural
  suffix cursor.
- Validate `order_valid` and observed-slot prefix on every entry.
- Controls are variable-length `[k,0..k-1]`, not full-K schedules; require exact
  nodes/value/fail-high equality with force-next probes.
- Treat full identity as a committed-prefix intervention, not baseline parity.
- Report raw, classification-preserving and exact-value comparisons separately.

## Protocol P7.1 — game-paired offline and live gate

- Freeze selected FENs with source PGN SHA-256, game index, ply, selection seed
  and ply seed. Each game contributes paired middlegame/endgame roots.
- Hold out every root from a game together. Use disjoint game samples for
  multiple corpus seeds.
- Fit all feature transforms on training groups only. Tune abstention using
  inner game-group predictions for direct-ranking models.
- Charge exact measured action cost and report classification/exact fallbacks,
  group spread and ratio-of-sums.
- After positive held-out capture, freeze one model and test it live on roots
  excluded from model fitting. Use fresh processes, warmup, clear hash,
  Threads 1, fixed depth, randomized paired schedules and repeated trials.
- A live candidate fails if either pooled nodes or pooled wall time regresses;
  do not proceed to Elo merely because offline local replay cost is positive.
