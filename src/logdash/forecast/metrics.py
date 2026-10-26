"""Forecast metrics on the test samples."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score


def score_metrics(labels: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    """Threshold-free metrics of one model on the test samples (JSON-safe; None when undefined).

    Keys: samples, positives, positive_rate, auroc, pr_auc. AUROC needs both classes, PR-AUC
    (average precision) needs at least one positive.
    """
    labels = np.asarray(labels, dtype=bool)
    samples = int(len(labels))
    positives = int(labels.sum())
    return {
        "samples": samples,
        "positives": positives,
        "positive_rate": positives / samples if samples else None,
        "auroc": float(roc_auc_score(labels, scores)) if 0 < positives < samples else None,
        "pr_auc": float(average_precision_score(labels, scores)) if positives else None,
    }


def threshold_metrics(
    labels: np.ndarray, scores: np.ndarray, threshold: float
) -> dict[str, float | None]:
    """accuracy and f1 when ``scores >= threshold`` predicts a spike.

    accuracy is None without samples; f1 is None without positives (it is undefined there).
    """
    labels = np.asarray(labels, dtype=bool)
    samples = int(len(labels))
    positives = int(labels.sum())
    pred = np.asarray(scores) >= threshold
    tp = int((pred & labels).sum())
    fp = int((pred & ~labels).sum())
    fn = positives - tp
    return {
        "accuracy": float((pred == labels).mean()) if samples else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if positives else None,
    }
