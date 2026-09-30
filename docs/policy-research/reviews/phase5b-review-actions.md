# Phase 5B remediation: review-finding actions and status

Tracking document for the post-evidence audit remediation (findings of the
read-only review of the regenerated M2/M3 evidence at `987a2fd6`) plus the
vision/Phase-6 preparation work. Status current as of the listed commits.

## R1 — Measurement A (per-candidate attribution) — DONE (`5da89d37`)

Finding: `probe.fail_high` is the whole reordered node's final outcome, not
proof that the forced candidate itself cut off; causal claims about a
candidate's own cutoff outcome need attribution fields.

Action: schema `internal-counterfactual/3`; every whole-node replay (baseline
and each forced probe) now records, at its root node, through a token-bound
TLS channel (`AttributionScope`):
- `first`: slot-1 emission (forced candidate in forced replays; natural
  ordinal-0 move in the baseline), `emitted`/`searched` flags, the child
  search's shadow do_moves (`child_nodes`; includes the move's own do_move
  and singular-extension work, attempt-logger convention), and the
  parent-relative returned value (`value`);
- `cutoff`: final move-loop cutoff attribution — `cutoff_seen`,
  `cutoff_move`, `cutoff_ordinal` (natural ordinal from the row's candidate
  enumeration), `cutoff_value`, `cutoff_by_first` (the slot-1 move itself
  proved the bound). Fail highs produced before/outside the move loop leave
  `cutoff_seen == false` and are distinguishable from fail lows via
  `fail_high`;
- probes additionally carry `prefix_pops` (buffered prefix moves re-searched
  after the forced candidate failed low).

Validation: ordinal-0 forced replay attribution identical to the baseline
attribution on 860/860 smoke rows; `cutoff_seen` implies `fail_high`
everywhere; sandbox and python suites green; armed-vs-unarmed live search
bit-identical; bench parity 453,169.

## R2 — Live-vs-replay node-count divergence — DONE (`08cb5cdb`)

Finding: baseline-replay vs live-subtree node counts disagreed on `tt_hit`
rows only (root A 23/1,112; root B d8 79/1,103 up to −325; root B d10
141/2,029), and the README's "warm live TT vs cold replay" explanation
conflicted with the overlay's copy-on-first-access implementation.

Root cause (found empirically with default-off diagnostics,
`PolicyResearchDiagMode`): a node's main move loop reads `(ss+1)->cutoffCnt`
for LMR reduction adjustments. That future frame accumulates the fail-high
count of every child searched at the next ply under the node's parent —
INCLUDING children of the node's earlier siblings — while only `(ss+2)` is
zeroed at node entry. `clone_and_rebind_stack` zero-initialized all future
frames, so isolated replays ran with `cutoffCnt == 0` while the live node
saw the sibling leftover (0..6 in practice; nonzero on 9.7% of rows),
shifting LMR depths whenever the live node followed cutting siblings. The
divergence is deterministic and confined to cut/tt_hit nodes (matching the
old evidence pattern); the TT overlay itself was exonerated.

Fix: mirror the live stack's future-frame `cutoffCnt` values during the
rebind (reads in-bounds and deterministic; deeper frames are re-zeroed by
the replayed node's own `(ss+2)` reset before any read).

Validation: baseline-vs-live node counts match on 860/860 rows (was
841/860); live `(ss+1)->cutoffCnt` equals the replay's on 860/860 rows
(per-row parity also recorded as `baseline.ss1_cutoff_cnt`); ordinal-0
forced == baseline identity preserved; armed-vs-unarmed bit-identical; bench
parity; sandbox green.

Note: the /2 evidence corpora (and all node-cost tables derived from them)
are pre-fix and superseded for node counts; regeneration under /3 on the
fixed engine is in progress (see overview.md).

## R3 — Statistical design (breadth, clustering, declared seeds) — PARTLY DONE

Finding: only two independent roots; rows dominated by shallow small nodes;
tail weighting noisy; single seed.

Actions:
- Regenerate the canonical four corpora under /3 on the fixed engine
  (root A d8 seeds 0/1, root B d8/d10 seed 0) — DONE (`11032847`).
- Breadth corpus: ten more separately executed root searches across
  openings, depth 8, seed 0 (`2d77b835`, 12,999 rows; 12 separately
  executed positions total with the canonicals -- manually selected
  opening lines, not a random population sample). Root-clustered analysis
  over the 12 roots (corrected aggregate measures): blind-promotion
  ratio-of-sums at ordinals 1-3 pooled 1.226/1.259/1.295; per-root
  ordinal-1 aggregate mean 1.241 sd 0.093; oracle savings mean 23.1%
  sd 3.6%.
- Remaining: middlegame/endgame roots, more declared seeds per root (tail
  Hájek/HT intervals with known denominators), and the formal Phase-6
  go/no-go report with clustered intervals.

## R4 — Cost as paired delta — DONE with measurement A

Finding: node-count (cost) outcome should be reported per paired delta, not
just fail-high stability.

Action: measurement-A child-node costs plus the existing per-probe totals and
baseline totals give per-row paired deltas (forced minus baseline) for both
the whole node and the candidate's own subtree. Paired-delta means/medians
are reported by p5b_analysis.py's pooled block (e.g. ordinal-1..3 means
2.28/2.73/3.11 nodes on the ten-root breadth set); the Phase-6 reports use
ratio-of-sums as the cost headline and report per-row ratio distributions
only descriptively.

## R5 — Documentation bugs — DONE (`16db2ce2`)

- data-schema.md claimed unselected candidates have `prob == 0`; in fact
  every hash-sampled rest member carries its positive marginal inclusion
  probability (verified N=33 rows, 27 unselected rest members with prob
  2/(N-K)). Corrected wording; HT/Hajek estimators use the known rest size.
