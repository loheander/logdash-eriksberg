"""Baseline models: no learning."""

from __future__ import annotations

import numpy as np

from logdash.forecast.data import Samples

PERSISTENCE = "persistence"  # model key in run files and the results table


def persistence_scores(spike: np.ndarray, horizon: int, samples: Samples) -> np.ndarray:
    """Repeat the last value: 1.0 when the host had a spike minute in (t - H, t], else 0.0.

    That is y(t - H), the last fully observed target. The window is clipped at the span start.
    """
    zero = np.zeros((spike.shape[0], 1), np.int64)
    cum = np.concatenate([zero, np.cumsum(spike, axis=1, dtype=np.int64)], axis=1)
    hi = samples.minute + 1
    lo = np.maximum(samples.minute + 1 - horizon, 0)
    return ((cum[samples.host, hi] - cum[samples.host, lo]) > 0).astype(np.float32)
