"""Shared statistical primitives for the policy-research tooling.

One home for the ridge / IRLS-logistic / isotonic / ranking / metric
primitives that the phase-3 report, the phase-6 probes and the phase-7
ranking experiment previously each implemented (or copied) for themselves.

Design contract
---------------

**Inputs carry no intercept column.** Every fittable entry point takes ``X``
without an intercept and returns ``(coef, intercept)`` as separate values, so
the intercept's treatment is always visible at the call site.

**No defaults.** ``lam``, the convergence tolerance and the iteration cap are
required keyword arguments. A caller states what it wants; nothing is
inherited silently.

**Penalty placement selects one of two preserved numerical formulations.**
``penalize_intercept`` is not only a modelling choice here: the two
formulations below are numerically distinct (different matrix assembly and a
different update), and published artifacts were produced by each, so both are
retained verbatim rather than rewritten.

* ``penalize_intercept=True``  -- the *working-response* form used by the
  phase-6 learnability probe and by ``export_linear_qe.py``, which generates
  the ``qe_model.h`` compiled into the engine. Intercept column last, penalty
  on every diagonal, coefficients solved directly, stopping on
  ``np.allclose(coef_new, coef, atol=tol, rtol=1e-6)``. Sample weights are not
  supported (raise).
* ``penalize_intercept=False`` -- the *incremental Newton* form used by the
  phase-3 calibration report and the phase-7 direct-ranking ridge. Intercept
  column first, penalty on feature coefficients only, solved as a step added
  to the running coefficients, stopping on ``max|step| < tol``. Sample weights
  are supported and normalised to sum to the row count.

An unsupported combination raises rather than silently picking a formulation.
See ``docs/adr/0001-preserved-intercept-conventions.md`` for why this
divergence is deliberate and which call site depends on which convention.

**Strict labels.** ``auc`` requires labels already encoded as 0/1 and raises
otherwise; a mis-encoded label column must fail loudly rather than be silently
scored as a positive class.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "irls_logistic",
    "ridge_fit",
    "pav_isotonic",
    "average_ranks",
    "auc",
    "brier",
    "log_loss",
    "ece",
    "weighted_std_stats",
    "weighted_quantile",
]


# ---------------------------------------------------------------------------
# Ridge
# ---------------------------------------------------------------------------

def ridge_fit(X, y, *, lam, penalize_intercept, weights=None):
    """Ridge regression. Returns ``(coef, intercept)``.

    ``X`` carries no intercept column. See the module docstring for the two
    preserved formulations and which one ``penalize_intercept`` selects.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    Xa = np.hstack([X, np.ones((len(X), 1))])

    if penalize_intercept:
        if weights is not None:
            raise ValueError(
                "the preserved penalized-intercept ridge formulation is "
                "unweighted; pass penalize_intercept=False for a weighted fit")
        XtX = Xa.T @ Xa
        XtX.flat[:: XtX.shape[0] + 1] += lam
        coef = np.linalg.solve(XtX, Xa.T @ y)
        return coef[:-1], coef[-1]

    if weights is None:
        raise ValueError(
            "the preserved unpenalized-intercept ridge formulation requires "
            "weights; pass penalize_intercept=True for an unweighted fit")
    w = np.asarray(weights, dtype=np.float64)
    sw = np.sqrt(w)[:, None]
    A = (Xa * sw).T @ (Xa * sw)
    reg = np.eye(A.shape[0]) * lam
    reg[-1, -1] = 0.0
    coef = np.linalg.solve(A + reg, (Xa * sw).T @ (y * sw[:, 0]))
    return coef[:-1], coef[-1]


# ---------------------------------------------------------------------------
# Logistic regression
# ---------------------------------------------------------------------------

