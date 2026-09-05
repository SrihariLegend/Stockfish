# Phase 6.3 — shared-permutation interaction-gap measurement: design spec

Status: **implemented and validated** (`1870e277`); first corpus and results in this directory (README.md). The K=1 control question resolved as specified below (identity == baseline serves as the machinery control; the force-next controls are the cross-machinery equivalence). Battery, schema, per-slot attribution and the cheapest-first reference are all implemented as designed; validation caveats (identity/control divergence on ~0.1-0.3% of rows, value-preserving, enumeration-order quirk) are quantified in the README. This is the last missing Phase-6
measurement (plan §11.3) and the decisive input to the go/no-go: whether
local single-candidate savings (Phase 6.2) survive when whole top-K
prefixes are reordered inside one continuing replay (TT writes, history
updates, LMR context, and cutoffs shared between the permuted candidates).

## Why the current data cannot answer this

The /3 force-next probes measure ONE candidate promoted to slot 1 with the
natural prefix buffered and re-searched only after that candidate fails
low. Consequence: a forced probe's cost already includes a full natural-
order tail, and successive candidates are never searched in one shared
trajectory. Simulating "search the node with the first three slots
reordered as [b, a, c]" from single-candidate probes would require an
independence assumption (candidate c's subtree given b then a ran first is
assumed equal to c's subtree given c ran first), which plan §11.3
explicitly rejects: "Retain TT writes between candidates within the
permutation. Use a separate overlay/state clone from other permutations."

## Measurand

For a sampled decision node and a permutation pi of its top-K natural
candidates, define C_shared(pi) = shadow do_moves spent by one whole-node
replay in which the node's move loop searches the K candidates in the order
pi (slots 1..K; slot-1 treatment exactly as today), then continues with the
remaining natural emissions (ordinals K..N-1) if no cutoff occurred. TT
writes, history updates, killer-free LMR context, and (ss+1)->cutoffCnt
accumulate across the K candidates exactly as in a real search. Each pi is
measured on its own fresh isolation (sync, clone, stack rebind, overlay) —
the "separate state clone" of plan §11.3. No buffered-move re-search
happens for the permuted prefix: the K candidates simply occupy the first K
search slots in the order pi. (This differs from force-next semantics,
which only reorders [candidate, ...prefix] by buffering; it IS the genuine
reorder the natural order [0..K-1, K..] would receive under a policy.)

Interaction gap:

    interaction_gap(pi) = C_shared(natural top-K order) - C_shared(pi)

with C_shared(natural) = the existing baseline replay (natural order) when
K = 0 reorders are applied. Reported per row for a fixed battery of
permutations; aggregates summed before division (plan §11.1).

## Move-loop semantics (implementation contract)

Generalize the force-next arm to a PermArm carrying a permutation
`targets[K]` (moves in the order to serve) of the node's top-K natural
emissions. The arm is bound exactly like today (posKey + ply + frame
token). At the armed node's main move loop:

1. If targets remain and the next target is already buffered, serve it from
   the buffer (the buffer holds exactly the natural top-K emissions seen so
   far that are not the next target; linear search over K <= 4 is fine).
2. Otherwise pull the picker's next natural emission m.
   - m is a target: if m is the next target, serve it (moveCount++ and
     proceed through the ordinary loop, including pruning); else buffer it
     (it will be served later from the buffer).
   - m is NOT a target (ordinal >= K): unreachable while targets remain,
     because the picker emits in ordinal order and all targets are in
     0..K-1; assert-guarded.
3. After the last target has been served (or a cutoff ends the node), drain
   the buffer in natural order (they are the remaining top-K members) and
   then continue pulling the picker from ordinal K onward.
4. Cutoff at any point ends the node; buffered and later moves are never
   searched (identical to natural semantics).

Buffer correctness notes inherited from the force-next hardening: pops and
serves only at the armed node's (posKey, ply, token); descendants and
re-entry frames (singular extension, null-move verification) never touch
the arm; slots served from the buffer must NOT advance the picker (the
picker only advances on its own emissions).

Measurement-A attribution applies per permutation replay exactly as today
(first = pi[0]'s own slot-1 outcome; cutoff attribution of the node).

## Permutation battery (plan §11.3 list, K = 3 or 4)

Per decision row, measure a fixed battery (all rows share it so the
interaction gap is comparable):
- natural order (already available as the baseline replay);
- reverse of the top-K (strong perturbation; maximal displacement);
- adjacent swaps (0,1), (1,2), and for K=4 (2,3);
- q/e-motivated orders are NOT available ex ante (no predictor yet): use
  the ex-post cheapest-first order (sort top-K by measured forced cost) as
  the oracle-side reference of the interaction-gap study, clearly labeled
  in-sample; and
- deterministic Gumbel-style perturbation: rotate the top-K by one position
  (k1..kK-1, k0).

Battery cost: K=3 -> 6-7 replays per row including baseline; K=4 -> ~10.
Rows are re-sampled (reuse the same sampling hook) with a smaller census
rate or a max-rows cap for the first corpus.

## Schema /4 rows (decision-permutation)

Reuse the decision-row layout (schema `internal-counterfactual/4`): same
identity keys, candidates enumeration (needed to map moves to ordinals),
and a `"permutations":[{...,"order":[ords],"nodes":...,"completed":...,
"value":...,"fail_high":...,"first":{...},"cutoff":{...}}]` array in place
of `probes`. `baseline` unchanged (it IS C_shared(natural)). No forced
probes, no prefix_pops. node_exit oracle rows unchanged. Selection:
permutation rows are a census of the same eligible decision nodes (no
hash-sampling needed; battery is fixed).

## Validation gates before any evidence is collected

1. Sandbox unit tests: permutation arm with K=1 must reproduce the
   force-next outcome bit-for-bit (K=1 permutation = promotion of one
   candidate without prefix preservation... NOTE: K=1 differs from
   force-next! force-next = [k, prefix 0..k-1]; permutation K=1 = [k, then
   ordinals k+1..]. They are different estimands; document. The identity
   control for permutations is: permutation [0,1,2] (identity on top-3)
   must reproduce the baseline replay bit-for-bit (nodes, value, cutoff
   attribution) on every row.
2. Determinism: same-seed reruns byte-identical under ASLR; armed vs
   unarmed live search identical; bench parity 453,169; TT immutability.
3. Ordinal-0/identity attribution controls as in /3.

## Expected data volume and first corpus

One depth-8 root yields ~1,000-1,500 eligible rows; at K=3 with 7 replays
per row (~1.3x baseline cost each) a first corpus of 6-8 roots is the
target for the go/no-go report. Evidence directory:
`docs/policy-research/evidence/p6-interaction/`.

## Deliverable

The interaction-gap table: per permutation, aggregate (sum) C_shared vs
baseline and vs the ex-post cheapest-first permutation; root-clustered
spread; stratified by baseline size and entry depth. Together with the 6.2
numbers this produces the four plan §11.5 answers (oracle size; contexts;
q/e share; interaction gap) and the final go/no-go for Phases 7-13.
