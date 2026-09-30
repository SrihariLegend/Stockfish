# Policy Research — Versioned data schema

Authoritative binary layout for research logging (Phase 2). Versioned by file
schema and by record schema. The engine never serializes native structs or
`Score`; every value is an explicitly written primitive, little-endian, fixed
width, so padding/ABI/endianness can never change meaning. Move encodings use
`Move::raw()` (u16: to 0–5, from 6–11, promotion 12–13, type 14–15; `none`=0,
`null`=65), and `Value`/`Depth` values are stored as raw native `int`
centipawn / ply integers and normalized only offline.

- File container: `research-log/1`
- Record data schema: `research-data/1`

## File layout

```
magic          8 bytes   'PXRLOG' 0x01 0x00
endian marker  u32       0x01020304
format schema  u16       1
uuid len       u32
uuid           bytes     random run id (header only; never compared)
records...     (see below)
```

Integers are little-endian everywhere. Strings are `u16 length + bytes`
(no NUL, UTF-8). There is no trailing record terminator; the stream is a
sequence of frames until EOF.

## Record frame

```
record type     u16
record schema   u16   (1 = research-data/1)
payload length  u32
payload         bytes
```

## Record types

| id | type                  | emitted by |
|----|-----------------------|------------|
| 1  | RUN_START             | recorder open (first logged `go`) |
| 2  | ROOT_START            | every logged fixed-depth root search |
| 3  | DECISION_POINT        | each sampled eligible node (observational) |
| 4  | MOVE_ATTEMPT          | each searched quiet move in a sampled node |
| 5  | COUNTERFACTUAL_RESULT | reserved (counterfactual phases; not emitted yet) |
| 6  | ROOT_END              | end of root search (flush point) |
| 7  | RUN_END              | run close: `quit`, or when the next `go`/disable finalizes the previous run |
| 8  | ERROR_RECORD          | collection cap or fatal condition |

### RUN_START (1)
```
mode                    u8    (Research::Mode: 0 off ... 6 reduction_shadow)
seed                    u64
sample threshold        u32   (0..1_000_000; sample when hash % 1e6 < threshold)
max records effective   u32   (finite; option value or hard cap)
policy version          string
engine info             string (engine_info(true); embedded commit)
```

### ROOT_START (2)
```
root key                u64   (Zobrist of the root position)
target depth            u32   (requested fixed depth, plies)
root FEN                string
```

### DECISION_POINT (3) — one per sampled eligible node
Eligible node (this phase): non-root, NonPV (null-window), not in check, no
excluded move, main move loop, `go depth` fixed. Tablebases are disabled by
protocol. The FEN lets offline tooling reconstruct the position.

```
root key                u64
node serial             u64   (1-based, per root, in search order)
position key            u64
ply                     i32   (from root)
depth                   i32   (remaining depth at this node, plies)
root iteration depth    i32   (current iterative-deepening depth, plies)
alpha                   i32   (node window at decision)
beta                    i32
static eval             i32   (ss->staticEval at decision)
flags                   u8    bit0 improving, bit1 ttHit, bit2 tt move present
rule50                  u16
side to move            u8    (0 white / 1 black)
FEN                     string
```

`beta - staticEval` and `staticEval - alpha` are derived offline.

### MOVE_ATTEMPT (4) — one per searched quiet move in a sampled node
A "quiet" attempt is any move with `capture_stage == false` (excludes captures,
promotions, en passant; includes quiet checks, flagged separately) that was
actually searched (never pruned/skipped moves). Unsearched moves are never
labeled with an outcome.

