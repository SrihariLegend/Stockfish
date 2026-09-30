# Phase 6.3 corrected scheduled-prefix corpus — schema `/5`

This corpus supersedes the schema `/4` interaction evidence. It uses the
scheduled-prefix semantics in [DESIGN.md](DESIGN.md): reserve the static prefix
from the replay's real MovePicker before any child search, emit the requested
order, then resume from the correct natural suffix cursor.

## Corpus and reproduction

Four depth-8 roots, Threads 1, Hash 16, sample rate 1.0, TopK 4, node budget
5,000, seed 0, research commit `98fa4f9d`:

- canonical `seed_a1` and corrected-FEN `root_b_d8`;
- breadth roots `b1_ruy` and `b3_qgd`.

```sh
python3 tools/policy_research/p6_interaction.py \
  docs/policy-research/evidence/p6-interaction-v2 \
  --json docs/policy-research/evidence/p6-interaction-v2/p6-interaction-results.json
```

`report.txt` is the complete generated text report.

## Validation

- 5,282 decision rows and 44,197 deduplicated scheduled-prefix entries.
- **0 invalid requested prefixes**; every entry has `order_valid=true` and
  every observed slot list is a prefix of its request.
- **15,846/15,846 exact force-next controls** match scalar probes in nodes,
  value and fail-high classification.
- 21,868 entries terminate before serving their complete request; these are
  valid early node terminations, not target-skip mismatches.
- Canonical root-A and root-B `/5` baseline and scalar-probe objects are
  byte-identical to their committed `/3` counterparts: 0 baseline mismatches
  and 0 probe-row mismatches.
- Zero budget censoring.

Committed identity differs from unmodified natural baseline on 670/5,282 rows
and costs 2.03% more pooled. This is expected and is now part of the estimand:
natural search may dynamically skip a later quiet, while committed identity
reserves the entire static top four. Identity is not presented as a no-op.

## Pooled raw order costs

Natural baseline: 54,357 local replay nodes.

| committed full order | cost ratio | save % | FH→FL | FL→FH |
|---|---:|---:|---:|---:|
| identity | 1.020 | -2.03 | 0 | 25 |
| reverse | 1.493 | -49.29 | 87 | 67 |
| rotation | 1.341 | -34.11 | 86 | 78 |
| swap(1,2) | 1.020 | -1.96 | 4 | 29 |
| swap(2,3) | 1.019 | -1.92 | 5 | 25 |
| scalar cheapest-first | 0.774 | +22.59 | 25 | 70 |

Blind full-prefix commitment is expensive. The old apparent near-neutrality of
adjacent swaps was partly an artifact of targets being dynamically skipped;
under an explicit committed prefix both swaps cost about 2% pooled.

## Aligned oracle references

| reference | save % |
|---|---:|
| scalar top-four, unconstrained | 26.56 |
| scalar top-four, classification-preserving | 23.26 |
| scalar top-four, exact value | 12.02 |
| committed cheapest-first, raw | 22.59 |
| committed cheapest-first, classification fallback | 19.86 |
| committed cheapest-first, exact-value fallback | 8.16 |
| best tested full schedule, unconstrained | 27.37 |
| best tested full schedule, classification-preserving | 24.47 |
| best tested full schedule, exact value | 12.67 |

The plan-defined cheapest-first-to-best-tested interaction gaps are 4.78pp raw,
4.61pp with classification fallback, and 4.51pp with exact-value fallback.
They measure both genuine shared-state/order interaction and error in converting
whole-node scalar costs into a complete order.

The stricter answer to “how much does a full schedule add beyond the best
single promotion?” is smaller:

- unconstrained: **0.81pp** (27.37 − 26.56);
- classification-preserving: **1.21pp** (24.47 − 23.26);
- exact value: **0.65pp** (12.67 − 12.02).

Thus the corrected conclusion is stronger than before: most measurable local
headroom lies in choosing the first candidate; committing the rest of the
static prefix adds little aligned oracle value and can itself be harmful.

## Root and opportunity spread

Best-tested full-order saving ranges from 25.66% to 28.88% over the four roots
(root mean 27.34%, SD 1.32%). This is descriptive only.

For baseline ≥10-node rows (1,276 rows; 40,958 baseline nodes):

- scalar classification-preserving oracle: 27.75%;
- committed cheapest-first: 28.96% raw;
- best tested: 33.05% raw, 29.39% classification-preserving, 15.64% exact.

For baseline ≥25-node rows (446 rows; 28,685 baseline nodes), best tested is
38.12% raw, 33.54% classification-preserving and 18.79% exact. These are
hindsight strata; measured baseline cost is not an available policy gate.

## Limitations

This remains a four-root, fixed-depth, Threads-1 opening experiment. Full-order
best is selected ex post from a small battery, not all 24 K=4 permutations and
not a learned policy. Local subtree costs overlap and do not compose into a
root speedup. The later paired-game corpus and live QE experiment broaden the
single-promotion study but do not recollect this expensive full-order battery;
the live policy fails both node and wall-time gates.
