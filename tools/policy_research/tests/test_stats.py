#!/usr/bin/env python3
"""Unit tests for the shared statistical primitives (no engine required).

These defend the *interface* of ``stats.py``: the penalty-placement contract,
the strict label contract, tie handling, and the invariants each consumer
depends on. They assert observable behaviour, so they survive a change of
internal formulation.

Run with:  python3 -m unittest discover -s tools/policy_research/tests -v
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stats  # noqa: E402


def _auc_oracle(labels, scores):
    """Independent pairwise-concordance AUC: the definition, not the code."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else (0.5 if a == b else 0.0)
    return wins / (len(pos) * len(neg))


class TestAuc(unittest.TestCase):
    def test_perfect_separation_and_inversion(self):
        self.assertEqual(stats.auc([0, 1], [0.1, 0.9]), 1.0)
        self.assertEqual(stats.auc([0, 1], [0.9, 0.1]), 0.0)

    def test_all_tied_scores_is_one_half(self):
        self.assertEqual(stats.auc([0, 1, 0, 1], [0.4, 0.4, 0.4, 0.4]), 0.5)

    def test_matches_pairwise_oracle_under_heavy_ties(self):
        rng = np.random.RandomState(7)
        # Small integer scores guarantee many exact ties.
        scores = rng.randint(0, 3, size=60).astype(np.float64)
        labels = rng.randint(0, 2, size=60)
        self.assertAlmostEqual(stats.auc(labels, scores),
                               _auc_oracle(labels, scores), places=12)

    def test_single_class_is_nan(self):
        self.assertTrue(np.isnan(stats.auc([0, 0, 0], [0.1, 0.2, 0.3])))
        self.assertTrue(np.isnan(stats.auc([1, 1], [0.1, 0.2])))
        self.assertTrue(np.isnan(stats.auc([], [])))

    def test_accepts_booleans(self):
        self.assertEqual(stats.auc([False, True], [0.1, 0.9]), 1.0)

    def test_rejects_labels_outside_zero_one(self):
        # A mis-encoded label column must fail loudly, not be scored as a
        # positive class.
        with self.assertRaises(ValueError):
            stats.auc([0, 2], [0.1, 0.9])
        with self.assertRaises(ValueError):
            stats.auc([-1, 1], [0.1, 0.9])


class TestPavIsotonic(unittest.TestCase):
    def test_already_monotone_is_unchanged(self):
        y = np.array([0.0, 0.5, 1.0])
        np.testing.assert_allclose(stats.pav_isotonic(y, np.ones(3)), y)

    def test_pools_violating_blocks(self):
        # 1 then 0 violates monotonicity, so those two pool to their mean.
        np.testing.assert_allclose(
            stats.pav_isotonic(np.array([1.0, 0.0, 2.0]), np.ones(3)),
            np.array([0.5, 0.5, 2.0]))

    def test_pooling_is_weighted(self):
        # (1*1 + 0*3) / 4 = 0.25
        np.testing.assert_allclose(
            stats.pav_isotonic(np.array([1.0, 0.0]), np.array([1.0, 3.0])),
            np.array([0.25, 0.25]))

    def test_output_is_nondecreasing(self):
        rng = np.random.RandomState(3)
        y = rng.rand(50)
        w = rng.rand(50) + 0.1
        fit = stats.pav_isotonic(y, w)
        self.assertTrue(np.all(np.diff(fit) >= 0))

    def test_empty_input(self):
        self.assertEqual(len(stats.pav_isotonic(np.array([]), np.array([]))), 0)


class TestAverageRanks(unittest.TestCase):
    def test_zero_based_with_midranks_on_ties(self):
        np.testing.assert_allclose(stats.average_ranks([5.0, 5.0, 7.0]),
                                   np.array([0.5, 0.5, 2.0]))

    def test_ranks_are_a_permutation_of_zero_to_n_minus_one(self):
        rng = np.random.RandomState(11)
        x = rng.randint(0, 4, size=40).astype(np.float64)
        r = stats.average_ranks(x)
        self.assertAlmostEqual(float(r.sum()), len(x) * (len(x) - 1) / 2.0)
        # order is preserved for strictly increasing values
        a, b = stats.average_ranks([1.0, 2.0, 3.0]), [0.0, 1.0, 2.0]
        np.testing.assert_allclose(a, b)