```
root key                u64
node serial             u64   (references the enclosing DECISION_POINT)
attempt serial          u64   (per root, 1-based)
move raw                u16   (Move::raw(); decode to UCI with the decision FEN)
quiet ordinal           u16   (1-based among attempted quiet moves at this node)
total attempted         u16   (ss->moveCount at this point, incl. captures/TT)
gives check             u8
is TT move              u8
child search count      u8    (1 or 2: LMR search + optional full-depth re-search;
                              must be ≥ 1 — attempts are written only for moves
                              that were actually searched)
first child depth       i32   (actual depth argument of the first child search; may
                              be ≤ 0 when the child falls into the qsearch
                              boundary path or an instant TT cutoff at child entry)
re-search depth         i32   (-1 if none)
alpha before            i32
beta before             i32
value returned          i32   (raw negamax value of the attempt)
nodes consumed          u64   (nodes spent by this attempt, measured from before the
                              Step-16 singular-extension probe through the child
                              search(es); includes the excluded-search subtree when
                              the TT move is verified for singularity)
outcome                 u8    1 fail-high cutoff / 2 fail-low / 3 aborted (stop)
```

Outcome is derived from the search's own decision rules at non-root NonPV
null-window nodes: an attempt that reaches `value >= beta` ends the loop
(fail-high cutoff = survival event). Otherwise the move was searched at the
recorded child depths and did not reach beta. Because these nodes are
null-window (`beta == alpha + 1`), a NonPV attempt can never "raise alpha"
without cutting off, so a completed fail-low (outcome 2) is an exact negative
event — the move was tried under the engine's reduction schedule and failed to
prove the bound — not a censored observation. Only aborted attempts (outcome 3:
the search stopped mid-attempt with `threads.stop` set) are right-censored, and
only they carry no value semantics. Outcome 3 records no stop *reason* (the
schema has no such field), so downstream analyses cannot separate
`SEARCH_ABORTED` from `BUDGET_CENSORED`: both map to one generic aborted /
right-censored class until a stop-reason field is added. The decoder
cross-checks outcome 1 ⇒ `value >= beta` and outcome 2 ⇒ `value < beta`. No
outcome is ever assigned to a move that was not searched.

Each MOVE_ATTEMPT implies the recorded quiet move was actually searched, so
`child search count ≥ 1`. `nodes consumed` may still be 0: the child search can
return before the child's own move loop (e.g., an immediate TT cutoff at child
entry), which counts no subtree nodes.

### COUNTERFACTUAL_RESULT (5)
Reserved id. Defined in the counterfactual phases; zero occurrences are legal.

### ROOT_END (6)
```
root key                u64
decision count          u64   (DECISION_POINTs this root)
attempt count           u64   (MOVE_ATTEMPTs this root)
```

Decoders count MOVE_ATTEMPT/DECISION_POINT records and require them to equal
these totals; the counts are bookkeeping, not authoritative.

### RUN_END (7)
```
run decision count      u64
run attempt count       u64
overflow                u8    (1 if the collection cap was hit)
error code              u8    (0 none; 1 cap overflow — see ERROR_RECORD)
```

`overflow` is a schema boolean: only the wire values 0 and 1 are legal, and
`overflow == 1` iff `error_code == 1` (error code 0 none / 1 cap overflow). A
cap overflow additionally emits an `ERROR_RECORD` with code 1; decoders require
all three to agree and reject any other wire value.

One run per `go`: each logged `go` opens a fresh run file, and RUN_END closes it
(when the next `go` arrives, research is disabled, or the engine quits). The
file is opened with truncation, so `PolicyResearchLogPath` must be rotated
between `go`s (or per process) — reusing one path for two logged `go`s leaves
only the second run on disk. The corpus protocol keeps one process per root
with a distinct path, so a file normally holds one run.

### ERROR_RECORD (8)
```
code                    u16   (1 = collection cap reached)
message length          u16
message                 string
```

## Sampling (deterministic)

No mutable RNG. SplitMix64 mixing of ordered inputs derived from search state:

```
h = seed
h = mix(h ^ rootKey)
h = mix(h ^ positionKey)
h = mix(h ^ (ply, depth, rootIterationDepth packed))
h = mix(h ^ nodeSerial)
sample iff h % 1_000_000 < threshold
```

`nodeSerial` is assigned in search order at every eligible node, so the sampled
identity is a pure function of the position, the search context, and the seed —
repeated identical runs select identical sample sets. `policyVersion`,
`seed`, and `sample rate` are recorded so any dataset can be regenerated or
compared.

