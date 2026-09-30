#!/usr/bin/env python3
"""Nested group-held-out direct-regret policies for Phase 7.

This deliberately small offline experiment compares a weighted direct ridge
model with a two-layer MLP. Inputs are only entry-state/candidate features.
The target is signed-log node gain relative to natural ordinal 0; a
fail-high-classification change receives a configurable negative utility.

For every outer held-out game/root group, the firing threshold is selected on
three-fold inner out-of-group predictions using exact measured cost plus the
same classification penalty. The final model is then fit on all outer-training
groups and evaluated once on the untouched group. No measured test cost or
outcome enters prediction or threshold selection.
"""
import argparse
import glob
import gzip
import json
import math
import os
import statistics

import numpy as np

from p6_explanatory import row_costs
from p6_learnability import (NUM_FEATURES, feature_matrix, row_examples,
                             transform_feature_dicts)

try:
    import torch
    import torch.nn as nn
except ImportError:
    torch = None


def discover(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(sorted(glob.glob(os.path.join(path, "*.jsonl.gz"))))
        else:
            files.append(path)
    return sorted(set(files))


def load_rows(paths, manifest_path, min_baseline_nodes):
    manifest = json.load(open(manifest_path))
    groups = {root["id"]: root["game_group"] for root in manifest["roots"]}
    by_group = {}
    for path in discover(paths):
        root_id = os.path.basename(path).split(".jsonl", 1)[0]
        group = groups[root_id]
        with gzip.open(path, "rt") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        rows = [row for row in rows if row.get("type") == "decision"
                and row["baseline"]["nodes"] >= min_baseline_nodes]
        by_group.setdefault(group, []).extend(rows)
    return by_group


def fitted_scale(rows):
    examples = [e for row in rows for e in row_examples(row)]
    _, scale = feature_matrix(examples)
    return scale


def row_inputs(row, scale):
    exs = list(row_examples(row))
    feature_by_ordinal = {e[1]: e[0] for e in exs}
    d = len(NUM_FEATURES)
    transformed = np.zeros((4, d), dtype=np.float64)
    present = np.zeros(4, dtype=np.float64)
    for ordinal, feats in feature_by_ordinal.items():
        transformed[ordinal] = transform_feature_dicts([feats], scale)[0, :-1]
        present[ordinal] = 1.0
    common = np.concatenate([transformed.ravel(), present])
    output = {}
    for ordinal in sorted(feature_by_ordinal):
        if ordinal == 0:
            continue
        action = np.zeros(4, dtype=np.float64)
        action[ordinal] = 1.0
        output[ordinal] = np.concatenate(
            [common, transformed[ordinal] - transformed[0], action])
    return output


def signed_log(value):
    return math.copysign(math.log1p(abs(value)), value)


def training_matrix(rows, scale, class_penalty):
    X, y, weights = [], [], []
    for row in rows:
        inputs = row_inputs(row, scale)
        costs, fail_high, _, _ = row_costs(row)
        baseline = costs[0]
        for ordinal, x in inputs.items():
            gain = baseline - costs[ordinal]
            if fail_high[ordinal] != fail_high[0]:
                gain = -class_penalty * max(baseline, costs[ordinal], 1)
            X.append(x)
            y.append(signed_log(gain))
            weights.append(min(16.0, math.sqrt(max(baseline, 1))))
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    weights /= weights.mean()
    return X, y, weights


class WeightedRidge:
    def __init__(self, lam=1.0):
        self.lam = lam

    def fit(self, X, y, weights, seed=0):
        del seed
        Xa = np.hstack([X, np.ones((len(X), 1))])
        sw = np.sqrt(weights)[:, None]
        A = (Xa * sw).T @ (Xa * sw)
        reg = np.eye(A.shape[0]) * self.lam
        reg[-1, -1] = 0.0
        self.coef = np.linalg.solve(A + reg, (Xa * sw).T @ (y * sw[:, 0]))
        return self

    def predict(self, X):
        return np.hstack([X, np.ones((len(X), 1))]) @ self.coef


class TinyMLP:
    def __init__(self, epochs=40, hidden=32):
        if torch is None:
            raise RuntimeError("PyTorch is required for the MLP experiment")
        self.epochs = epochs
        self.hidden = hidden

    def fit(self, X, y, weights, seed=0):
        torch.manual_seed(seed)
        torch.set_num_threads(1)
        self.y_mean = float(np.average(y, weights=weights))
        self.y_sd = float(np.sqrt(np.average((y - self.y_mean) ** 2,
                                              weights=weights))) or 1.0
        xt = torch.tensor(X, dtype=torch.float32)
        yt = torch.tensor((y - self.y_mean) / self.y_sd, dtype=torch.float32)
        wt = torch.tensor(weights, dtype=torch.float32)
        self.net = nn.Sequential(nn.Linear(X.shape[1], self.hidden), nn.ReLU(),
                                 nn.Linear(self.hidden, self.hidden), nn.ReLU(),
                                 nn.Linear(self.hidden, 1))
        optimizer = torch.optim.AdamW(self.net.parameters(), lr=0.004,
                                      weight_decay=1e-4)
        self.net.train()
        for _ in range(self.epochs):
            optimizer.zero_grad()
            pred = self.net(xt).squeeze(1)
            loss = (torch.nn.functional.smooth_l1_loss(pred, yt,
                                                        reduction="none") * wt).mean()
            loss.backward()
            optimizer.step()
        return self

    def predict(self, X):
        self.net.eval()
        with torch.no_grad():
            z = self.net(torch.tensor(X, dtype=torch.float32)).squeeze(1).numpy()
        return z * self.y_sd + self.y_mean


def fit_predict(model_name, train_rows, test_rows, seeds, class_penalty):
    scale = fitted_scale(train_rows)
    Xtr, ytr, wtr = training_matrix(train_rows, scale, class_penalty)
    test_parts = []
    row_slices = []
    for row in test_rows:
        inputs = row_inputs(row, scale)
        ordinals = sorted(inputs)
        start = len(test_parts)
        test_parts.extend(inputs[o] for o in ordinals)
        row_slices.append((start, len(test_parts), ordinals))
    Xte = np.asarray(test_parts, dtype=np.float64)
    predictions = np.zeros(len(Xte), dtype=np.float64)
    for seed in seeds:
        model = WeightedRidge() if model_name == "ridge" else TinyMLP()
        model.fit(Xtr, ytr, wtr, seed)
        predictions += model.predict(Xte) / len(seeds)
    by_row = []
    for start, end, ordinals in row_slices:
        by_row.append({o: float(score) for o, score in
                       zip(ordinals, predictions[start:end])})
    return by_row


def choose(scores, threshold):
    if not scores:
        return 0
    ordinal, score = max(scores.items(), key=lambda item: (item[1], -item[0]))
    return ordinal if score > max(0.0, threshold) else 0


def evaluate(rows, predictions, threshold, class_penalty):
    out = {key: 0 for key in ("B", "C", "objective", "class_fallback",
                              "exact_fallback", "later", "fhfl", "flfh")}
    for row, scores in zip(rows, predictions):
        ordinal = choose(scores, threshold)
        costs, fail_high, probes, baseline = row_costs(row)
        values = {0: baseline["value"]}
        values.update({o: p["value"] for o, p in probes.items()})
        B, C = costs[0], costs[ordinal]
        flip = fail_high[ordinal] != fail_high[0]
        out["B"] += B
        out["C"] += C
        out["objective"] += C + (class_penalty * B if flip else 0)
        out["class_fallback"] += B if flip else C
        out["exact_fallback"] += B if values[ordinal] != values[0] else C
        out["later"] += ordinal > 0
        out["fhfl"] += flip and fail_high[0]
        out["flfh"] += flip and not fail_high[0]
    return out


def tune_threshold(rows, predictions, class_penalty):
    maxima = np.array([max([0.0] + list(scores.values())) for scores in predictions])
    positive = maxima[maxima > 0]
    grid = [float("inf"), 0.0]
    if len(positive):
        grid += list(np.unique(np.quantile(positive, np.linspace(0, 1, 41))))
    best = None
    for threshold in grid:
        result = evaluate(rows, predictions, threshold, class_penalty)
        key = (result["objective"], result["later"], -threshold)
        if best is None or key < best[0]:
            best = (key, threshold)
    return float(best[1])


def inner_predictions(model, outer_groups, by_group, seeds, class_penalty):
    groups = sorted(outer_groups)
    folds = [groups[i::3] for i in range(3)]
    rows, predictions = [], []
    for validation in folds:
        fit_groups = [g for g in groups if g not in validation]
        fit_rows = [r for g in fit_groups for r in by_group[g]]
        validation_rows = [r for g in validation for r in by_group[g]]
        pred = fit_predict(model, fit_rows, validation_rows, seeds, class_penalty)
        rows.extend(validation_rows)
        predictions.extend(pred)
    return rows, predictions


def savings(result, key="C"):
    return 100.0 * (1.0 - result[key] / result["B"]) if result["B"] else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--group-manifest", required=True)
    parser.add_argument("--json", required=True)
    parser.add_argument("--min-baseline-nodes", type=int, default=0)
    parser.add_argument("--classification-penalty", type=float, default=1.0)
    parser.add_argument("--models", default="ridge,mlp")
    parser.add_argument("--seeds", default="7,19")
    args = parser.parse_args()
    by_group = load_rows(args.paths, args.group_manifest, args.min_baseline_nodes)
    groups = sorted(by_group)
    seeds = [int(x) for x in args.seeds.split(",")]
    models = args.models.split(",")
    report = {"groups": {}, "pooled": {}, "config": vars(args)}
    for held_out in groups:
        train_groups = [g for g in groups if g != held_out]
        train_rows = [r for g in train_groups for r in by_group[g]]
        test_rows = by_group[held_out]
        report["groups"][held_out] = {}
        print(f"== hold out {held_out} ({len(test_rows)} rows)", flush=True)
        for model in models:
            inner_rows, inner_pred = inner_predictions(
                model, train_groups, by_group, seeds, args.classification_penalty)
            threshold = tune_threshold(inner_rows, inner_pred,
                                       args.classification_penalty)
            test_pred = fit_predict(model, train_rows, test_rows, seeds,
                                    args.classification_penalty)
            result = evaluate(test_rows, test_pred, threshold,
                              args.classification_penalty)
            result["threshold"] = threshold if math.isfinite(threshold) else None
            report["groups"][held_out][model] = result
            print(f"  {model:6s} threshold={threshold:7.3f} "
                  f"save={savings(result):7.3f}% "
                  f"classfb={savings(result, 'class_fallback'):7.3f}% "
                  f"exactfb={savings(result, 'exact_fallback'):7.3f}% "
                  f"later={result['later']:4d} flips={result['fhfl']+result['flfh']:3d}",
                  flush=True)
    for model in models:
        pooled = {key: 0 for key in ("B", "C", "objective", "class_fallback",
                                     "exact_fallback", "later", "fhfl", "flfh")}
        root_savings = []
        for group in groups:
            result = report["groups"][group][model]
            for key in pooled:
                pooled[key] += result[key]
            root_savings.append(savings(result))
        pooled["saving_pct"] = savings(pooled)
        pooled["class_fallback_saving_pct"] = savings(pooled, "class_fallback")
        pooled["exact_fallback_saving_pct"] = savings(pooled, "exact_fallback")
        pooled["positive_groups"] = sum(value > 0 for value in root_savings)
        pooled["root_mean_pct"] = statistics.mean(root_savings)
        pooled["root_sd_pct"] = statistics.stdev(root_savings)
        report["pooled"][model] = pooled
        print(f"POOLED {model:6s} {pooled['B']}->{pooled['C']} "
              f"save={pooled['saving_pct']:.3f}% "
              f"classfb={pooled['class_fallback_saving_pct']:.3f}% "
              f"exactfb={pooled['exact_fallback_saving_pct']:.3f}% "
              f"positive={pooled['positive_groups']}/{len(groups)} "
              f"root mean/sd={pooled['root_mean_pct']:.3f}/{pooled['root_sd_pct']:.3f}")
    with open(args.json, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, allow_nan=False)
        fh.write("\n")


if __name__ == "__main__":
    main()
