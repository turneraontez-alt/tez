"""Shadow FACTOR LAB — per-factor reliability attribution for official predictions.

READ-ONLY / SHADOW MODE. This module never changes a live prediction, an entry
decision, or the frozen champion. It answers, from already-settled official
predictions, which decision-time factors actually pointed at the final settled
result — and whether any of them have earned promotion into the active model.

Inputs are the ledger's :meth:`V95Ledger.resolved_factor_rows` export: each row is
one settled official prediction carrying its DECISION-TIME factor values
(``features``), the final settled ``result`` (YES/NO), and the settlement-derived
analysis targets (``correct``, ``changed_before_close``). The factors are the only
thing fed into the math; the outcome is strictly the target, so there is no
look-ahead in the factors themselves.

Per factor (a feature whose sign leans YES when positive) we compute, overall and
split by interval / market regime:

* **fired** — rows where the factor was materially present (``|value| > deadzone``).
* **final-aligned reliability** — of the fired rows, how often the factor's lean
  matched the FINAL settled side (the rest were temporary / misleading moves),
  with a Wilson lower bound so a thin sample reads as "unproven", not "skilled".
* **probability shift** — YES-settle-rate when the factor leaned YES minus when it
  leaned NO: how strongly the factor moved the YES/NO probability.
* **accuracy lift** — settled-accuracy on fired rows minus the base accuracy.
* **final-change rate** — how often the prediction genuinely changed side before
  close while the factor was present.

Factor COMBINATIONS (top factors, agreeing pairs) and a PROMOTION GATE are also
reported. A factor is only ever marked *promotion-ready* — it is never applied —
once it clears every bar: enough samples, a Wilson-LB reliability over threshold,
out-of-sample consistency across a time split, and a measurable probability shift.
Promotion remains a manual, significance-tested decision elsewhere.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

INTERVALS = ("15M", "10M", "7M")

# Defaults (all overridable per call). Deliberately conservative so the gate does
# not promote noise.
DEFAULT_DEADZONE = 0.05            # |value| below this = factor not materially present
DEFAULT_MIN_SAMPLES = 40          # fired rows required before a factor is judged
DEFAULT_RELIABILITY_THRESHOLD = 0.55  # Wilson-LB final-aligned rate to be "reliable"
DEFAULT_OOS_FLOOR = 0.52          # each time-half must beat this to be "consistent"
DEFAULT_MIN_SHIFT = 0.05          # minimum YES/NO probability shift to count as useful
DEFAULT_TOP_FOR_COMBOS = 6        # cap pairwise combos to C(N,2)


def _wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial proportion. 0.0 for n == 0."""
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _factor_names(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every factor key seen in any row's features, in stable sorted order."""
    names: set[str] = set()
    for row in rows:
        feats = row.get("features")
        if isinstance(feats, Mapping):
            names.update(str(k) for k in feats.keys())
    return sorted(names)


def _fired_direction(value: float | None, deadzone: float) -> str | None:
    """'YES' / 'NO' when the factor is materially present, else None."""
    if value is None:
        return None
    if value > deadzone:
        return "YES"
    if value < -deadzone:
        return "NO"
    return None


def _reliability_block(fired_pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """fired_pairs = [(lean, result), ...] -> {fired, aligned, reliability, lb}."""
    fired = len(fired_pairs)
    aligned = sum(1 for lean, result in fired_pairs if lean == result)
    reliability = (aligned / fired) if fired else 0.0
    return {
        "fired": fired,
        "aligned": aligned,
        "reliability": reliability,
        "reliability_lb": _wilson_lower_bound(aligned, fired),
    }


def _analyse_factor(name: str, rows: Sequence[Mapping[str, Any]], *,
                    deadzone: float, base_accuracy: float | None,
                    min_samples: int, reliability_threshold: float,
                    oos_floor: float, min_shift: float) -> dict[str, Any] | None:
    """Full reliability profile for one factor, or None if it never fired."""
    fired_pairs: list[tuple[str, str]] = []          # (lean, result), time-ordered
    yes_when_yes_lean = yes_when_no_lean = 0          # numerators for prob shift
    n_yes_lean = n_no_lean = 0
    correct_sum = correct_n = 0
    change_sum = change_n = 0
    by_interval: dict[str, list[tuple[str, str]]] = {iv: [] for iv in INTERVALS}
    by_regime: dict[str, list[tuple[str, str]]] = {}

    for row in rows:
        feats = row.get("features")
        if not isinstance(feats, Mapping):
            continue
        lean = _fired_direction(_num(feats.get(name)), deadzone)
        if lean is None:
            continue
        result = str(row.get("result") or "").upper()
        if result not in ("YES", "NO"):
            continue
        pair = (lean, result)
        fired_pairs.append(pair)
        if lean == "YES":
            n_yes_lean += 1
            yes_when_yes_lean += 1 if result == "YES" else 0
        else:
            n_no_lean += 1
            yes_when_no_lean += 1 if result == "YES" else 0
        c = row.get("correct")
        if c is not None:
            correct_sum += int(c)
            correct_n += 1
        change_sum += int(row.get("changed_before_close") or 0)
        change_n += 1
        iv = str(row.get("checkpoint") or "").upper()
        if iv in by_interval:
            by_interval[iv].append(pair)
        regime = str(row.get("regime") or "?")
        by_regime.setdefault(regime, []).append(pair)

    if not fired_pairs:
        return None

    overall = _reliability_block(fired_pairs)
    yes_rate_hi = (yes_when_yes_lean / n_yes_lean) if n_yes_lean else None
    yes_rate_lo = (yes_when_no_lean / n_no_lean) if n_no_lean else None
    prob_shift = (yes_rate_hi - yes_rate_lo) if (yes_rate_hi is not None and yes_rate_lo is not None) else None
    accuracy = (correct_sum / correct_n) if correct_n else None
    accuracy_lift = (accuracy - base_accuracy) if (accuracy is not None and base_accuracy is not None) else None

    # Out-of-sample consistency: split the fired rows into time halves (they arrive
    # time-ordered) and require each half to clear the floor.
    half = len(fired_pairs) // 2
    first, second = fired_pairs[:half], fired_pairs[half:]
    oos = None
    if half >= max(2, min_samples // 2):
        r1 = _reliability_block(first)["reliability"]
        r2 = _reliability_block(second)["reliability"]
        oos = {"first": r1, "second": r2, "consistent": (r1 >= oos_floor and r2 >= oos_floor)}

    # Promotion gate (shadow only — reported, never applied).
    reasons: list[str] = []
    if overall["fired"] < min_samples:
        reasons.append(f"insufficient_samples({overall['fired']}<{min_samples})")
    if overall["reliability_lb"] < reliability_threshold:
        reasons.append(f"reliability_lb({overall['reliability_lb']:.2f}<{reliability_threshold:.2f})")
    if oos is None:
        reasons.append("insufficient_oos_samples")
    elif not oos["consistent"]:
        reasons.append("oos_inconsistent")
    if prob_shift is None or abs(prob_shift) < min_shift:
        shown = "n/a" if prob_shift is None else f"{abs(prob_shift):.2f}"
        reasons.append(f"prob_shift_too_small({shown}<{min_shift:.2f})")

    return {
        "name": name,
        "fired": overall["fired"],
        "reliability": overall["reliability"],
        "reliability_lb": overall["reliability_lb"],
        "prob_shift": prob_shift,
        "accuracy_lift": accuracy_lift,
        "final_change_rate": (change_sum / change_n) if change_n else None,
        "by_interval": {
            iv: _reliability_block(pairs) for iv, pairs in by_interval.items() if pairs
        },
        "by_regime": {
            reg: _reliability_block(pairs) for reg, pairs in sorted(by_regime.items()) if pairs
        },
        "oos": oos,
        "promotion": {"ready": not reasons, "reasons": reasons},
    }


def _combos(factors: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], *,
            deadzone: float, top_n: int, min_samples: int) -> list[dict[str, Any]]:
    """Agreeing-pair reliability among the top factors: rows where both factors
    fired the SAME side, graded against the final result."""
    names = [str(f["name"]) for f in factors[:top_n]]
    out: list[dict[str, Any]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            pairs: list[tuple[str, str]] = []
            for row in rows:
                feats = row.get("features")
                if not isinstance(feats, Mapping):
                    continue
                la = _fired_direction(_num(feats.get(a)), deadzone)
                lb = _fired_direction(_num(feats.get(b)), deadzone)
                if la is None or lb is None or la != lb:
                    continue
                result = str(row.get("result") or "").upper()
                if result in ("YES", "NO"):
                    pairs.append((la, result))
            if len(pairs) >= max(2, min_samples // 2):
                block = _reliability_block(pairs)
                out.append({"factors": [a, b], **block})
    out.sort(key=lambda c: (c["reliability_lb"], c["fired"]), reverse=True)
    return out


def analyze(rows: Sequence[Mapping[str, Any]], *,
            deadzone: float = DEFAULT_DEADZONE,
            min_samples: int = DEFAULT_MIN_SAMPLES,
            reliability_threshold: float = DEFAULT_RELIABILITY_THRESHOLD,
            oos_floor: float = DEFAULT_OOS_FLOOR,
            min_shift: float = DEFAULT_MIN_SHIFT,
            top_for_combos: int = DEFAULT_TOP_FOR_COMBOS) -> dict[str, Any]:
    """Build the full shadow factor-reliability report from settled rows."""
    rows = list(rows)
    if not rows:
        return {"available": False, "total_samples": 0, "factors": [], "combinations": [],
                "notes": ["no settled official predictions yet"]}

    yes_n = sum(1 for r in rows if str(r.get("result") or "").upper() == "YES")
    correct_vals = [int(r["correct"]) for r in rows if r.get("correct") is not None]
    base_accuracy = (sum(correct_vals) / len(correct_vals)) if correct_vals else None

    factors: list[dict[str, Any]] = []
    for name in _factor_names(rows):
        prof = _analyse_factor(
            name, rows, deadzone=deadzone, base_accuracy=base_accuracy,
            min_samples=min_samples, reliability_threshold=reliability_threshold,
            oos_floor=oos_floor, min_shift=min_shift,
        )
        if prof is not None:
            factors.append(prof)
    # Rank by proven reliability (Wilson LB), then by how strongly it moved the
    # probability, then by sample size.
    factors.sort(key=lambda f: (f["reliability_lb"], abs(f["prob_shift"] or 0.0), f["fired"]), reverse=True)

    combinations = _combos(factors, rows, deadzone=deadzone, top_n=top_for_combos,
                           min_samples=min_samples)

    return {
        "available": True,
        "total_samples": len(rows),
        "base_yes_rate": yes_n / len(rows),
        "base_accuracy": base_accuracy,
        "factors": factors,
        "combinations": combinations,
        "promotion_ready": [f["name"] for f in factors if f["promotion"]["ready"]],
        "params": {
            "deadzone": deadzone, "min_samples": min_samples,
            "reliability_threshold": reliability_threshold, "oos_floor": oos_floor,
            "min_shift": min_shift,
        },
        "notes": [
            "SHADOW MODE — nothing here is auto-applied to live predictions or entries.",
            "Factors are decision-time only; outcomes are the target (no look-ahead).",
        ],
    }


# --------------------------------------------------------------------------- #
#  Plain-text rendering (for the stats CLI)                                    #
# --------------------------------------------------------------------------- #
def _pct(value: Any, digits: int = 0) -> str:
    f = _num(value)
    return "—" if f is None else f"{f * 100:.{digits}f}%"


def _signed_pct(value: Any) -> str:
    f = _num(value)
    return "—" if f is None else f"{f * 100:+.0f}pp"


def format_report(report: Mapping[str, Any], *, top: int = 12, detail: bool = False) -> list[str]:
    """Render the factor report as compact text lines for the stats command."""
    if not report.get("available"):
        return ["FACTOR LAB (shadow): " + "; ".join(report.get("notes") or ["no data"])]
    lines: list[str] = []
    lines.append(f"FACTOR LAB (shadow) — {report['total_samples']} settled predictions · "
                 f"base YES {_pct(report.get('base_yes_rate'))} · "
                 f"base acc {_pct(report.get('base_accuracy'))}")
    p = report.get("params", {})
    lines.append(f"  gate: n≥{p.get('min_samples')} · reliability-LB≥"
                 f"{p.get('reliability_threshold')} · OOS≥{p.get('oos_floor')} · "
                 f"shift≥{p.get('min_shift')}")
    lines.append("  factor                 fired  reliab(LB)   shift   acc-lift  ready")
    for f in report.get("factors", [])[:top]:
        ready = "✓" if f["promotion"]["ready"] else "·"
        lines.append(
            f"  {str(f['name'])[:20]:<20}  {f['fired']:>5}  "
            f"{_pct(f['reliability']):>4}({_pct(f['reliability_lb']):>4}) "
            f"{_signed_pct(f['prob_shift']):>6}  {_signed_pct(f['accuracy_lift']):>7}   {ready}"
        )
        if detail:
            iv = " ".join(f"{k}:{_pct(v['reliability'])}({v['fired']})"
                          for k, v in f.get("by_interval", {}).items())
            if iv:
                lines.append(f"      by interval: {iv}")
            if not f["promotion"]["ready"]:
                lines.append(f"      blocked: {', '.join(f['promotion']['reasons'])}")
    combos = report.get("combinations", [])
    if combos:
        lines.append("  strongest agreeing combinations:")
        for c in combos[:5]:
            lines.append(f"    {' + '.join(c['factors'])}: "
                         f"{_pct(c['reliability'])}({_pct(c['reliability_lb'])} LB) over {c['fired']}")
    ready = report.get("promotion_ready") or []
    lines.append(f"  promotion-ready (shadow, NOT applied): "
                 f"{', '.join(ready) if ready else 'none yet'}")
    return lines
