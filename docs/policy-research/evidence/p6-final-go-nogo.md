# Phase 6 — final report and plan-11.5 go/no-go

Status of the four plan-11.5 questions on the accumulated Phase-6 evidence
(/3 scalar force-next corpora: 12-root, 15,825 rows; p6-explanatory;
p6-learnability LOO probe; /4 shared-permutation corpus: 4 roots, 5,282
rows). All savings are ratio-of-sums over eligible decision nodes; all
measurements are local (per-node), collected offline with Threads == 1 at
fixed depth; the 12-16 root positions are manually selected opening lines
(exploratory, not a random population sample).

## Q1 — How much total cost can an oracle save?

Local, top-four, ex-post: ~24% of eligible subtree nodes
(classification-preserving; 19-30% per root), ~12.8% under exact-value
equivalence, ~29% if whole multi-move orderings may be chosen per row
(shared-permutation corpus). Savings concentrate in nontrivial nodes:
rows with baseline >= 10 nodes are 23.8% of rows and hold ~90% of the
savings; >= 25-node rows hold ~71%.

## Q2 — Which contexts contain the opportunity?

Opportunity correlates with subtree size/entry depth (deeper nodes are
costlier and more reorderable) and with rows whose natural lead move is not
a cut-on-arrival (86.8% of natural cutoffs already happen on ordinal 0).
Whether pre-search features can identify the high-regret contexts at usable
precision is NOT established (see Q3): gating remains the economically
required architecture but its viability is unproven on this corpus.

## Q3 — How much of the oracle is explained by q/e? (learnability)

Ex-post decompositions: perfect own-cut knowledge plus cost-aware
abstention captures 57-65% of the classification-preserving oracle
(13.7-15.7% of 24.0%); ~41% of oracle picks and ~45% of oracle savings
come from continuation effects (promoted candidate does not itself cut
off). Whole-node scalar cost (measurement B) is the dominant ex-post
signal; a q/e-style head restricted to own-cut labels is a minor component.

Learnability probe (root-held-out, abstaining, feature-only ridge models
on pre-search features; 12-fold LOO): **no positive capture**. Tuned
q-gates and cost-gated rules choose full abstention on every fold;
unrestricted cost-argmin loses 16.4%; QE loses 4.3%; even in-sample tuned
rules do not beat baseline. Ranking diagnostics are strong (held-out own-
cut AUC 0.87, log-cost Spearman 0.70) but choice-value is not: the
features cannot resolve whether a later candidate beats THIS row's natural
cost at the precision the asymmetric promotion penalty requires. On the
baseline >= 10 stratum the same rules lose 0.7-1.0% against a 28% oracle.

## Q4 — How large is the TT/order interaction gap?

Measured with the shared-permutation machinery (schema /4): scalar-cost-
ordered shared permutations realize 24.2% pooled vs 29.4% for the per-row
best tested order and 23.3% for the classification-preserving single-
promotion oracle. The interaction headroom beyond scalar costs is ~5-6pp
of eligible subtree nodes — real but secondary; scalar whole-node costs
predict shared-order costs almost fully. Blind orderings cost +27 to +49%;
the (1,2) swap is nearly free (+1%).

## Q5 — How many cycles per policy call can be afforded?

No composable number exists from local sums (eligible-subtree accounting
overlaps ~2.4x root node counts; a deployed policy changes its own call
distribution). Directional bounds: at these depths eligible nodes are
~20-27% of all searched nodes, and the local oracle's average saving is
~2.5 subtree nodes per eligible row (exact-value ~1.3); a universal scorer
must cost a small fraction of one subtree node-equivalent per call, while
stratified invocation on predicted-expensive nodes could afford more. The
affordability test itself requires a live policy run (fixed-depth wall
time with the scorer in the loop) — no such measurement exists yet.

## Go/no-go

- **GO (with strict scoping) to Phase 7-9 research modeling** — survival/
  q/cost heads and a direct-ranking teacher — because: (a) local oracle
  headroom is real, consistent (12-16 roots) and ~60% ex-post attributable
  to cost-aware selection; (b) the interaction gap is small, so scalar-cost
  labels are an adequate teacher target; (c) the current negative
  learnability result is a linear-model result on tabular features and does
  not bound richer models. Phase-7-9 work must be labeled as research:
  no production integration.
- **NO-GO to Phase 10+ (production student / engine integration) until**:
  (1) a stronger model class (nonlinear/graph/neural over entry state)
  demonstrates POSITIVE root-held-out capture on a broader corpus (random
  game-derived middlegame/endgame positions, game-split holdout, multiple
  declared seeds), or plan-21.1-style context restriction (near-root /
  high-regret gated invocation) is shown viable with a cheap gate; (2) a
  live-policy fixed-depth wall-time experiment measures real composable
  savings and per-call cycle cost; (3) search-quality gates (root-result
  agreement, deeper-reference checks) and later Elo/SPRT infrastructure
  exist.
- **Phases 11-13 (integration, DAgger, LMR shadow) stay unstarted**.
- The universal, globally invoked proof-scheduler endpoint remains
  plausible but is NOT supported by current evidence as profitable: every
  measured feature-only rule that actually fires loses, and only ex-post
  (hindsight) rules save. The project's next decisive milestone is a model
  class that converts the strong ranking signal (AUC 0.87 / Spearman 0.70)
  into positive choice value out-of-root.
