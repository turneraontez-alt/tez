"""Telegram panel builders for the V9.5 checkpoint UI.

Two surfaces, both rendered as a single ``<pre>`` body with a bold header outside
(the established single-panel convention; the checkpoint panel keeps the
``V9.5 CHECK`` marker the suppression + formatter chain key on):

* :func:`build_checkpoint_panel` — the live, FORWARD-LOOKING push at each 15M/10M/7M
  checkpoint: manipulation watch, the current YES/NO prediction (with WATCH clarity
  and the prior call so a side flip is unmistakable), and graduated entry guidance.
  It deliberately carries NO win/loss record.

* :func:`build_cycle_recap` — the ONE close-out push after a contract settles: what
  happened to that contract (per-interval hit/miss, flips, entry result, manipulation
  call) plus the running official W-L totals.

These are pure formatters: no I/O, no ledger access, no decisions. The live loop
maps its data into these arguments and owns sending / official-record writing.
"""
from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

# Entry states, ordered strongest -> weakest. The label is what the owner reads;
# the band mapping (0-100 score) arrives with the Phase-2 shadow score.
ENTER_NOW = "ENTER_NOW"
ENTRY_RECOMMENDED = "ENTRY_RECOMMENDED"
WAIT = "WAIT"
WATCH = "WATCH"
NO_ENTRY = "NO_ENTRY"

_ENTRY_LABEL = {
    ENTER_NOW: "🟢 ENTRY: ENTER NOW",
    ENTRY_RECOMMENDED: "✅ ENTRY: ENTRY RECOMMENDED",
    WAIT: "⏳ ENTRY: WAIT FOR BETTER PRICE",
    WATCH: "👁 ENTRY: WATCH ONLY",
    NO_ENTRY: "🛑 ENTRY: NO ENTRY",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: Any, *, digits: int = 1) -> str:
    """Format a 0..1 fraction (or None) as a percent string."""
    try:
        if value is None:
            return "—"
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _cents(value: Any) -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value):.0f}¢"
    except (TypeError, ValueError):
        return "—"


def _side(value: Any) -> str:
    s = str(value or "").upper()
    return s if s in {"YES", "NO"} else "—"


# --------------------------------------------------------------------------- #
#  Live checkpoint panel                                                       #
# --------------------------------------------------------------------------- #
def build_checkpoint_panel(*, checkpoint: str, asset: str, side: str,
                           probability: float | None,
                           prior_side: str | None = None,
                           prior_checkpoint: str | None = None,
                           manipulation: Mapping[str, Any] | None = None,
                           entry_state: str,
                           entry: Mapping[str, Any] | None = None) -> str:
    """Render the forward-looking checkpoint panel.

    ``manipulation`` (when a watch is active): ``{risk, level, type,
    direction_after, entry_effect}`` where ``risk`` is a percent number (0..100).
    ``entry`` carries the WAIT target block / WATCH reason when relevant:
    ``{current_price, entry_low, entry_high, max_price, trigger, reason}``.
    The header keeps the ``V9.5 CHECK`` marker; the body is one ``<pre>`` panel."""
    cp = str(checkpoint).upper()
    header = f"🔎 <b>V9.5 CHECK — {_esc(cp)} · {_esc(asset)}</b>"
    body: list[str] = []

    # --- manipulation watch ---
    if manipulation:
        risk = manipulation.get("risk")
        level = manipulation.get("level")
        risk_txt = "—" if risk is None else f"{float(risk):.0f}%"
        body.append("⚠️ MANIPULATION")
        body.append(f"Risk: {risk_txt}{(' ' + str(level)) if level else ''}")
        if manipulation.get("type"):
            body.append(f"Type: {_esc(manipulation['type'])}")
        if manipulation.get("direction_after"):
            body.append(f"Direction after: {_side(manipulation['direction_after'])}")
        if manipulation.get("entry_effect"):
            body.append(f"Entry effect: {_esc(manipulation['entry_effect'])}")
        body.append("")
    else:
        body.append("✅ MANIPULATION: none detected")
        body.append("")

    # --- prediction (with prior-call clarity) ---
    body.append("🔮 PREDICTION")
    body.append(f"Side: {_side(side)}")
    body.append(f"Probability: {_pct(probability, digits=0)}")
    prior = _side(prior_side)
    if prior != "—":
        where = f" at {_esc(prior_checkpoint)}" if prior_checkpoint else ""
        if prior == _side(side):
            body.append(f"Was: {prior}{where} (unchanged)")
        else:
            body.append(f"Was: {prior}{where}  ⚠️ FLIPPED")
    body.append("")

    # --- graduated entry guidance ---
    body.append(_ENTRY_LABEL.get(entry_state, _ENTRY_LABEL[NO_ENTRY]))
    entry = entry or {}
    if entry_state == WAIT:
        if entry.get("current_price") is not None:
            body.append(f"Current price: {_cents(entry['current_price'])}")
        lo, hi = entry.get("entry_low"), entry.get("entry_high")
        if lo is not None and hi is not None:
            body.append(f"Recommended entry: {_cents(lo)}–{_cents(hi)}")
        if entry.get("max_price") is not None:
            body.append(f"Maximum price: {_cents(entry['max_price'])}")
        if entry.get("trigger"):
            body.append(f"Trigger: {_esc(entry['trigger'])}")
    elif entry_state in (ENTER_NOW, ENTRY_RECOMMENDED):
        if entry.get("current_price") is not None:
            body.append(f"Current price: {_cents(entry['current_price'])}")
        if entry.get("max_price") is not None:
            body.append(f"Maximum price: {_cents(entry['max_price'])}")
    elif entry_state == WATCH and entry.get("reason"):
        body.append(f"Reason: {_esc(entry['reason'])}")

    return header + "\n<pre>\n" + "\n".join(body).rstrip() + "\n</pre>"


