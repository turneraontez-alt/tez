"""Honest model-accuracy / promotion-readiness readout.

A PURE interpreter over ``V95Ledger.metrics()`` — it recomputes nothing and
changes no model state. It turns the buried Brier / log-loss / paired-significance
numbers into a single glanceable verdict that answers the questions that actually
matter for a calibrated binary predictor:

* Is there enough resolved data to judge anything yet? (N vs the promotion gate)
* Is the model better *calibrated* than chance? (expected calibration error)
* Is it more *skillful* than just trusting the market price? (Brier vs market)
* Is the shadow challenger genuinely better than the frozen champion, and
  significantly so? (paired Brier test p-values)
* Is the realized edge positive?

Raw % "accuracy" is included for reference but is deliberately NOT the headline —
for binaries, calibration + skill-vs-market + edge are what matter.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# Below this many resolved rows, accuracy/calibration are noise, not signal.
MIN_ROWS_TO_TRUST = 30


def _ece(bands: Sequence[Mapping[str, Any]]) -> float | None:
    """Expected calibration error: count-weighted |actual_win_rate - predicted|.

    0.0 = perfectly calibrated; higher = more over/under-confident.
    """
    total = sum(int(b.get("count") or 0) for b in bands)
    if total <= 0:
        return None
    acc = 0.0
    for b in bands:
        count = int(b.get("count") or 0)
        actual = b.get("actual_win_rate")
        predicted = b.get("mean_predicted")
        if actual is None or predicted is None:
            continue
        acc += (count / total) * abs(float(actual) - float(predicted))
    return round(acc, 4)


def _sub(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 6)


def build_accuracy_report(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    """Interpret a ledger ``metrics()`` dict into a readiness report."""
    if not isinstance(metrics, Mapping) or not metrics.get("available"):
        return {"available": False, "reason": (metrics or {}).get("error") or "ledger_unavailable"}

    min_rows = int(metrics.get("minimum_promotion_rows") or 50)
    primary = str(metrics.get("primary_learning_checkpoint") or "10M")
    by_cp_metrics: Mapping[str, Any] = metrics.get("by_checkpoint") or {}
    promo_by_cp: Mapping[str, Any] = metrics.get("promotion_by_checkpoint") or {}
    sb_by_cp: Mapping[str, Any] = ((metrics.get("scoreboard") or {}).get("by_checkpoint")) or {}
    overall = metrics.get("overall") or {}
    bands = metrics.get("calibration_bands") or []

    def _cp_block(cp: str) -> dict[str, Any]:
        m = by_cp_metrics.get(cp) or {}
        promo = promo_by_cp.get(cp) or {}
        sb = sb_by_cp.get(cp) or {}
        resolved = int(m.get("resolved") or 0)
        champ = m.get("champion_brier")
        chall = m.get("challenger_brier")
        market = m.get("baseline_brier")
        realized = sb.get("realized_avg_cents")
        learning = bool(promo.get("learning_enabled"))
        candidate = bool(promo.get("candidate"))
        if not learning:
            verdict = "LEARNING_OFF"
        elif resolved < min_rows:
            verdict = "ACCUMULATING"
        elif candidate:
            verdict = "PROMOTION_CANDIDATE"
        else:
            verdict = "CHALLENGER_NOT_BETTER"
        return {
            "resolved": resolved,
            "rows_needed_for_promotion": max(0, min_rows - resolved),
            "sample_trustworthy": resolved >= MIN_ROWS_TO_TRUST,
            "accuracy": m.get("accuracy"),
            "champion_brier": champ,
            "challenger_brier": chall,
            "market_brier": market,
            # >0 means the model's probabilities are more accurate than the market line.
            "champion_skill_vs_market": _sub(market, champ),
            # >0 means the challenger is more accurate than the frozen champion.
            "challenger_edge_vs_champion": _sub(champ, chall),
            "vs_champion_p_value": (promo.get("vs_champion") or {}).get("p_value"),
            "vs_market_p_value": (promo.get("vs_baseline") or {}).get("p_value"),
            "realized_avg_cents": realized,
            "edge_positive": (float(realized) > 0) if realized is not None else None,
            "learning_enabled": learning,
            "promotion_candidate": candidate,
            "promotion_reason": promo.get("reason"),
            "verdict": verdict,
        }

    by_checkpoint = {cp: _cp_block(cp) for cp in by_cp_metrics}
    primary_block = by_checkpoint.get(primary, {})
    overall_resolved = int(overall.get("resolved") or 0)
    p_resolved = int(primary_block.get("resolved") or 0)

    if overall_resolved == 0:
        headline = "No resolved predictions yet — predictions are flowing; let them settle."
    elif p_resolved < min_rows:
        headline = (
            f"ACCUMULATING — {p_resolved}/{min_rows} resolved on {primary} (primary). "
            "Too little data to judge accuracy; keep baking."
        )
    elif primary_block.get("verdict") == "PROMOTION_CANDIDATE":
        headline = (
            f"PROMOTION CANDIDATE on {primary} — challenger significantly beats champion "
            "and market. Review and promote manually."
        )
    else:
        headline = (
            f"{p_resolved} resolved on {primary}; challenger not yet significantly "
            "better than champion."
        )

    return {
        "available": True,
        "model_version": metrics.get("model_version"),
        "primary_checkpoint": primary,
        "minimum_promotion_rows": min_rows,
        "min_rows_to_trust": MIN_ROWS_TO_TRUST,
        "calibration_error_ece": _ece(bands),  # 0 = perfectly calibrated
        "overall_resolved": overall_resolved,
        "headline": headline,
        "by_checkpoint": by_checkpoint,
        "calibration_bands": list(bands),
    }


def compact_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """A tiny, glanceable slice for /api/health (no per-band detail)."""
    if not report.get("available"):
        return {"available": False}
    primary = report.get("primary_checkpoint")
    block = (report.get("by_checkpoint") or {}).get(primary, {})
    return {
        "available": True,
        "headline": report.get("headline"),
        "primary_checkpoint": primary,
        "primary_verdict": block.get("verdict"),
        "primary_resolved": block.get("resolved"),
        "rows_needed_for_promotion": block.get("rows_needed_for_promotion"),
        "calibration_error_ece": report.get("calibration_error_ece"),
        "champion_skill_vs_market": block.get("champion_skill_vs_market"),
    }