class TestRidgePenaltyContract(unittest.TestCase):
    """The penalty placement is the whole point of the split; if a future
    edit swaps the two conventions, these fail."""

    def setUp(self):
        rng = np.random.RandomState(5)
        self.X = rng.randn(40, 3)
        self.y = 5.0 + 0.0 * self.X[:, 0]  # signal lives entirely in intercept

    def test_penalized_intercept_is_shrunk_by_lambda(self):
        coef, intercept = stats.ridge_fit(self.X, self.y, lam=1e6,
                                          penalize_intercept=True)
        self.assertLess(abs(intercept), 1e-3)

    def test_unpenalized_intercept_stays_at_the_weighted_mean(self):
        weights = np.ones(len(self.y))
        coef, intercept = stats.ridge_fit(self.X, self.y, lam=1e6,
                                          penalize_intercept=False,
                                          weights=weights)
        self.assertAlmostEqual(intercept, 5.0, places=6)
        np.testing.assert_allclose(coef, np.zeros(3), atol=1e-6)

    def test_shapes(self):
        coef, intercept = stats.ridge_fit(self.X, self.y, lam=1e-3,
                                          penalize_intercept=True)
        self.assertEqual(len(coef), self.X.shape[1])
        self.assertIsInstance(float(intercept), float)

    def test_uniform_weights_match_the_normal_equations(self):
        weights = np.ones(len(self.y))
        coef, intercept = stats.ridge_fit(self.X, self.y, lam=0.0,
                                          penalize_intercept=False,
                                          weights=weights)
        design = np.hstack([self.X, np.ones((len(self.X), 1))])
        expected = np.linalg.lstsq(design, self.y, rcond=None)[0]
        np.testing.assert_allclose(np.append(coef, intercept), expected,
                                   atol=1e-9)

    def test_unsupported_combinations_raise(self):
        with self.assertRaises(ValueError):
            stats.ridge_fit(self.X, self.y, lam=1.0, penalize_intercept=True,
                            weights=np.ones(len(self.y)))
        with self.assertRaises(ValueError):
            stats.ridge_fit(self.X, self.y, lam=1.0,
                            penalize_intercept=False)


class TestIrlsLogistic(unittest.TestCase):
    def setUp(self):
        rng = np.random.RandomState(13)
        self.X = rng.randn(60, 2)
        self.y = (self.X[:, 0] > 0).astype(np.float64)

    def _fit(self, **kw):
        opts = dict(lam=1e-3, tol=1e-9, max_iter=80)
        opts.update(kw)
        return stats.irls_logistic(self.X, self.y, **opts)

    def test_recovers_the_sign_of_a_learnable_signal(self):
        coef, intercept, iters, converged = self._fit(penalize_intercept=False)
        self.assertGreater(coef[0], 0.0)
        self.assertTrue(converged)
        self.assertGreaterEqual(iters, 1)

    def test_uniform_weights_reproduce_the_unweighted_fit(self):
        a = self._fit(penalize_intercept=False)
        X, y = self.X, self.y
        b = stats.irls_logistic(X, y, lam=1e-3, tol=1e-9, max_iter=80,
                                penalize_intercept=False,
                                weights=np.ones(len(y)))
        np.testing.assert_allclose(a[0], b[0], atol=1e-12)
        self.assertAlmostEqual(a[1], b[1], places=12)

    def test_zero_weight_rows_are_ignored_not_fatal(self):
        w = np.ones(len(self.y))
        w[:10] = 0.0
        coef, intercept, _, _ = self._fit(penalize_intercept=False, weights=w)
        self.assertTrue(np.all(np.isfinite(coef)))
        self.assertTrue(np.isfinite(intercept))

    def test_convergence_flag_reports_an_iteration_cap(self):
        _, _, iters, converged = self._fit(penalize_intercept=False,
                                           max_iter=1)
        self.assertEqual(iters, 1)
        self.assertFalse(converged)

    def test_penalty_placement_moves_the_intercept(self):
        """Strong penalty: the penalized-intercept convention pulls the
        intercept toward zero, the unpenalized one does not."""
        penalized = stats.irls_logistic(self.X, self.y, lam=1e4,
                                        penalize_intercept=True,
                                        tol=1e-8, max_iter=40)
        free = stats.irls_logistic(self.X, self.y, lam=1e4,
                                   penalize_intercept=False,
                                   tol=1e-9, max_iter=80)
        self.assertLess(abs(penalized[1]), abs(free[1]))
        self.assertGreater(abs(free[1]), 0.05)

    def test_weighted_working_response_form_raises(self):
        with self.assertRaises(ValueError):
            stats.irls_logistic(self.X, self.y, lam=1.0,
                                penalize_intercept=True, tol=1e-8,
                                max_iter=40, weights=np.ones(len(self.y)))


