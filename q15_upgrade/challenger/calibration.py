"""Probability calibration — fit on a VALIDATION fold only, never on test.

Three options: identity (no-op), Platt (sigmoid), isotonic (PAVA). The choice is
config-driven; the harness selects it using validation data and then freezes it
before scoring the unseen test fold.
"""

from __future__ import annotations

from typing import Sequence

from .mathx import clamp, logit, sigmoid


class IdentityCalibrator:
    name = "identity"

    def fit(self, probs: Sequence[float], labels: Sequence[int]) -> "IdentityCalibrator":
        return self

    def transform(self, p: float) -> float:
        return clamp(p, 0.01, 0.99)


class PlattCalibrator:
    """Sigmoid calibration: p' = sigmoid(a * logit(p) + b), L2-regularised."""

    name = "platt"

    def __init__(self, l2: float = 1.0, lr: float = 0.1, max_iter: int = 500):
        self.a = 1.0
        self.b = 0.0
        self.l2 = l2
        self.lr = lr
        self.max_iter = max_iter
        self.fitted = False

    def fit(self, probs: Sequence[float], labels: Sequence[int]) -> "PlattCalibrator":
        xs = [logit(clamp(float(p), 1e-6, 1 - 1e-6)) for p in probs]
        ys = [float(y) for y in labels]
        n = len(xs)
        if n < 5:
            self.a, self.b, self.fitted = 1.0, 0.0, False
            return self
        a, b = 1.0, 0.0
        for _ in range(self.max_iter):
            ga = gb = 0.0
            for x, y in zip(xs, ys):
                pred = sigmoid(a * x + b)
                err = pred - y
                ga += err * x
                gb += err
            # L2 pulls a->1, b->0 (identity prior).
            ga = ga / n + self.l2 / n * (a - 1.0)
            gb = gb / n + self.l2 / n * b
            a -= self.lr * ga
            b -= self.lr * gb
        self.a, self.b, self.fitted = a, b, True
        return self

    def transform(self, p: float) -> float:
        return clamp(sigmoid(self.a * logit(clamp(float(p), 1e-6, 1 - 1e-6)) + self.b), 0.01, 0.99)


class IsotonicCalibrator:
    """Monotone calibration via Pool-Adjacent-Violators, with interpolation."""

    name = "isotonic"

    def __init__(self):
        self.xs: list[float] = []
        self.ys: list[float] = []
        self.fitted = False

    def fit(self, probs: Sequence[float], labels: Sequence[int]) -> "IsotonicCalibrator":
        pairs = sorted(zip((float(p) for p in probs), (float(y) for y in labels)), key=lambda t: t[0])
        if len(pairs) < 5:
            self.fitted = False
            return self
        # PAVA on the labels ordered by predicted prob.
        xs = [p for p, _ in pairs]
        blocks = [[y, 1.0] for _, y in pairs]  # [weighted_sum, weight]
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
                blocks[i][0] += blocks[i + 1][0]
                blocks[i][1] += blocks[i + 1][1]
                del blocks[i + 1]
                if i > 0:
                    i -= 1
            else:
                i += 1
        # Expand block means back to per-point fitted values.
        fitted_y: list[float] = []
        bi = 0
        count = 0
        for _ in pairs:
            mean = blocks[bi][0] / blocks[bi][1]
            fitted_y.append(mean)
            count += 1
            if count >= blocks[bi][1]:
                bi += 1
                count = 0
        self.xs, self.ys, self.fitted = xs, fitted_y, True
        return self

    def transform(self, p: float) -> float:
        if not self.fitted or not self.xs:
            return clamp(float(p), 0.01, 0.99)
        p = float(p)
        if p <= self.xs[0]:
            return clamp(self.ys[0], 0.01, 0.99)
        if p >= self.xs[-1]:
            return clamp(self.ys[-1], 0.01, 0.99)
        lo, hi = 0, len(self.xs) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.xs[mid] <= p:
                lo = mid
            else:
                hi = mid
        x0, x1 = self.xs[lo], self.xs[hi]
        y0, y1 = self.ys[lo], self.ys[hi]
        frac = 0.0 if x1 == x0 else (p - x0) / (x1 - x0)
        return clamp(y0 + frac * (y1 - y0), 0.01, 0.99)


def make_calibrator(kind: str, l2: float = 1.0):
    kind = (kind or "identity").lower()
    if kind == "platt":
        return PlattCalibrator(l2=l2)
    if kind == "isotonic":
        return IsotonicCalibrator()
    return IdentityCalibrator()