- README `seed_a1` raw byte count 13,367,786 → 13,431,039 (verified against
  `root_end.bytes` and the decompressed file).
- "120/1,356 visits collide" → 120 identity keys / 305 visits / 185
  duplicate occurrences.
- README "warm TT artifacts" explanation replaced with the R2 root-cause
  narrative (supersession note marks the /2 corpora pre-fix for node counts).

## Remaining / next steps (vision recommendations)

Completed this round: review-findings fixes (aggregate-cost correction,
censored-probe re-collection at budget 5000, statistical wording) in
`45b0d177`; Phase 6.2 explanatory policy study in `89853d91` (q/e
decomposition: the oracle is dominated by direct whole-node cost, not
cutoff probability; at most ~25% of the 24% oracle is q/e-attributable;
per-invocation economics and the gate + stratified-scorer shape).

Next:
1. Implement the plan §11.3 shared-permutation interaction-gap mode
   (design spec: `evidence/p6-interaction/DESIGN.md`), then collect the
   first p6-interaction corpus and produce the plan §11.5 go/no-go report.
2. Broaden the corpus: real middlegame/endgame roots, more declared seeds,
   tail Hájek/HT estimates with known denominators.
3. Learnability probe: root-held-out predictor over entry-state features
   before any neural work (Phases 7-13 only after the go/no-go clears).

## R6 — Read-only expert review of the corrected /3 evidence and Phase-6.2 (addressed)

Review of the final /3 corpora plus the p6-explanatory study (`45b0d177`,
`89853d91`) identified the following problems/gaps; all are addressed in the
commits of this round:

1. **Phase-6.2 q/e conclusion invalid** — `Q1_CHEAPEST` forced promotion
   whenever an own-cut candidate existed (38% of rows), even at a loss, so
   it is neither a q/e bound nor an upper bound. Corrected policy set with
   abstaining variants (`Q1_CHEAPEST_ABSTAIN` 15.69% pooled,
   `Q1_SAFE_ABSTAIN` 13.66%, classification-safe) shows self-cut knowledge +
   cost-aware abstention captures 57-65% of ORACLE_CLS (24.0%), and
   continuation effects ~41% of picks / 45% of savings — the "at most 25%"
   statement is withdrawn. None of these are yet learnable-shape rules; the
   root-held-out feature-only probe in `p6-learnability/` measures that.
2. **501 baseline rows labeled "pre-loop exits"** — every decision row
   reaches its move loop, so fail highs with `cutoff_seen == false` and an
   unsearched slot-1 emission are in-loop early returns (singular-extension/
   multi-cut probe fail high), a separate proof mechanism for q-labeling.
   Fixed in the v3 README and p5b_analysis --measurement-a output.
3. **FL->FH rows called "improvements"** — reworded everywhere as
   classification changes.
4. **Cycle-budget extrapolation premature** — nested local subtree sums
   (~2.4x actual root nodes) cannot be divided into a per-call budget;
   wording corrected, affordability deferred to a live policy run.
5. **Phase-6.3 design contradictions** — K=1 control, ex-post cheapest-first
   constructibility, per-prefix attribution, and intra-node-vs-global scope
   fixed in the implementation round (see below).
6. **Tooling defects** — p6_explanatory abstention bug + dead code fixed and
   unit-tested (test_p6_analysis_tools.py); p5b_analysis `--census`
   advertisement removed, measurement-A summary implemented, multiple path
   arguments so the 12-root pooled reproduction is one command; README
   reproduction commands corrected; stale wording in this file and the
   overview fixed.
7. **Experiment-design breadth — PARTLY CLOSED** — two disjoint seeded
   samples now provide 48 paired middlegame/endgame roots from 24 actual
   engine games. Both roots from a game are held out together. This removes
   the opening-only and root-leakage defects, but one source match and fixed
   depth/Threads=1 still limit population claims.

## R8 — Shared-permutation interaction gap + final go/no-go — CORRECTED

The original schema `/4` result is superseded. Review found 3,822 replays
whose slots were not a prefix of the request, invalid full-K force-next
controls, and a swap(2,3)-as-cheapest analyzer bug. Schema `/5` reserves the
static prefix before search and uses variable-length `[k,0..k-1]` controls:
5,282/5,282 valid rows, zero invalid prefixes, 15,846/15,846 exact controls,
and canonical `/3` byte parity. Aligned extra best-full-order headroom over
the scalar oracle is only 0.81pp raw, 1.21pp classification-preserving and
0.65pp exact. The plan-defined scalar-order-to-best gap is ~4.5-4.8pp but
also measures scalar-to-order conversion error. Evidence:
`evidence/p6-interaction-v2/`.

## R7 — Learnability probe — CORRECTED AND BROADENED

The original negative result was invalid: models were trained on standardized
features but policy inference used raw features. The scorer now applies each
fold's training transform and a regression test compares single and batch
prediction paths.

- Corrected 12-opening-root LOO: QE +0.60% pooled, positive on 10/12 roots;
  baseline>=10 QE +2.06%. CHEAP is -0.40%, not -16.4%.
- Two disjoint seeded game samples: 48 paired middlegame/endgame roots from
  24 games, held out by game. QE replicates at +0.59% pooled; direct nested
  ridge +0.37%; two-layer MLP (seeds 7+19) +0.18%.
- The positive offline sign satisfies the research prerequisite for a live
  test but captures <3% of the local oracle.
- Live held-out fixed-depth intervention fails decisively: universal QE is
  +5.5% nodes/+52% wall at depth 8 and +21.9%/+74% at depth 12; simple depth
  gates also lose. The revised `evidence/p6-final-go-nogo.md` is therefore a
  NO-GO for universal/production integration and permits only a materially
  different sparse-gating research hypothesis.
