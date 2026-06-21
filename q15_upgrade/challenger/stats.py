"""Paired control-vs-challenger statistics (addendum Section I).

Because the control and challenger score the SAME contracts, comparisons are
paired. Provides paired log-loss / Brier differences, block-bootstrap confidence
intervals (which respect serial dependence from overlapping labels), McNemar's
test for hard classifications, and an effective-sample-size estimate.

Sign convention: a NEGATIVE mean difference (challenger - control) means the
challenger is BETTER (lower loss).
"""

from __future__ import annotations

import math
import random
from typing import Sequence

from .mathx import clamp


def _ll(p: float, y: int) -> float:
    p = clamp(p, 1e-6, 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def paired_differences(p_control: Sequence[float], p_challenger: Sequence[float],
                       y: Sequence[int]) -> dict:
    """Per-sample paired (challenger - control) log-loss and Brier differences."""
    if not (len(p_control) == len(p_challenger) == len(y)):
        raise ValueError("inputs must be equal length")
    ll_diff = [_ll(pk, yi) - _ll(pc, yi) for pc, pk, yi in zip(p_control, p_challenger, y)]
    brier_diff = [((pk - yi) ** 2 - (pc - yi) ** 2)
                  for pc, pk, yi in zip(p_control, p_challenger, y)]
    return {
        "log_loss_diff": ll_diff,
        "brier_diff": brier_diff,
        "mean_log_loss_diff": sum(ll_diff) / len(ll_diff) if ll_diff else 0.0,
        "mean_brier_diff": sum(brier_diff) / len(brier_diff) if brier_diff else 0.0,
    }


def block_bootstrap_ci(values: Sequence[float], block_size: int = 5,
                       n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Moving-block bootstrap CI for the MEAN of a serially-dependent series."""
    vals = list(values)
    n = len(vals)
    if n == 0:
        return (0.0, 0.0)
    block_size = max(1, min(block_size, n))
    n_blocks = math.ceil(n / block_size)
    rng = random.Random(seed)
    starts_max = n - block_size
    means = []
    for _ in range(n_boot):
        sample = []
        for _ in range(n_blocks):
            s = rng.randint(0, max(0, starts_max))
            sample.extend(vals[s:s + block_size])
        sample = sample[:n]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi)


def mcnemar(correct_control: Sequence[bool], correct_challenger: Sequence[bool]) -> dict:
    """McNemar test on discordant hard-classification pairs."""
    b = sum(1 for c, k in zip(correct_control, correct_challenger) if c and not k)
    c = sum(1 for cc, k in zip(correct_control, correct_challenger) if (not cc) and k)
    # continuity-corrected chi-square (1 dof)
    stat = ((abs(b - c) - 1) ** 2) / (b + c) if (b + c) > 0 else 0.0
    return {"b_control_only": b, "c_challenger_only": c, "statistic": stat,
            "challenger_better_discordant": c > b}


def effective_sample_size(group_ids: Sequence) -> int:
    """Independent-observation count = number of distinct overlap groups.

    Overlapping 15-min labels sharing a group (e.g. same close window) count once,
    so thousands of overlapping predictions are NOT thousands of independent obs.
    """
    return len(set(group_ids))
