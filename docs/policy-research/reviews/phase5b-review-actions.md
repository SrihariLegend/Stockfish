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
- Breadth corpus: ten more independent roots across openings, depth 8,
  seed 0 (`2d77b835`, 12,999 rows; 12 independent roots total with the
  canonicals). Root-clustered analysis over 12 roots: promotion ratio mean
  2.16 sd 0.20; oracle savings mean 23.1% sd 3.6%.
- Remaining: middlegame/endgame roots, more declared seeds per root (tail
  Hájek/HT intervals with known denominators), and the formal Phase-6
  go/no-go report with clustered intervals.

## R4 — Cost as paired delta — DONE with measurement A

Finding: node-count (cost) outcome should be reported per paired delta, not
just fail-high stability.

Action: measurement-A child-node costs plus the existing per-probe totals and
baseline totals give per-row paired deltas (forced minus baseline) for both
the whole node and the candidate's own subtree; analysis scripts in the Phase
6 report will report paired deltas and their distributions (in progress).

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

## Vision / Phase-6 staging — PRELIMINARY GO (corrected /3 evidence)

The corrected /3 measurements sharpen both sides of the economics: blind
promotion costs ~2.2x (not 1.2-1.35x as the pre-fix /2 corpora suggested)
while the classification-preserving local oracle still saves 19-28% of
baseline nodes (21-34% on baseline >= 10-node rows; 9-18% under exact-value
equivalence) — consistent across 12 independent roots. Measurement A shows
the causal core: natural order cuts on its ordinal-0 move in 85-92% of
move-loop cutoffs, and a forced ordinal-1..3 candidate proves the bound on
only 27-35%/19-21%/11-13% of rows after a full unreduced slot-1 search.
The universal context-conditioned proof-scheduler endpoint remains the
target; the decisive Phase-6 question is what share of the oracle gap a
learnable entry-state policy can realize (q/e study), whether top-K
interactions preserve the local savings, and whether inference cost stays
sub-MovePicker-scale.