class TestMetrics(unittest.TestCase):
    def test_brier_endpoints(self):
        self.assertEqual(stats.brier([0, 1], [0.0, 1.0]), 0.0)
        self.assertEqual(stats.brier([0, 1], [1.0, 0.0]), 1.0)

    def test_log_loss_is_near_zero_for_perfect_predictions(self):
        self.assertLess(stats.log_loss([0, 1, 1, 0], [0.0, 1.0, 1.0, 0.0]),
                        1e-5)

    def test_log_loss_penalises_confident_errors(self):
        self.assertGreater(stats.log_loss([0, 1], [1.0, 0.0]), 10.0)

    def test_ece_zero_when_confidence_matches_accuracy(self):
        # All rows in one bin, half positive: accuracy == confidence.
        self.assertAlmostEqual(stats.ece([1, 0], [0.5, 0.5], bins=10), 0.0)

    def test_ece_reports_the_bin_gap(self):
        self.assertAlmostEqual(stats.ece([1, 1], [0.9, 0.9], bins=10), 0.1,
                               places=12)

    def test_bins_is_required(self):
        with self.assertRaises(TypeError):
            stats.ece([1, 0], [0.5, 0.5])


class TestWeightedDescriptives(unittest.TestCase):
    def test_uniform_weights_match_the_unweighted_statistics(self):
        rng = np.random.RandomState(17)
        X = rng.randn(30, 4)
        mu, sd = stats.weighted_std_stats(X, np.ones(30))
        np.testing.assert_allclose(mu, X.mean(axis=0), atol=1e-12)
        np.testing.assert_allclose(sd, X.std(axis=0), atol=1e-12)

    def test_constant_feature_gets_unit_sd(self):
        X = np.ones((10, 2))
        _, sd = stats.weighted_std_stats(X, np.ones(10))
        np.testing.assert_allclose(sd, np.array([1.0, 1.0]))

    def test_extreme_quantiles_hit_the_sample_range(self):
        # The abscissa is normalized by the last midpoint, so it ends exactly
        # at 1.0 and np.interp clamps below the first midpoint: q=1 and q=0
        # return the extremes.
        rng = np.random.RandomState(19)
        x = rng.randn(41)
        w = np.ones(41)
        self.assertAlmostEqual(
            float(stats.weighted_quantile(x, w, 1.0)), float(x.max()),
            places=12)
        self.assertAlmostEqual(
            float(stats.weighted_quantile(x, w, 0.0)), float(x.min()),
            places=12)

    def test_quantiles_are_monotone_and_bounded(self):
        rng = np.random.RandomState(23)
        x = rng.randn(30)
        w = rng.rand(30) + 0.1
        qs = [float(stats.weighted_quantile(x, w, q))
              for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        self.assertEqual(qs, sorted(qs))
        self.assertGreaterEqual(qs[0], x.min())
        self.assertLessEqual(qs[-1], x.max())

    def test_weight_pulls_the_quantile_toward_the_weighted_mass(self):
        x = np.array([0.0, 10.0])
        low = float(stats.weighted_quantile(x, np.array([9.0, 1.0]), 0.5))
        high = float(stats.weighted_quantile(x, np.array([1.0, 9.0]), 0.5))
        self.assertLess(low, high)


if __name__ == "__main__":
    unittest.main()
