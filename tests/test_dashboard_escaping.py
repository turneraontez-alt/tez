"""Dashboard XSS-escaping regression tests (P3).

The live dashboard builds card markup with innerHTML. Several dynamic, possibly
upstream/public-API-derived strings — confirmation reasons, data-quality
warnings, the ticker/error text on simple cards, and the trade taker side — were
interpolated raw. If any such string ever contains markup it would render as
HTML. An ``esc()`` helper already existed; these paths now use it.

This is a static guard over templates/index.html (no JS engine required): it
pins that each flagged value is wrapped in ``esc()`` and that the old raw
interpolation is gone, so a future edit can't silently reintroduce the hole.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "index.html"
).read_text(encoding="utf-8")


def test_confirmation_reasons_are_escaped():
    assert '${esc(r)}' in TEMPLATE
    assert '<span class="confirm-chip">${r}</span>' not in TEMPLATE


def test_data_quality_warnings_are_escaped():
    assert '${esc(x)}' in TEMPLATE
    assert '<div class="warn">⚠ ${x}</div>' not in TEMPLATE


def test_simple_card_error_text_is_escaped():
    assert "esc(d.error||'No market')" in TEMPLATE
    assert "${d.error||'No market'}" not in TEMPLATE


def test_cycling_card_ticker_is_escaped():
    assert "esc(d.ticker)" in TEMPLATE
    assert "'Was: '+d.ticker)" not in TEMPLATE


def test_trade_taker_side_is_escaped():
    assert "esc(String(t.taker_side).toUpperCase())" in TEMPLATE
    assert "'taker '+t.taker_side.toUpperCase()" not in TEMPLATE


def test_underlying_source_is_escaped():
    assert "${esc(d.underlying_source)}" in TEMPLATE
    assert "${d.underlying_source}" not in TEMPLATE


def test_target_fallback_is_escaped():
    # d.target falls back to the raw Kalshi-API yes_sub_title string, served
    # unsanitized via /api/snapshot, so the fallback branch must be escaped.
    assert "esc(d.target||'–')" in TEMPLATE
    assert "(d.target_value?fmtUsd(d.target_value):(d.target||'–'))" not in TEMPLATE


def test_esc_helper_still_present():
    # The escaper itself must remain defined for the above call sites.
    assert "function esc(" in TEMPLATE
