"""Background shadow signals + a continuous, significance-tested A/B that asks:
*would this signal have improved the probability?* — without ever touching the
frozen champion or placing an order.

Six experimental signals are computed every cycle from data the system ALREADY
collects (no new external feeds), recorded next to each prediction, and graded at
settlement:

  order_flow_persistence  (#5)  directional — is aggressive flow persistently
                                confirmed by price drift?
  book_resiliency         (#6)  directional — wick-rejection footprint of
                                liquidity defending a side (replenishment proxy).
  prediction_stability    (#14) confidence — amplify the model's lean when the
                                pick is stable (low flip risk), shrink it when not.
  entropy_noise           (#17) confidence — shrink toward 0.5 when recent returns
                                look like directionless noise (high sign entropy).
  regime_transition       (#18b) confidence — de-confidence when the regime is
                                near a boundary and likely to flip.
  coinflip_fade           (#20) calibration — fade the champion's lean back toward
                                0.5 ONLY inside the near-coin-flip band (zero
                                outside it). The live record shows calibrated
                                P(YES) in ~[0.50,0.60) realises YES only ~27% of
                                the time (over-confident) while P(YES) >= 0.60 went
                                17/17 (well calibrated). This signal is the settled
                                A/B evidence for the band de-confidence applied by
                                the champion guard _coinflip_confidence_shrink.

Every signal is expressed as a YES-oriented real in ``[-1, 1]`` so all five are
graded uniformly. The A/B fits a one-parameter logistic *adjustment* on top of
the champion probability and measures the Brier improvement **out of sample**
(time-ordered train/test split) with a paired t-test — so a signal only looks
good if it carries information the champion missed, on data it was not fit to.

Pure and deterministic: no clock, no I/O, no network. The ledger persists the
values; this module only computes and grades them.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

SIGNAL_NAMES: tuple[str, ...] = (
    "order_flow_persistence",
    "book_resiliency",
    "prediction_stability",
    "entropy_noise",
    "regime_transition",
    "coinflip_fade",
)

# Half-width (in P(YES) space, around 0.5) of the near-coin-flip band that
# coinflip_fade de-confidences. Matches the champion guard's default
# Q15_V95_COINFLIP_SHRINK_BAND so the A/B measures exactly that guard's effect.
_COINFLIP_FADE_BAND = 0.10

# Human labels for the compact report.
_SIGNAL_LABELS: dict[str, str] = {
    "order_flow_persistence": "flow-persist",
    "book_resiliency": "book-resil",
    "prediction_stability": "stability",
    "entropy_noise": "low-noise",
    "regime_transition": "regime-stab",
    "coinflip_fade": "coin-fade",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(os.environ.get(name, default)))))
    except (TypeError, ValueError):
        return default


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    p = _clamp(p, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _binary_entropy(p: float) -> float:
    """Shannon entropy (bits) of a Bernoulli(p); 0 at p∈{0,1}, 1 at p=0.5."""
    p = _clamp(p, 0.0, 1.0)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))


# --------------------------------------------------------------------------- #
# Signal computation (per cycle, from already-collected data)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SignalConfig:
    enabled: bool
    recent_candles: int
    min_rows: int
    train_fraction: float
    brier_floor: float
    alpha: float

    @classmethod
    def from_env(cls) -> "SignalConfig":
        return cls(
            # Collection is ON by default: the signals are written to
            # predictions.shadow_signal_json and read back ONLY by the
            # out-of-sample A/B (resolved_shadow_signal_rows -> evaluate). They
            # never touch the live champion probability, so collecting them is
            # pure observation — and without collection the A/B can never
            # accumulate the evidence needed to judge them. Set to "0" to stop.
            enabled=_env_bool("Q15_V95_SHADOW_SIGNALS_ENABLED", True),
            recent_candles=_env_int("Q15_V95_SHADOW_SIGNALS_RECENT_CANDLES", 24, 4, 240),
            min_rows=_env_int("Q15_V95_SHADOW_SIGNALS_MIN_ROWS", 40, 10, 100000),
            train_fraction=_env_float("Q15_V95_SHADOW_SIGNALS_TRAIN_FRACTION", 0.70, 0.50, 0.90),
            brier_floor=_env_float("Q15_V95_SHADOW_SIGNALS_BRIER_FLOOR", 0.0005, 0.0, 0.05),
            alpha=_env_float("Q15_V95_SHADOW_SIGNALS_ALPHA", 0.05, 0.0001, 0.5),
        )


def _candle_closes_highs_lows(canonical: Any, n: int) -> tuple[list[float], list[float], list[float], list[float]]:
    candles = list(getattr(canonical, "candles", ()) or ())[-n:]
    closes, highs, lows, opens = [], [], [], []
    for row in candles:
        if not isinstance(row, Mapping):
            continue
        c = _num(row.get("close"))
        h = _num(row.get("high"), c)
        low = _num(row.get("low"), c)
        o = _num(row.get("open"), c)
        if c is None or c <= 0:
            continue
        closes.append(c)
        highs.append(h if h is not None else c)
        lows.append(low if low is not None else c)
        opens.append(o if o is not None else c)
    return closes, highs, lows, opens


def _log_returns(closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def compute_signals(analysis: Mapping[str, Any], canonical: Any, config: SignalConfig | None = None) -> dict[str, float]:
    """Compute the five YES-oriented experimental signals for one prediction.

    Returns a dict of ``signal_name -> value in [-1, 1]``. Missing inputs degrade
    a signal to 0.0 (no information), never an exception.
    """
    config = config or SignalConfig.from_env()
    orientation = 1.0 if getattr(canonical, "yes_is_higher", True) else -1.0
    model_yes = _num(analysis.get("yes_probability"), 0.5) or 0.5
    lean = _clamp(2.0 * model_yes - 1.0, -1.0, 1.0)  # YES-space directional lean of the champion

    closes, highs, lows, opens = _candle_closes_highs_lows(canonical, config.recent_candles)
    returns = _log_returns(closes)

    features = analysis.get("feature_values") if isinstance(analysis.get("feature_values"), Mapping) else {}
    regime = analysis.get("regime") if isinstance(analysis.get("regime"), Mapping) else {}
    volatility = analysis.get("volatility") if isinstance(analysis.get("volatility"), Mapping) else {}
    threshold_d = {}
    details = analysis.get("feature_details")
    if isinstance(details, Mapping) and isinstance(details.get("threshold_interaction"), Mapping):
        threshold_d = details["threshold_interaction"]

    # --- #5 order-flow persistence: flow direction confirmed by price drift ---
    flow_present = features.get("flow") is not None
    flow_yes = _clamp(orientation * (_num(features.get("flow"), 0.0) or 0.0), -1.0, 1.0)
    persistence = 0.0
    if returns and abs(flow_yes) > 1e-9:
        # In price space, a YES-bullish flow expects positive (orientation-adjusted)
        # returns. Fraction of recent returns confirming the flow direction.
        flow_price_sign = 1.0 if (orientation * flow_yes) >= 0 else -1.0
        agree = sum(1 for r in returns if (r >= 0) == (flow_price_sign >= 0)) / len(returns)
        persistence = flow_yes * (2.0 * agree - 1.0)
    elif flow_present and abs(flow_yes) > 1e-9:
        # Candle gap (no recent returns to confirm drift): instead of going dead
        # at 0.0, carry a damped flow lean so the signal still reflects the flow
        # we DO have. Halved because the price-drift confirmation is unavailable.
        persistence = 0.5 * flow_yes
    order_flow_persistence = _clamp(persistence, -1.0, 1.0)

    # --- #6 book resiliency: wick-rejection footprint of defended liquidity ----
    book_resiliency = 0.0
    if closes:
        rng_sum = 0.0
        net = 0.0
        for h, low, o, c in zip(highs, lows, opens, closes):
            rng = h - low
            if rng <= 0:
                continue
            upper_wick = h - max(o, c)   # sellers rejected (bearish defense)
            lower_wick = min(o, c) - low  # buyers rejected (bullish defense)
            net += (lower_wick - upper_wick)
            rng_sum += rng
        if rng_sum > 0:
            # Positive = buy-side liquidity more resilient (bullish); orient to YES.
            book_resiliency = _clamp(orientation * (net / rng_sum), -1.0, 1.0)

    # --- #14 prediction stability: amplify the lean when the pick is stable -----
    flip_raw = (analysis.get("flip_risk") or {}).get("score")
    if flip_raw is None:
        # Unknown flip risk must NOT be read as "perfectly stable" (which would
        # amplify the lean to full strength). Treat absence as neutral.
        stability = 0.5
    else:
        # flip_risk score is on a 0..100 scale (see flip_risk.py). Normalise to a
        # 0..1 flip fraction first — otherwise 1.0 - flip is negative for any real
        # score and clamps to 0, which zeroed this signal on the live record
        # (mean_abs_signal=0.0). stability = how UN-likely a flip is.
        flip = _clamp((_num(flip_raw, 0.0) or 0.0) / 100.0, 0.0, 1.0)
        stability = _clamp(1.0 - flip, 0.0, 1.0)
    prediction_stability = _clamp(lean * stability, -1.0, 1.0)

    # --- #17 entropy / noise: shrink the lean when returns look directionless ----
    if returns:
        p_up = sum(1 for r in returns if r > 0) / len(returns)
        noise = _binary_entropy(p_up)  # 1.0 = maximally noisy, 0.0 = one-directional
    else:
        noise = 1.0
    entropy_noise = _clamp(lean * (1.0 - noise), -1.0, 1.0)

    # --- #18b regime transition: de-confidence near a regime boundary ----------
    crossings_raw = threshold_d.get("crossings")
    sigma_raw = regime.get("one_minute_sigma")
    if sigma_raw is None:
        sigma_raw = volatility.get("sigma_per_sqrt_second")
    div_raw = ((analysis.get("feature_details") or {}).get("exchange_consensus", {}) or {}).get("divergence_bps")
    regime_inputs_present = any(v is not None for v in (crossings_raw, sigma_raw, div_raw))
    crossings = _num(crossings_raw, 0.0) or 0.0
    one_minute_sigma = _num(sigma_raw, 0.0) or 0.0
    divergence = _num(div_raw, 0.0) or 0.0
    crossings_norm = _clamp(crossings / 6.0, 0.0, 1.0)
    vol_norm = _clamp(one_minute_sigma / 0.0045, 0.0, 1.0)
    div_norm = _clamp(divergence / 35.0, 0.0, 1.0)
    if regime_inputs_present:
        transition_prob = _clamp(0.40 * crossings_norm + 0.30 * vol_norm + 0.30 * div_norm, 0.0, 1.0)
    else:
        # No regime inputs at all → unknown, not "no transition". A neutral
        # transition probability moderately de-confidences rather than leaving
        # the lean at full strength on missing data.
        transition_prob = 0.5
    regime_transition = _clamp(lean * (1.0 - transition_prob), -1.0, 1.0)

    # --- #20 coin-flip fade: de-confidence the lean in the near-coin-flip band ----
    # The settled record shows the champion is over-confident in the near-coin-flip
    # band (calibrated P(YES) ~[0.50,0.60) realises YES ~27% of the time), while it
    # is well calibrated OUTSIDE the band (P(YES) >= 0.60 went 17/17). The
    # YES-oriented correction therefore pulls the lean *back toward 0.5* ONLY inside
    # a band of half-width ``_COINFLIP_FADE_BAND`` around 0.5, and is exactly zero
    # for confident calls — so it mirrors the champion guard
    # ``_coinflip_confidence_shrink`` (same band) and the A/B measures precisely
    # that guard's de-confidence effect out of sample. The band weight tapers
    # linearly from the band edge to 0.5; the SIGN (negative for a YES lean,
    # positive for a NO lean) carries the de-confidence direction.
    band_dist = abs(model_yes - 0.5)
    band_weight = _clamp(1.0 - band_dist / _COINFLIP_FADE_BAND, 0.0, 1.0)
    coinflip_fade = _clamp(-lean * band_weight, -1.0, 1.0)

    return {
        "order_flow_persistence": round(order_flow_persistence, 6),
        "book_resiliency": round(book_resiliency, 6),
        "prediction_stability": round(prediction_stability, 6),
        "entropy_noise": round(entropy_noise, 6),
        "regime_transition": round(regime_transition, 6),
        "coinflip_fade": round(coinflip_fade, 6),
    }


# --------------------------------------------------------------------------- #
# A/B evaluation: does the signal improve the probability (out of sample)?
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SignalScore:
    name: str
    n_train: int
    n_test: int
    champion_brier: float
    augmented_brier: float
    brier_reduction: float
    slope: float
    t_stat: float
    p_value: float
    improves: bool
    mean_abs_signal: float
    insufficient: bool


def _fit_logistic_offset(offsets: Sequence[float], signals: Sequence[float], y: Sequence[int],
                         ridge: float = 1.0, iters: int = 200, lr: float = 0.20) -> tuple[float, float, float, float]:
    """Fit p = sigmoid(offset + a + b·(s - mean)/std) by ridge gradient descent.

    Returns (a, b, mean, std). Standardising the signal keeps the step well-scaled
    and the ridge keeps b finite on separable/tiny samples. Deterministic.
    """
    n = len(signals)
    mean = sum(signals) / n if n else 0.0
    var = sum((s - mean) ** 2 for s in signals) / n if n else 0.0
    std = math.sqrt(var) if var > 1e-12 else 1.0
    xs = [(s - mean) / std for s in signals]
    a = 0.0
    b = 0.0
    for _ in range(iters):
        ga = 0.0
        gb = 0.0
        for off, x, yi in zip(offsets, xs, y):
            p = _sigmoid(off + a + b * x)
            err = p - yi
            ga += err
            gb += err * x
        ga = ga / n
        gb = gb / n + ridge * b / n
        a -= lr * ga
        b -= lr * gb
    return a, b, mean, std


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _two_sided_p(t: float, df: float) -> float:
    """Two-sided Student-t p-value via the regularised incomplete beta, with a
    normal fallback. Self-contained so this module has no ledger dependency."""
    if not math.isfinite(t):
        return 1.0
    if df <= 0:
        return _clamp(2.0 * (1.0 - _normal_cdf(abs(t))), 0.0, 1.0)
    x = df / (df + t * t)
    return _clamp(_betai(0.5 * df, 0.5, x), 0.0, 1.0)


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3.0e-7, 1.0e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _score_signal(name: str, champion_yes: Sequence[float], signal: Sequence[float],
                  y: Sequence[int], config: SignalConfig) -> SignalScore:
    n = len(y)
    if n < config.min_rows:
        return SignalScore(name, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, False,
                           sum(abs(s) for s in signal) / n if n else 0.0, True)
    split = max(1, int(round(config.train_fraction * n)))
    split = min(split, n - 1)  # guarantee a non-empty test fold
    off_train = [_logit(p) for p in champion_yes[:split]]
    a, b, mean, std = _fit_logistic_offset(off_train, signal[:split], y[:split])

    champ_se_sum = 0.0
    aug_se_sum = 0.0
    diffs: list[float] = []
    for p_champ, s, yi in zip(champion_yes[split:], signal[split:], y[split:]):
        x = (s - mean) / std
        p_aug = _sigmoid(_logit(p_champ) + a + b * x)
        champ_se = (p_champ - yi) ** 2
        aug_se = (p_aug - yi) ** 2
        champ_se_sum += champ_se
        aug_se_sum += aug_se
        diffs.append(champ_se - aug_se)  # >0 means the signal helped this row

    n_test = len(diffs)
    champ_brier = champ_se_sum / n_test
    aug_brier = aug_se_sum / n_test
    reduction = champ_brier - aug_brier
    mean_d = sum(diffs) / n_test
    var_d = sum((d - mean_d) ** 2 for d in diffs) / (n_test - 1) if n_test > 1 else 0.0
    se = math.sqrt(var_d / n_test) if var_d > 0 and n_test > 0 else 0.0
    t = mean_d / se if se > 0 else 0.0
    p_value = _two_sided_p(t, df=n_test - 1)
    improves = bool(reduction > config.brier_floor and mean_d > 0 and p_value < config.alpha)
    return SignalScore(
        name=name, n_train=split, n_test=n_test,
        champion_brier=round(champ_brier, 6), augmented_brier=round(aug_brier, 6),
        brier_reduction=round(reduction, 6), slope=round(b, 4),
        t_stat=round(t, 3), p_value=round(p_value, 5), improves=improves,
        mean_abs_signal=round(sum(abs(s) for s in signal) / n, 4), insufficient=False,
    )


def evaluate(rows: Sequence[Mapping[str, Any]], config: SignalConfig | None = None) -> dict[str, SignalScore]:
    """Grade every signal out of sample.

    ``rows`` must be ordered oldest-first and each carry ``champion_yes`` (the
    calibrated YES probability the live system used), ``outcome`` (1 for YES, 0
    for NO), and ``signals`` (the per-prediction dict produced by
    ``compute_signals``). Returns ``signal_name -> SignalScore``.
    """
    config = config or SignalConfig.from_env()
    champ: list[float] = []
    outcomes: list[int] = []
    per_signal: dict[str, list[float]] = {name: [] for name in SIGNAL_NAMES}
    for row in rows:
        champion_yes = _num(row.get("champion_yes"))
        outcome = row.get("outcome")
        signals = row.get("signals")
        if champion_yes is None or outcome not in (0, 1) or not isinstance(signals, Mapping):
            continue
        champ.append(_clamp(champion_yes, 1e-6, 1.0 - 1e-6))
        outcomes.append(int(outcome))
        for name in SIGNAL_NAMES:
            per_signal[name].append(_num(signals.get(name), 0.0) or 0.0)
    return {name: _score_signal(name, champ, per_signal[name], outcomes, config) for name in SIGNAL_NAMES}


def build_report_lines(scores: Mapping[str, SignalScore]) -> list[str]:
    """Compact hourly-report block. Returns [] when there is nothing to show."""
    usable = [s for s in scores.values() if not s.insufficient]
    if not usable:
        # Surface the gating sample count once so the silence is explained.
        any_score = next(iter(scores.values()), None)
        if any_score is None:
            return []
        return ["", "🧪 Experimental signals — gathering data (insufficient settled rows)"]
    lines = ["", "🧪 Experimental signals (shadow A/B — out-of-sample)"]
    lines.append(f"{'signal':<13}{'ΔBrier':>9}{'p':>7}{'n':>6}{'':>5}")
    # Best (most negative augmented Brier improvement) first.
    for score in sorted(usable, key=lambda s: -s.brier_reduction):
        label = _SIGNAL_LABELS.get(score.name, score.name)[:12]
        mark = "✅" if score.improves else ("·" if score.brier_reduction > 0 else "✗")
        lines.append(
            f"{label:<13}{score.brier_reduction:>+9.4f}{score.p_value:>7.3f}{score.n_test:>6}{mark:>5}"
        )
    winners = [s for s in usable if s.improves]
    if winners:
        names = ", ".join(_SIGNAL_LABELS.get(s.name, s.name) for s in winners)
        lines.append(f"Significant lift: {names} (promotion candidates — manual review)")
    return lines


def scores_to_dict(scores: Mapping[str, SignalScore]) -> dict[str, Any]:
    """JSON-friendly view for /api and the dashboard."""
    return {
        name: {
            "n_train": s.n_train, "n_test": s.n_test,
            "champion_brier": s.champion_brier, "augmented_brier": s.augmented_brier,
            "brier_reduction": s.brier_reduction, "slope": s.slope,
            "t_stat": s.t_stat, "p_value": s.p_value, "improves": s.improves,
            "mean_abs_signal": s.mean_abs_signal, "insufficient": s.insufficient,
        }
        for name, s in scores.items()
    }


__all__ = [
    "SIGNAL_NAMES",
    "SignalConfig",
    "SignalScore",
    "build_report_lines",
    "compute_signals",
    "evaluate",
    "scores_to_dict",
]
