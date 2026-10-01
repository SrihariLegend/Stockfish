# 1. Preserve both intercept conventions in the shared stats module

Date: 2026-10-01
Status: Accepted

## Context

The policy-research tooling grew four independent statistical solvers across three
files, and they disagreed about the intercept:

| solver | call site | intercept penalised? |
| --- | --- | --- |
| `p3_dataset._logistic_irls_std` | phase-3 calibration report | **no** |
| `p6_learnability.ridge_logistic` | phase-6 learnability probe | **yes** |
| `p6_learnability.ridge_fit` | phase-6 probe + `export_linear_qe` | **yes** |
| `p7_direct_ranking.WeightedRidge` | phase-7 direct ranking | **no** |

All four combinations of the convention appeared in one codebase. That pattern is the
signature of an accident of authorship rather than a deliberate modelling choice: nothing
in the plan or the experiment protocols distinguishes which of these fits should shrink
its intercept.

The cost was not only inconsistency. `export_linear_qe.py` fits the frozen linear-QE
policy with the **phase-6** solvers and emits `src/policy_research/qe_model.h`, which is
compiled into the engine and drives the live-QE benchmark. The penalised-intercept
convention is therefore baked into a shipped artifact and into the live-benchmark
evidence that depends on it.

At the same time the two formulations are not interchangeable at the bit level. The
phase-3 form assembles a Hessian and gradient, solves for a Newton *step*, and adds it to
the running coefficients, stopping on `max|step| < tol`. The phase-6 form solves
directly for the coefficients from the working response, stopping on
`np.allclose(coef_new, coef, atol=tol, rtol=1e-6)`. They share a fixed point but not an
arithmetic path, so a single rewritten kernel cannot reproduce both exactly.

The branch treats reproducibility as load-bearing: its evidence is numeric, and a silent
change to a coefficient propagates into reported metrics and into a compiled model.

## Decision

Consolidate the four solvers plus the duplicated isotonic, ranking and metric primitives
into `tools/policy_research/stats.py`, with:

- **`penalize_intercept` as an explicit, required argument.** It also selects which of the
  two preserved formulations runs, because each was written for one convention. Both
  formulations are retained verbatim rather than rewritten.
- **No defaults** for `lam`, `tol` or `max_iter`; every call site states what it wants, so
  a future phase cannot silently inherit a convention.
- **Unsupported combinations raise.** A weighted fit with a penalised intercept, or an
  unweighted fit with an unpenalised intercept, is not expressible: the preserved
  formulations do not cover them, and inventing one would produce numbers no published
  artifact was computed with.
- **`auc` requires 0/1 labels** and raises otherwise. A mis-encoded label column must fail
  loudly rather than be silently scored as a positive class.

## Consequences

Positive: no published number moves; the divergence becomes visible at every call site
instead of being distributed across three files; `qe_model.h` regeneration is proven
byte-identical; the strict-label guard turns a silent scoring error into an exception.

Negative and accepted: the module contains two numerical kernels behind one interface, so
it is not "one algorithm". The `penalize_intercept` argument carries two meanings at once
(a modelling choice and a formulation selector), which is the one place where the
exactness requirement leaked into the interface. A future caller wanting a penalised
intercept *and* sample weights cannot have it without adding a genuinely new formulation —
and if that is ever needed, it must be added as a third preserved form with its own
evidence, not by quietly changing one of these.

The same reasoning applies to the other preserved divergences found in this pass, which
are documented in the module docstring rather than here: the two ridge formulations, and
`weighted_quantile`'s normalisation by the last midpoint rather than by the total weight
(correcting it would move `_fit_isotonic_map`'s bin edges and change published
calibration numbers).

## Call-site mapping after the cut-over

| call site | formulation | arguments |
| --- | --- | --- |
| `p3_dataset` calibration fits | incremental Newton | `lam=1e-3, penalize_intercept=False, tol=1e-9, max_iter=80`, weights = node weight / root-balanced |
| `p6_learnability` q-hat fit | working response | `lam=LAMBDA_RIDGE, penalize_intercept=True, tol=1e-8, max_iter=40` |
| `export_linear_qe` q-hat fit | working response | as above (must stay identical: it emits the compiled header) |
| `p6_learnability` cost fit | penalised ridge | `lam=LAMBDA_RIDGE, penalize_intercept=True` |
| `p7_direct_ranking` ridge model | unpenalised weighted ridge | `lam=1.0, penalize_intercept=False`, sample weights |
