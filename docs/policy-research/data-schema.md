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
prove the bound — not a censored observation. Only budget aborts (outcome 3,
`threads.stop` mid-attempt) are right-censored, and only they carry no value
semantics. The decoder cross-checks outcome 1 ⇒ `value >= beta` and outcome
2 ⇒ `value < beta`. No outcome is ever assigned to a move that was not
searched.

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

`overflow == 1` iff `error_code == 1`, and a cap overflow additionally emits an
`ERROR_RECORD` with code 1; decoders require all three to agree.

One run per `go`: each logged `go` opens a fresh run file, and RUN_END closes it
(when the next `go` arrives, research is disabled, or the engine quits). The
corpus protocol keeps one process per root, so a file normally holds one run.

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

- Record lengths and schema versions checked on read; every payload length is
  bounded before any fixed-width field is unpacked.
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
- RUN_END `overflow` must agree with `error_code` and with the presence of a
  code-1 ERROR_RECORD.
- policy version present (may be empty only when explicitly unset).