# --------------------------------------------------------------------------- #
#  End-of-cycle recap                                                          #
# --------------------------------------------------------------------------- #
def _wl(bucket: Mapping[str, Any] | None) -> tuple[int, int, Any, bool]:
    bucket = bucket or {}
    return (int(bucket.get("right") or 0), int(bucket.get("wrong") or 0),
            bucket.get("accuracy"), bool(bucket.get("low_n")))


def _interval_record_line(label: str, group: Mapping[str, Any] | None) -> str:
    group = group or {}
    yr, yw, _, _ = _wl(group.get("yes"))
    nr, nw, _, _ = _wl(group.get("no"))
    tr, tw, tacc, low = _wl(group.get("total"))
    tail = " (low)" if low else ""
    return f"{label:<5} Y {yr}-{yw}  N {nr}-{nw}  T {tr}-{tw} {_pct(tacc)}{tail}"


def build_cycle_recap(*, asset: str, close_label: str, result: str,
                      intervals: Sequence[Mapping[str, Any]],
                      flips: str | None = None,
                      entry_result: Mapping[str, Any] | None = None,
                      manipulation_result: Mapping[str, Any] | None = None,
                      official: Mapping[str, Any] | None = None) -> str:
    """Render the single close-out recap for a settled contract.

    ``intervals`` is a list of ``{interval, side, hit}`` for the checkpoints that
    were actually sent for this contract. ``official`` is the
    :meth:`V95Ledger.official_scoreboard` dict for the running-record block."""
    header = f"🏁 <b>CYCLE CLOSED — {_esc(asset)} {_esc(close_label)}</b>"
    body: list[str] = [f"Result: {_side(result)}"]

    correct = 0
    counted = 0
    for item in intervals:
        cp = str(item.get("interval") or "").upper()
        side = _side(item.get("side"))
        hit = item.get("hit")
        if hit is not None:
            counted += 1
            correct += 1 if hit else 0
        mark = "✓" if hit else ("✗" if hit is not None else "—")
        body.append(f"{cp + ':':<5} {side:<3} {mark}")

    body.append(f"Flips: {_esc(flips) if flips else 'none'}")
    if counted:
        body.append(f"Predictability: {correct}/{counted} correct")

    if entry_result:
        cp = str(entry_result.get("checkpoint") or "").upper()
        decision = entry_result.get("decision") or "—"
        outcome = entry_result.get("outcome")
        cents = entry_result.get("cents")
        tail = ""
        if outcome:
            tail = f" → {outcome}"
            if cents is not None:
                tail += f" {float(cents):+.0f}¢"
        body.append(f"Entry: {_esc(decision)}{(' @' + cp) if cp else ''}{tail}")

    if manipulation_result:
        if manipulation_result.get("flagged"):
            verdict = manipulation_result.get("correct")
            vtxt = "correct" if verdict else ("wrong" if verdict is not None else "—")
            mtype = manipulation_result.get("type")
            body.append(f"Manipulation: {_esc(mtype) if mtype else 'flagged'} → {vtxt}")
        else:
            body.append("Manipulation: none flagged")

    if official and official.get("available"):
        body.append("")
        body.append("— RUNNING RECORD —")
        for interval in ("15M", "10M", "7M"):
            body.append(_interval_record_line(interval, official.get(interval)))
        body.append(_interval_record_line("ENTRY", official.get("entry")))
        mr, mw, macc, mlow = _wl(official.get("manipulation"))
        body.append(f"MANIP {mr}-{mw} {_pct(macc)}{' (low)' if mlow else ''}")

    return header + "\n<pre>\n" + "\n".join(body).rstrip() + "\n</pre>"
