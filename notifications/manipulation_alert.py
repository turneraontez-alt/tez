"""Gated manipulation-alert notification policy (read-only).

Detection keeps running every cycle (the flip-risk overlay + the manipulation
signals already computed in ``checkpoint_v95``). This module changes NOTHING
about detection or the prediction — it only decides which suspected-manipulation
findings are actually pushed to Telegram, per the owner's policy. A finding is
sent ONLY when all of these hold:

  1. HIGH PROBABILITY — the suspected manipulation has a high estimated chance of
     actually occurring (learned flip hit-rate for this risk level, conservative
     Wilson lower bound, clears ``min_probability``).
  2. NORMAL CHECK DELIVERED — the normal/base check for that interval was already
     *successfully delivered* (not muted, not failed). A manipulation alert never
     rides ahead of, or in place of, the normal check.
  3. RECOMMENDATION CHANGED — the manipulation analysis now recommends a DIFFERENT
     outcome / side / entry / exit / action than the normal check did for that
     interval. Unchanged calls are never re-sent.

Low-probability, unconfirmed, repetitive or unchanged findings are dropped, and
all qualifying findings for one interval are COMBINED into a single concise
alert. Every alert states: the estimated probability/confidence, what changed
after the normal check, the original recommendation, the new recommendation, the
evidence that triggered the change, and the time interval analysed.

This is pure policy + formatting so it is unit-testable without a database or a
live notifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


ALERT_MARKER = "MANIPULATION ALERT"


@dataclass
class NormalCheck:
    """What the normal/base check for an interval said, and whether it actually
    reached the owner. ``delivered`` is the strict gate for condition 2: muted or
    failed sends do NOT count as delivered."""
    checkpoint: str
    delivered: bool
    asset: str | None = None
    side: str | None = None       # recommended outcome, YES / NO
    action: str | None = None     # trade decision, e.g. ENTRY_RECOMMENDED / NO_ENTRY
    at: float = 0.0


@dataclass
class ManipCandidate:
    """One suspected-manipulation finding awaiting the notification gate."""
    asset: str
    checkpoint: str
    ticker: str
    probability: float | None          # 0..1 estimated chance it actually occurs
    confidence: float                  # 0..100 evidence coverage
    evidence: list[str] = field(default_factory=list)  # human-readable tells
    original_side: str | None = None   # the normal check's recommended side
    original_action: str | None = None # the normal check's recommended action
    new_side: str | None = None        # the manipulation-implied side (flip target)
    new_action: str | None = None      # the manipulation-implied action

    # ---- change detection (condition 3) ----
    @staticmethod
    def _norm(text: str | None) -> str:
        return str(text or "").strip().upper().replace(" ", "_")

    def side_changed(self) -> bool:
        return bool(self.new_side and self.original_side
                    and self._norm(self.new_side) != self._norm(self.original_side))

    def action_changed(self) -> bool:
        return bool(self.new_action and self.original_action
                    and self._norm(self.new_action) != self._norm(self.original_action))

    def changed(self) -> bool:
        return self.side_changed() or self.action_changed()


def qualifies(candidate: ManipCandidate, normal: NormalCheck | None, *,
              min_probability: float, min_confidence: float,
              already_sent: Iterable[tuple]) -> tuple[bool, str]:
    """Apply the three-part policy + dedup. Returns ``(send, reason)``.

    ``reason`` is a short machine label for diagnostics/logging — it is the FIRST
    failing gate, in policy order, or ``"ok"``.
    """
    # 2) The normal/base check for the interval must have been delivered first.
    if normal is None or not normal.delivered:
        return False, "normal_check_not_delivered"
    # 1) High probability the manipulation actually occurs (conservative estimate).
    if candidate.probability is None or candidate.probability < min_probability:
        return False, "low_probability"
    if candidate.confidence < min_confidence:
        return False, "low_confidence"
    # 3) Must recommend a DIFFERENT side / action than the normal check.
    if not candidate.changed():
        return False, "unchanged"
    # Drop repetitive / already-sent findings.
    if (candidate.ticker, candidate.checkpoint, ManipCandidate._norm(candidate.new_side)) in set(already_sent):
        return False, "repetitive"
    return True, "ok"


def _pct(value: float | None, scale: float = 1.0) -> str:
    return "—" if value is None else f"{round(float(value) * scale)}%"


def build_combined_alert(checkpoint: str, normal: NormalCheck | None,
                         candidates: list[ManipCandidate]) -> str:
    """One concise HTML alert covering every qualifying finding for the interval.

    Each finding block carries the six required fields. The header marks this as
    a post-check manipulation alert (it explicitly follows the normal check)."""
    lines = [
        f"⚠️ <b>{ALERT_MARKER} — {checkpoint}</b>",
        f"<i>Re-check AFTER your {checkpoint} check — the later analysis disagrees.</i>",
    ]
    for c in candidates:
        # What changed, in one line.
        if c.side_changed():
            changed = f"predicted outcome {c.original_side} → {c.new_side}"
        elif c.action_changed():
            changed = f"action {c.original_action} → {c.new_action}"
        else:  # defensive; gate guarantees a change
            changed = "recommendation revised"
        evidence = " + ".join(c.evidence) if c.evidence else "elevated manipulation signals"
        orig = " · ".join(p for p in (c.original_side, c.original_action) if p) or "—"
        new = " · ".join(p for p in (c.new_side, c.new_action) if p) or "—"
        lines += [
            "",
            f"<b>{c.asset}</b>",
            f"• Probability: {_pct(c.probability, 100)} (confidence {_pct(c.confidence)})",
            f"• What changed: {changed}",
            f"• Original recommendation: {orig}",
            f"• New recommendation: {new}",
            f"• Evidence: {evidence}",
            f"• Interval analysed: {c.checkpoint}",
        ]
    return "\n".join(lines)
