"""Time-series validation utilities — purged walk-forward with an embargo.

Rules enforced (Phase 7/8 of the mandate):
  * never shuffle — folds are strictly chronological;
  * a test fold is separated from training by an embargo gap;
  * training rows whose label window [t, t+horizon] overlaps the test window are
    PURGED, so overlapping 15-minute labels cannot leak future price path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass
class Fold:
    train_idx: list[int]
    val_idx: list[int]
    test_idx: list[int]


def _check_sorted(timestamps: Sequence[float]) -> None:
    for i in range(1, len(timestamps)):
        if timestamps[i] < timestamps[i - 1]:
            raise ValueError("timestamps must be sorted ascending (no shuffling)")


def purged_walk_forward(
    timestamps: Sequence[float],
    n_splits: int = 4,
    embargo_seconds: float = 900.0,
    horizon_seconds: float = 900.0,
    val_fraction: float = 0.5,
) -> list[Fold]:
    """Expanding-window walk-forward.

    The series is divided into ``n_splits`` contiguous test blocks. For each, the
    training pool is everything chronologically before the block; a slice of the
    most recent training rows becomes the validation set (``val_fraction`` of the
    block size). Training rows within ``embargo`` of the test start, or whose
    label window overlaps any test row's window, are removed.
    """
    ts = [float(t) for t in timestamps]
    _check_sorted(ts)
    n = len(ts)
    if n < n_splits + 1 or n_splits < 1:
        return []

    block = n // (n_splits + 1)  # first block reserved for the initial train pool
    if block < 1:
        return []

    folds: list[Fold] = []
    for s in range(1, n_splits + 1):
        test_start = s * block
        test_end = (s + 1) * block if s < n_splits else n
        test_idx = list(range(test_start, test_end))
        if not test_idx:
            continue
        test_lo_ts = ts[test_idx[0]]
        test_windows = [(ts[i], ts[i] + horizon_seconds) for i in test_idx]

        train_pool = list(range(0, test_start))
        # Embargo: drop train rows too close to the test start.
        train_pool = [i for i in train_pool if ts[i] <= test_lo_ts - embargo_seconds]
        # Purge: drop train rows whose label window overlaps any test window.
        purged = []
        for i in train_pool:
            w0, w1 = ts[i], ts[i] + horizon_seconds
            overlaps = any(not (w1 <= a or w0 >= b) for (a, b) in test_windows)
            if not overlaps:
                purged.append(i)
        if not purged:
            continue
        # Validation = most recent slice of the purged training pool.
        n_val = max(1, int(len(purged) * val_fraction)) if len(purged) >= 4 else 0
        val_idx = purged[len(purged) - n_val:] if n_val else []
        train_idx = purged[: len(purged) - n_val] if n_val else purged
        if not train_idx:
            continue
        folds.append(Fold(train_idx=train_idx, val_idx=val_idx, test_idx=test_idx))
    return folds