## Determinism contract

For fixed-depth, single-thread runs of one executable:

- Best move, score, PV, and node count must be identical with logging on vs off.
- Decoded record streams (payloads only; file headers and UUIDs excluded) must
  be byte-identical across repeated runs.

## Decoding/validation rules

- Record lengths and schema versions checked on read. The file header, every
  fixed-width payload, and every u16 string length prefix (plus its declared
  span) are bounds-checked before any unpack/slice; malformed input raises a
  decoder `ValidationError`, never a native struct error.
- Move raw values must decode to existing squares and a valid move type.
  Promotion bits hold `PieceType - KNIGHT` (2..5) and decode to the UCI
  suffixes `n/b/r/q`.
- Every MOVE_ATTEMPT.nodeSerial must reference a DECISION_POINT of the *open*
  root; references to decisions of an already-closed root are rejected.
- MOVE_ATTEMPT.childSearchCount must be ≥ 1 (only actually searched moves carry
  an outcome).
- Outcome must agree with the returned value: outcome 1 ⇒ `value >= beta`;
  outcome 2 ⇒ `value < beta`.
- quiet ordinal must be strictly increasing within (root, node).
- outcome ∈ {1, 2, 3}; no unsearched move carries an outcome (attempts are only
  ever written for moves that were searched).
