# Schema `/5` scheduled-prefix design

## Why schema `/4` was replaced

Schema `/4` assumed that exhausting a fresh MovePicker before search produced
the first K moves that the live move loop would later emit. That is false after
an earlier move has been searched: shallow pruning may call
`MovePicker::skip_quiet_moves()` and remove a reserved quiet. The `/4` arm then
served a non-target move, marked `fully_served=false`, and the analyzer treated
that as an ordinary early cutoff. In the four-root corpus 3,822/43,762 entries
had a non-prefix execution. `/4` also treated full-K
`[k,0..k-1,k+1..]` as equivalent to force-next even though force-next releases
the suffix to dynamic pruning.

Schema `/5` makes the treatment explicit and testable.

## Static candidate set

At the common decision point, the existing candidate enumerator records the
legal static pre-search MovePicker order. Let its first K moves be
`m[0..K-1]`, with K=min(4,N). These are candidate ranks, not a claim that an
unmodified search will necessarily emit every later rank after dynamic quiet
skipping.

## Scheduled-prefix replay

Before searching any target, the replay's real MovePicker is advanced through
the target set in natural order. Every legal emission must belong to the
requested static prefix; otherwise `order_valid=false`. The picker is now at
the natural suffix cursor.

The root move loop then emits the reserved targets directly in requested order.
All normal slot-dependent logic, pruning, child search, TT writes, histories,
LMR context and cutoff behavior apply. If the node survives the prefix, the
already-advanced real MovePicker supplies the suffix. If the node terminates
after only a prefix of the request, that is a valid early termination and
`fully_served=false`, `order_valid=true`.

Every permutation serializes the observed slots. The analyzer independently
requires those slot ordinals to equal a prefix of `order`.

## Two treatment lengths

### Exact force-next controls

For ordinal k in 1..3, schedule only:

```text
[k, 0, ..., k-1]
```

Then release to the natural suffix. Both the scheduled-prefix replay and the
original force-next replay have consumed the real picker through ordinal k
before any target is searched. They must match nodes, returned value and
fail-high classification bit-for-bit.

### Full top-four schedules

The interaction battery commits all K static candidates:

- identity `[0,1,2,3]`;
- reverse;
- one-step rotation;
- adjacent swaps `(1,2)` and `(2,3)`;
- scalar whole-node-cost cheapest-first.

A committed identity is **not** a no-op control. Natural search can skip a later
quiet, whereas committed identity promises all reached prefix slots. Its cost
is reported as an intervention relative to natural baseline, not used as an
equivalence gate.

## Required gates

A corpus is valid only when:

1. all scalar and scheduled-prefix probes complete within budget;
2. every entry has `order_valid=true`;
3. observed slots are a prefix of the request;
4. every force-next control equals its scalar probe exactly;
5. candidate/probe joins and node-exit accounting pass;
6. canonical `/3` baseline and scalar-probe records remain unchanged.

## Analysis references

All costs are ratio-of-sums. Report separately:

- scalar top-four unconstrained, fail-high-preserving and exact-value oracles;
- committed cheapest-first raw and hindsight class/exact fallback costs;
- best tested full schedule under raw, class-preserving and exact-value rules;
- natural-to-committed-identity effect;
- plan-11.3 interaction gap:
  `C_shared(cheapest-first) - C_shared(best tested full schedule)`.

The interaction gap is not purely a TT effect: sorting whole-node force-first
costs is only one scalar-to-order conversion. Therefore also report additional
best-full headroom over the aligned best scalar oracle.
