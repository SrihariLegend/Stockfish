# Policy Research — Overview

Status board for the incremental alpha-beta proof-policy project. See
[`plan.md`](plan.md) for the full specification (objectives, engineering rules, phases,
tests, exit gates, definition of done).

## Current state

| Phase | Description | Status |
|---|---|---|
| 0 | Architecture inventory and mutation audit | **Complete — awaiting review** |
| 1 | Deterministic research harness | **Complete** — corpus-v1 runner + run manifest; determinism gate passes (12 roots × depth 11) |
| 2 | Versioned research logging | Not started |
| 3 | Observational dataset and calibration baseline | Not started |
| 4 | Root-level counterfactual experiments | Not started |
| 5 | Internal counterfactual search sandbox | Not started |
| 6 | Oracle / ratio / interaction-gap studies | Not started |
| 7 | Proof-time survival modeling | Not started |
| 8 | Exact and prototype Jacobian experiments | Not started |
| 9 | Teacher model | Not started |
| 10 | Incremental production student | Not started |
| 11 | Conservative engine integration | Not started |
| 12 | On-policy data generation (DAgger) | Not started |
| 13 | LMR shadow modeling | Not started |

## Phase 0 deliverables

- `architecture-inventory.md` — exact code locations, flows, and integration points for
  Search, MovePicker, TT, worker state, Position, and NNUE.
- `search-mutation-audit.md` — every mutable object reachable from recursive search, with
  ownership, update sites, and counterfactual-isolation requirements.
- Open questions list (bottom of `architecture-inventory.md`).

## Phase 0 exit gate

> Do not continue until every mutable object reachable from recursive search has been
> accounted for.

Branch: `policy-research`. Base commit: `06675f70`.