def irls_logistic(X, y, *, lam, penalize_intercept, tol, max_iter,
                  weights=None):
    """Ridge-penalized IRLS logistic regression.

    Returns ``(coef, intercept, iters, converged)`` with ``coef`` excluding the
    intercept. ``X`` carries no intercept column. See the module docstring for
    the two preserved formulations and which one ``penalize_intercept``
    selects.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    if penalize_intercept:
        return _irls_working_response(X, y, lam=lam, tol=tol,
                                      max_iter=max_iter, weights=weights)
    return _irls_incremental_newton(X, y, lam=lam, tol=tol, max_iter=max_iter,
                                    weights=weights)


def _irls_working_response(X, y, *, lam, tol, max_iter, weights):
    """Intercept column last, every diagonal penalized, coefficients solved
    directly from the working response; stops on np.allclose.

    Preserved verbatim from the phase-6 ``ridge_logistic`` so that the frozen
    ``qe_model.h`` and every phase-6/7 diagnostic reproduce bit-for-bit.
    """
    if weights is not None:
        raise ValueError(
            "the preserved working-response logistic formulation is "
            "unweighted; pass penalize_intercept=False for a weighted fit")
    Xa = np.hstack([X, np.ones((len(X), 1))])
    w = np.zeros(Xa.shape[1])
    iters = 0
    converged = False
    for it in range(max_iter):
        iters = it + 1
        z = np.clip(Xa @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        s = np.clip(p * (1 - p), 1e-9, None)
        zstar = z + (y - p) / s
        W = np.sqrt(s)
        Xw = Xa * W[:, None]
        H = Xw.T @ Xw
        H.flat[:: H.shape[0] + 1] += lam
        w_new = np.linalg.solve(H, Xw.T @ (W * zstar))
        if np.allclose(w_new, w, atol=tol, rtol=1e-6):
            w = w_new
            converged = True
            break
        w = w_new
    return w[:-1], w[-1], iters, converged


def _irls_incremental_newton(X, y, *, lam, tol, max_iter, weights):
    """Intercept column first and NOT penalized; the Newton step is solved and
    added to the running coefficients; stops on max|step| < tol.

    Preserved verbatim from the phase-3 ``_logistic_irls_std`` so the
    calibration report reproduces bit-for-bit.
    """
    Xb = np.column_stack([np.ones(len(X)), X])
    pen = np.zeros(Xb.shape[1])
    pen[1:] = lam
    wt = np.ones(len(y), dtype=np.float64) if weights is None \
        else np.asarray(weights, dtype=np.float64)
    wsum = float(wt.sum())
    if wsum > 0:
        wt = wt * (len(y) / wsum)
    beta = np.zeros(Xb.shape[1])
    iters = 0
    converged = False
    for it in range(1, max_iter + 1):
        iters = it
        eta = Xb @ beta
        p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        H = (Xb * (wt * p * (1 - p))[:, None]).T @ Xb + np.diag(pen)
        g = Xb.T @ (wt * (y - p)) - pen * beta
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        beta = beta + step
        if np.max(np.abs(step)) < tol:
            converged = True
            break
    return beta[1:], beta[0], iters, converged


# ---------------------------------------------------------------------------
# Isotonic regression
# ---------------------------------------------------------------------------

def pav_isotonic(y, w):
    """Nondecreasing isotonic regression over weighted means (small arrays).

    Classic PAV: block means that violate monotonicity are pooled. Returns the
    fitted (calibrated) value for each input position.
    """
    vals = np.asarray(y, dtype=np.float64).copy()
    wt = np.asarray(w, dtype=np.float64).copy()
    n = len(vals)
    out = np.empty(n)
    if n == 0:
        return out
    starts = list(range(n))
    bv = vals.copy()
    bw = wt.copy()
    while True:
        merged = False
        new_s, new_bv, new_bw = [starts[0]], [bv[0]], [bw[0]]
        for i in range(1, len(starts)):
            # means of last block and current block
            m_prev = new_bv[-1] / new_bw[-1]
            m_cur = bv[i] / bw[i]
            if m_prev <= m_cur:
                new_s.append(starts[i]); new_bv.append(bv[i]); new_bw.append(bw[i])
            else:
                # pool current into previous block
                new_bv[-1] += bv[i]
                new_bw[-1] += bw[i]
                merged = True
        starts, bv, bw = new_s, new_bv, new_bw
        if not merged:
            break
    pos = 0
    for blk in range(len(bv)):
        end = starts[blk + 1] if blk + 1 < len(starts) else n
        m = bv[blk] / bw[blk]
        out[starts[blk]: end] = m
    return out


# ---------------------------------------------------------------------------
# Ranking and metrics
# ---------------------------------------------------------------------------

def average_ranks(x):
    """Zero-based average ranks (ties receive their group's mean rank)."""
    x = np.asarray(x)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    i = 0
    while i < len(x):
        j = i + 1
        while j < len(x) and x[order[j]] == x[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0
        i = j
    return ranks


def auc(labels, scores):
    """Mann-Whitney U AUC with tie-aware average ranks.

    ``labels`` MUST already be encoded as 0/1; anything else raises, so a
    mis-encoded label column fails here instead of being silently scored.
    Returns NaN when either class is empty.
    """
    lab = np.asarray(labels)
    bad = None
    if lab.dtype == bool:
        pass
    elif np.issubdtype(lab.dtype, np.number):
        flat = lab.reshape(-1)
        ok = (flat == 0) | (flat == 1)
        if not bool(np.all(ok)):
            bad = np.unique(flat[~ok])
    else:
        bad = np.unique(lab)
    if bad is not None:
        raise ValueError(
            f"auc labels must be 0/1; found {list(bad)[:8]!r}. Re-encode the "
            "label column rather than coercing it.")

    s = np.asarray(scores, dtype=np.float64)
    b = lab.astype(bool)
    npos = int(b.sum())
    nneg = len(b) - npos
    if npos == 0 or nneg == 0:
        return float("nan")
    ranks = average_ranks(s)
    return float((ranks[b].sum() - npos * (npos - 1) / 2.0) / (npos * nneg))


def brier(y, p):
    y = np.asarray(y, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    return float(np.mean((y - p) ** 2))


def log_loss(y, p):
    y = np.asarray(y, dtype=np.float64)
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


def ece(y, p, *, bins):
    """Expected calibration error over ``bins`` equal-width confidence bins."""
    y = np.asarray(y, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if n == 0:
            continue
        acc = float(y[m].mean())
        conf = float(p[m].mean())
        total += (n / len(y)) * abs(acc - conf)
    return float(total)


# ---------------------------------------------------------------------------
# Weighted descriptive statistics
# ---------------------------------------------------------------------------

def weighted_std_stats(X, row_w):
    """Weighted feature mean / population sd used for standardization."""
    X = np.asarray(X, dtype=np.float64)
    row_w = np.asarray(row_w, dtype=np.float64)
    tot = float(row_w.sum())
    mu = np.dot(row_w, X) / tot
    sd = np.sqrt(np.dot(row_w, (X - mu) ** 2) / tot)
    sd[sd == 0] = 1.0
    return mu, sd


def weighted_quantile(x, row_w, q):
    """Weighted quantile (midpoint-CDF generalized inverse).

    c_i = (weighted prefix up to i-1 + prefix up to i) / 2, normalized by the
    LAST midpoint c_{n-1} — not by the total weight — so the abscissa spans
    (0, 1] and ``q = 1`` returns the largest sample while ``q`` below ``c_0``
    clamps to the smallest.  Linearly interpolated over the sorted sample.

    Callers use this only when weights are non-uniform (uniform weights keep
    the ``np.quantile`` path, so canonical numbers are unchanged).
    Deterministic.  The normalization is preserved verbatim from the phase-3
    implementation this was extracted from: correcting it to divide by the
    total weight would move ``_fit_isotonic_map``'s bin edges and change
    published calibration numbers.
    """
    x = np.asarray(x, dtype=np.float64)
    row_w = np.asarray(row_w, dtype=np.float64)
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    ws = row_w[order]
    c = np.cumsum(ws) - 0.5 * ws
    c = c / c[-1]
    return np.interp(q, c, xs)