- node costs (nodes consumed) are ≥ 0; a completed attempt may cost 0 subtree
  nodes when its child search returns without entering a node (e.g., an
  immediate TT cutoff at the child's entry).
- RUN_START appears exactly once, as the first record; RUN_END exactly once, as
  the last. ROOT_START/ROOT_END are paired and ordered; root keys unique per
  run; DECISION_POINT and MOVE_ATTEMPT only inside a root.
- ROOT_END decision/attempt counts must equal the counted records; RUN_END
  totals must equal the per-root sums.
- RUN_END `overflow` and `error_code` carry only their schema wire values
  (0/1); any other byte value is rejected, not coerced.
- RUN_END `overflow` must agree with `error_code` and with the presence of a
  code-1 ERROR_RECORD.
- policy version present (may be empty only when explicitly unset).

---

# Internal counterfactual dataset (JSONL, schema `internal-counterfactual/3`)

Version 3 adds the measurement-A per-candidate attribution (slot-1 outcome +
final move-loop cutoff attribution on every replay, `baseline.first`,
`baseline.cutoff`, per-probe `first`/`cutoff`/`prefix_pops`) and the
`baseline.ss1_cutoff_cnt` field (replay/live stack-state parity check), and
fixes the replay/live determinism gap documented under node_exit (sibling
(ss+1)->cutoffCnt mirroring). Version 2 corpora remain readable under their
own schema; consumers must treat pre-`08cb5cdb` forced-probe evidence
(especially `fe046e92`) as superseded and pre-`08cb5cdb` node-count evidence
as pre-fix (replays lacked the sibling fail-high context, so replay node
counts diverged from live subtrees on tt_hit cut nodes).

Separate versioned artifact from the binary recorder above. Produced by the
phase-5 internal counterfactual instrument (`PolicyResearchMode=internal_counterfactual`),
which arms exactly one writer per `go` (observational binary logging and this
dataset are never active together). One self-contained file per root search,
truncated on each armed `go`; the file is UTF-8 JSON Lines (one JSON object per
line, `\n` terminated, no pretty printing).

Row type sequence per file: exactly one `run_start` (line 1), exactly one
`root_start` (line 2), zero or more interleaved `decision`/`node_exit` rows,
exactly one `root_end` as the final line. `root_end` is always written —
including when a decision-row cap or byte budget stopped collection — and
carries the accounting for the file. `decision` rows are written synchronously
by the sampling hook; each recorded decision spawns exactly one `node_exit`
audit row, written later when that live node's search frame exits.

## Lifecycle rows

### run_start
```json
{"schema":"internal-counterfactual/3","type":"run_start",
 "engine":"<engine_info(true)>","seed":<u64>,"sample_rate":<0<r<=1 double>,
 "top_k":<int>,"node_budget":<u64>,"max_records":<u32 effective cap>}
```

### root_start
```json
{"schema":"internal-counterfactual/3","type":"root_start",
 "root_key":<u64>,"target_depth":<int, 0 for fixed-node go>,
 "target_nodes":<u64, 0 for fixed-depth go>,"fen":"<root FEN>"}
```

### root_end
```json
{"schema":"internal-counterfactual/3","type":"root_end","root_key":<u64>,
 "rows":<u64 decision rows in file>,"bytes":<u64 total file bytes incl. lifecycle>,
 "overflow":<bool>,"io_failed":<bool>,"target_depth":<int>,"achieved_depth":<int>,
 "target_nodes":<u64>,"searched_nodes":<u64>,"completed":<bool>,
 "best_move":"<uci|null>","best_value":<int|null>}
```
`rows` counts decision rows only; `bytes` counts every row including lifecycle
rows and is defined as the file size after this row (computed by fixed-point
iteration inside the writer; consumers may verify `bytes == file size`).
`completed` is true when the root search reached its target
(`achieved_depth >= target_depth` for fixed-depth roots, `searched_nodes >=
target_nodes` for fixed-node roots). `best_move`/`best_value` are the final
live best move and its raw internal Value; `best_value` null iff no searched
move existed at root end. Decision-row caps (`max_records`, effective hard cap)
and the hard byte budget apply to decision rows; lifecycle rows bypass them —
`root_end` itself is never dropped. `overflow` is true when the dataset is
truncated: a sampling hit arrived after the row cap or byte budget was already
reached and its decision row was skipped (the sampler keeps skipping cheaply,
`would_record()` goes false, and `note_row_skipped()` raises the flag), or a
row was dropped by `emit_row_locked`. `io_failed` reports any file error.

## decision rows

One row per sampled eligible internal decision node whose baseline replay
reached the node's main move loop (the decision capture fired) and whose
selected forced replays consumed the force-next arm (`forced_slot1 == true`
for at least one probe). Eligibility: NonPV null-window (`alpha + 1 == beta`),
node not in check, no excluded move, rootDepth > 0, main thread, not inside a
shadow probe, safe environment (`Threads == 1` in options and pool),
fixed-depth or fixed-node limits with no clock/movetime/infinite, dataset
armed and caps open, live stop not raised. The row is written synchronously at
the live node by the research hook (`Research::on_internal_node_counterfactual`)
after all replays; endpoint non-mutation is asserted in research builds (live
pos key, worker node counter, base TT bytes).

Common fields: `schema`, `type:"decision"`, `root_key` (the immutable root
identity captured at root start — `InternalDatasetLog::current_root_key()`,
equal to the `root_start`/`root_end` root_key on every row of the file, NOT
the sampled node's key), `pos_key` (internal `Position::key()` of the decision
node), `fen`, `ply`, `entry_depth` (remaining depth at the node's **entry**;
this is the replay depth and the sample-identity depth), `depth` (remaining
depth at the **decision point**, i.e. the main move loop head where the
capture fired), `root_depth`, `alpha`, `beta`, `static_eval` (null when the
decision point had no static eval), `improving`, `tt_hit`, `tt_move` (null
when none), `cut_node`, `rule50`, `fullmove`, `sample_seed` (deterministic
sample identity; NOT unique per visit), `sample_id` (unique per-visit id,
monotonic within the run starting at 1, reset at every root start; the join
key to the row's `node_exit`), `sample_rate`, `selection`
(`"probe_all"` | `"topK_plus_hash_sample"`), `n_candidates`, `node_budget`
(finite per-replay budget applied to this node).

The decision-point metadata (`static_eval`, `improving`, `tt_hit`, `tt_move`,
`decision_point_nodes`) is captured **inside the unforced baseline replay** at
that replay's own decision point (`DecisionCapture`, armed only for the
baseline), never from the live node — the whole-node replay from true search
entry is the estimand vehicle, and its pre-loop work is observable.

### baseline
```json
{"nodes":<u64>,"decision_point_nodes":<u64>,"ss1_cutoff_cnt":<int>,
 "completed":<bool>,"stop":"none|budget|user","budget_hit":<bool>,
 "value":<int|null>,"fail_high":<bool|null>,
 "first":<measurement-A slot-1 attribution>,
 "cutoff":<final-cutoff attribution>}
```
Unforced whole-node replay of the same node from its true entry state
(measurement-B reference; ordinal-0 forced replay is bit-identical).
`nodes` counts shadow do_moves for the whole replay (pre-loop work + subtree,
all strictly below the target node); `decision_point_nodes` splits off the
replay-relative do_moves spent before the main move loop head
(`0 <= decision_point_nodes <= nodes`). `value` null iff the replay was
censored or stopped; `fail_high` null under the same conditions.
`ss1_cutoff_cnt` is the fail-high count the replay's own move loop observed
on its `(ss + 1)` frame at the decision point (mirrored from the live stack
by the frame rebind; equals the live value on every row by construction
post-`08cb5cdb`). `first`/`cutoff` are the measurement-A attribution objects
described under the next heading.

### Measurement-A attribution (schema /3; present on baseline and every probe)

`probe.fail_high` is the whole reordered node's final outcome; it does NOT by
itself prove the forced candidate cut off. The `first` object describes the
node's slot-1 emission -- the forced candidate in forced replays, the natural
ordinal-0 move in the baseline replay -- and the `cutoff` object describes
the final move-loop cutoff:
```json
"first":{"emitted":<bool>,"move":"<uci|null>","searched":<bool>,
         "child_nodes":<u64|null>,"value":<int|null>}
"cutoff":{"cutoff_seen":<bool>,"cutoff_move":"<uci|null>",
          "cutoff_ordinal":<int|null>,"cutoff_value":<int|null>,
          "cutoff_by_first":<bool>}
```
- `first.emitted`: the root's move loop accepted a slot-1 emission (always
true for kept rows, which require the move loop).
- `first.searched`: the slot-1 move passed the ordinary per-move prunes and
its child search ran; a pruned slot-1 candidate is emitted-but-not-searched
(that IS its slot-1 outcome).
- `first.child_nodes` (non-null iff searched): shadow do_moves from the
slot-1 emission through its child search (includes the move's own do_move
and any singular-extension verification work, matching the attempt-logger
convention). `first.value`: the parent-relative value the slot-1 child
search returned (the value the move loop then used for its cutoff check).
- `cutoff.cutoff_seen`: true only when the root's own move loop ended in a
cutoff (`value >= beta` break), the dominant fail-high path. Fail highs
produced before/outside the move loop (TT cutoffs, razor/futility/ProbCut
returns) leave `cutoff_seen == false` with a null cutoff move; they remain
distinguishable from fail lows via `fail_high == true`.
- `cutoff.cutoff_move`/`cutoff_ordinal`: which legal candidate cut off and
its natural MovePicker ordinal (resolved against the row's `candidates`).
- `cutoff.cutoff_by_first`: the cutoff happened on the slot-1 move itself,
i.e. the forced candidate (or the natural lead move in the baseline)
actually proved the bound. The whole-node `fail_high` can hold while
`cutoff_by_first == false` (the slot-1 move failed low and a later move --
possibly a re-searched prefix/suffix emission -- produced the cutoff).

### candidates
Full legal denominator in exact MovePicker emission order, ordinal = search-
visible slot index (0-based; illegal emissions skipped without counting):
```json
{"move":"<uci>","ordinal":<int>,"stage":"tt|good_capture|bad_capture|good_quiet|bad_quiet",
 "stage_score":<int|null>,"main_hist":<int>,"capture_hist":<int|null>,"pawn_hist":<int|null>,
 "cont_hist":<int|null>,"low_ply_hist":<int|null>,"see_bucket":<-1|0|1>,"check":<bool>,
 "capture":<bool>,"tt_move":<bool>,"prob":<double>,"selected":<bool>}
```
Features mirror the picker's inputs read from the shadow worker's own state at
the baseline replay's decision point; nulls mark features not defined for that
emission class (`stage_score` null for TT emissions; captures never carry
pawn/continuation/low-ply history; low-ply history only below
`LOW_PLY_HISTORY_SIZE`). `see_bucket` is the SEE bucket of the stage
(−1/0/+1 for see < 0 / ~0 / > 0). `prob` is the **marginal inclusion
probability** of the candidate in the selection: 1.0 for probe-all members and
top-K members, 2/(N−K) for every member of the hash-sampled rest when `N > 8`
(1.0 when only one rest member exists). Every rest member carries its
positive marginal probability -- including members that were not drawn -- so
Hájek/HT estimation with the known rest size can use every row; there is no
`prob == 0` row content. `selected` marks the members actually replayed this
row.

### probes
One entry per executed forced replay (baseline excluded), in selection order:
```json
{"move":"<uci>","prob":<double>,"nodes":<u64>,"completed":<bool>,
 "stop":"none|budget|user","budget_hit":<bool>,
 "value":<int|null>,"forced_slot1":<bool>,"fail_high":<bool|null>,
 "first":<measurement-A slot-1 attribution>,
 "cutoff":<final-cutoff attribution>,"prefix_pops":<int>}
```
Each forced replay runs the whole node from its true entry with the candidate
emitted as slot 1 (force-next reorder: the candidate is searched first; every
natural emission before it in picker order is buffered, not deleted, and is
re-searched after the forced move fails low — prefix-preserving semantics).
`nodes` is the replay-relative shadow do_move count. `forced_slot1` is true
iff the replay reached the main move loop and consumed the force-next arm.
Censored probes (budget or live stop) carry `value == null` and
`fail_high == null`. Rows whose every probe has `forced_slot1 == false` are
degenerate and are dropped, never written. `first` describes the forced
candidate's own slot-1 outcome; `cutoff` the final move-loop cutoff with
`cutoff.cutoff_by_first == true` iff the forced candidate itself cut off
(see the measurement-A heading above). `prefix_pops` counts the buffered
prefix moves the node re-searched after the forced candidate failed low
(0 when the candidate cut off, when no natural emission preceded it, or when
the node returned before the move loop).

### node_exit (audit; one per recorded decision)
```json
{"schema":"internal-counterfactual/3","type":"node_exit","root_key":<u64>,
 "pos_key":<u64>,"ply":<int>,"entry_depth":<int>,"sample_seed":<u64>,
 "sample_id":<u64>,"live_subtree_nodes":<u64>}
```
Written by `LiveExitScope` when the recorded live node's search frame exits
(i.e. its subtree has fully executed) — after the decision row, so consumers
join `node_exit` to `decision` on `sample_id` within the file's root (one
sample id per decision row; ids are unique per run and repeated visits never
collide — the `(pos_key, ply, entry_depth, sample_seed)` identity alone can
collide when the same node is visited more than once at equal depth (e.g. 120
identity keys covering 305 root-A visits). It records the real live subtree
node count so live-vs-replay cost can be validated.

**Live-vs-replay determinism (post-`08cb5cdb`):** the baseline replay node
count matches the live subtree node count on every row. The pre-fix
mismatches (observed only on `tt_hit` nodes; live counts up to hundreds of
do_moves smaller) were NOT warm-vs-cold TT artifacts: a node's move loop
reads `(ss+1)->cutoffCnt` for LMR adjustments, and that frame accumulates the
fail-high count of every child searched at the next ply under the node's
parent — including children of the node's EARLIER SIBLINGS — while only
`(ss+2)` is zeroed at node entry. The frame rebind zero-initialized future
frames, so replays ran with `cutoffCnt == 0` while the live node saw the
sibling leftover, shifting LMR depths whenever the node followed cutting
siblings (concentrated on cut/tt_hit nodes, matching the old evidence
pattern). `clone_and_rebind_stack` now mirrors the live future-frame
`cutoffCnt` values (deeper frames are re-zeroed by the replayed node's own
`(ss+2)` reset before any read), and `baseline.ss1_cutoff_cnt` makes the
parity verifiable per row.
Like other non-decision rows it bypasses the decision cap, but it only exists
for recorded decisions (a skipped decision never pushes the oracle).

## Selection probabilities and determinism

The sample sub-hash is SplitMix64-style mixing of the documented root sampling
seed with the candidate ordinal (`ds_subhash(seed, ordinal)`); the two marginal
candidates are the lowest two sub-hash indices of the non-top-K rest. No
mutable RNG anywhere; identical runs select identical sets. `2 <= N <= 8`
probes all at probability 1; `N > 8` probes `K = min(TopK or 4, N-1)` top
emissions at probability 1 plus one/two marginal candidate(s) at
`2/(N-K)` (marginal probability 1 when only one exists).

## Validation rules (decoder/tests)

- Line 1 `run_start`, line 2 `root_start`, last line `root_end`; type sequence
  otherwise free, all rows carry `schema == "internal-counterfactual/3"`.
- `root_end.rows` equals the number of `decision` rows in the file;
  `root_end.bytes` equals the file size; `root_end.overflow`/`io_failed`
  consistent with row presence and diagnostics; `root_end` targets
  (`target_depth`/`target_nodes`) equal the `root_start` targets.
- Every decision row: candidates non-empty, ordinals `0..n-1` contiguous and
  matching the fresh pre-search MovePicker enumeration order, exactly the legal-move count of the recorded FEN at
  the recorded side to move; selected/probed candidates have `prob > 0`; probes
  reference candidate moves; every kept row has at least one `forced_slot1`.
- `baseline.decision_point_nodes <= baseline.nodes`.
- Every `decision` has exactly one `node_exit` with the same `sample_id`;
  sample ids in the file are unique and monotonic from 1; `root_key` on every
  decision/node_exit row equals the `root_start` root_key.
- Censored probes (incomplete/stopped) carry `value == null` and
  `fail_high == null`; completed probes carry integer `value` and boolean
  `fail_high`.

# Scheduled-prefix extension (JSONL, schema `internal-counterfactual/5`)

Schema `/5` extends `/3` decision rows with `permutations`. Schema `/4` is
superseded and must not be used for quantitative claims: it allowed dynamic
`skip_quiet_moves()` to erase requested targets and conflated that mismatch
with ordinary early cutoff.

Each `/5` permutation object contains:

```json
{
  "order": [2, 0, 1, 3],
  "nodes": 42,
  "completed": true,
  "stop": "none",
  "budget_hit": false,
  "value": 31,
  "fail_high": true,
  "served": 2,
  "fully_served": false,
  "order_valid": true,
  "slots": [
    {"slot": 1, "move": "...", "ordinal": 2,
     "searched": true, "child_nodes": 17, "value": 31}
  ],
  "first": {},
  "cutoff": {}
}
```

`order` is a schedule over the static pre-search candidate ordinals. Before
searching a target, the replay's real MovePicker consumes the entire requested
natural prefix, leaving its cursor at the correct suffix. Reserved targets are
then emitted in requested order through the ordinary search loop. A node may
terminate after a proper prefix; therefore `fully_served=false` is valid only
when `slots[*].ordinal` still equals a prefix of `order` and
`order_valid=true`.

Battery entries are:

- force-next controls `[k,0..k-1]`, after which the natural suffix resumes;
- committed full-K identity, reverse, rotation and adjacent swaps;
- committed full-K scalar-cost cheapest-first.

Committed identity is not required to equal natural baseline because natural
search may dynamically skip a later quiet. Every force-next control is required
to equal its `/3` scalar probe exactly in nodes, value and fail-high status.
The analyzer rejects an entire decision row on any missing/censored order,
invalid observed prefix, duplicate order or failed control.
