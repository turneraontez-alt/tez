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


def _grade(value: Any) -> str:
    """A pick's A/B/C/D confidence grade, or em-dash when absent/invalid."""
    g = str(value or "").strip().upper()
    return g if g in {"A", "B", "C", "D"} else "—"


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
#  Ranked Top-3 checkpoint panel (the official interval report)                #
# --------------------------------------------------------------------------- #
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Display thresholds (0..100). The flip "→ SIDE" target is only shown once the
# genuine-flip risk is high enough to matter; manipulation is flagged as
# entry-affecting ("— WAIT") only once it is high. Both keep the panel quiet
# until a number actually changes the decision.
_FLIP_ARROW_MIN = 35.0
_MANIP_WAIT_MIN = 60.0


def _num100(value: Any) -> str:
    """Format a 0..100 number as a whole-percent string, or em-dash."""
    try:
        if value is None:
            return "—"
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "—"


def _prob01(value: Any) -> str:
    """Format a 0..1 probability as a whole-percent string, or em-dash. A
    threshold >=1.0 means 'not yet validated' and renders as a dash."""
    try:
        if value is None:
            return "—"
        f = float(value)
        if f > 1.0:                      # sentinel un-validated threshold (1.01)
            return "—"
        return f"{f * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _flip_decision(value: Any) -> str:
    return "YES" if str(value or "").upper() == "YES" else "NO"


def build_ranked_checkpoint_panel(*, checkpoint: str, picks: Sequence[Mapping[str, Any] | None],
                                  top_k: int = 3) -> str:
    """Render the official interval report: the three ranked FINAL-OUTCOME picks
    plus one compact decision block.

    Each visible line answers only the questions that matter for the close:
    the three most-likely settled sides (``asset side — confidence · grade``, #1
    first, where grade is the champion's A/B/C/D confidence grade),
    then a single decision block keyed to the headline (#1) pick — genuine-flip
    risk, temporary-manipulation risk, the ENTER/WAIT/SKIP call, the best entry
    price, the one strongest reason, and the calibration sample. All the detailed
    feature/edge/calibration math stays in the background. A ``None`` slot (fewer
    than ``top_k`` valid assets) renders as ``—`` — never an invented pick.

    Each pick is a fully-extracted mapping (see ``checkpoint_v95._extract_pick``):
    ``{rank, asset, side, confidence, confidence_grade, flip_prob, flip_side,
       manip_prob, entry_label, best_entry_max, main_reason, sample, is_entry,
       ...}``.

    The header keeps the ``V9.5 CHECK`` marker and an actionable/non-actionable
    marker (``ENTRY RECOMMENDED`` / ``NO ENTRY YET``) the suppression chain keys
    on; the body is one ``<pre>`` panel.
    """
    cp = str(checkpoint).upper()
    any_entry = any(bool(p and p.get("is_entry")) for p in picks)
    marker = "ENTRY RECOMMENDED" if any_entry else "NO ENTRY YET"
    # "TOP 3 PICKS" stamps this as the OFFICIAL per-interval report (one per window,
    # carrying the three ranked final-outcome picks). The notifier always delivers
    # it — even on a NO-ENTRY interval — so the owner sees every interval check and
    # the visible record / Shadow-vs-Yours comparison fills. The ENTRY RECOMMENDED /
    # NO ENTRY YET marker is preserved for the formatter + record chain.
    header = f"🔎 <b>V9.5 CHECK — {_esc(cp)} · TOP 3 PICKS · {marker}</b>"

    body: list[str] = [f"{_esc(cp)} CHECK", ""]

    # --- the three ranked final-outcome picks (most likely settled side first) ---
    for rank in range(1, top_k + 1):
        pick = picks[rank - 1] if rank - 1 < len(picks) else None
        medal = _MEDALS.get(rank, f"#{rank}")
        if not pick:
            body.append(f"{medal} —")
            continue
        body.append(f"{medal} {_esc(pick.get('asset'))} {_side(pick.get('side'))} "
                    f"— {_pct(pick.get('confidence'), digits=0)} "
                    f"· {_grade(pick.get('confidence_grade'))}")

    # --- one decision block, keyed to the headline (#1) pick ---
    head = next((p for p in picks if p), None)
    if head:
        body.append("")
        # Strict FLIP CHECK — the ONLY flip/manipulation output shown. A YES/NO
        # call on whether the predicted side genuinely flips by settlement, vs a
        # learned, validated per-interval threshold. All manipulation math,
        # evidence and validation stay in the background (never shown here).
        body.append(f"{_esc(cp)} FLIP CHECK")
        body.append(f"Decision: {_flip_decision(head.get('flip_decision'))}")
        body.append(f"Flip Probability: {_prob01(head.get('flip_decision_probability'))}")
        body.append(f"Required Threshold: {_prob01(head.get('flip_decision_threshold'))}")
        body.append("")
        body.append(f"Entry: {_esc(head.get('entry_label') or '—')}")
        be = head.get("best_entry_max")
        body.append(f"Best entry: {('≤' + _cents(be)) if be is not None else '—'}")
        body.append(f"Main reason: {_esc(head.get('main_reason') or '—')}")
        sample = head.get("sample")
        body.append(f"Sample: {int(sample) if sample is not None else '—'}")

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
