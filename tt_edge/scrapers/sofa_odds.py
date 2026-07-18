"""Sofascore-provided odds -> American prices (the Phase 1 automated odds
source).

Sofascore's undocumented ``/api/v1/event/{id}/odds/{provider}/all`` payload
carries a match-winner market whose two choices are named "1" (home) and
"2" (away), priced as FRACTIONAL strings ("4/5", "21/20", "1") — with some
regions serving decimal strings ("1.80"). Both are parsed exactly with
Decimal and converted to American.

IMPORTANT OPERATOR CONTRACT (also in the README and on every alert built
from this book): these are sofascore's aggregated prices, NOT your book's.
An alert priced from this source means "the edge exists AT THIS PRICE" —
take the bet only if your book posts this price or better. The manual
odds-entry flow remains the source of truth when you have the book open.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from tt_edge.edge.vig import OddsError, validate_american

BOOK = "sofascore"

# Market names observed for the two-way match result across sports on the
# odds endpoint; matched case-insensitively, first hit wins.
_WINNER_MARKETS = ("full time", "match winner", "winner")

_HUNDRED = Decimal(100)
_ONE = Decimal(1)


def fractional_to_decimal_odds(text: str) -> Decimal:
    """'4/5' -> 1.8, '21/20' -> 2.05, '1' -> 2.0 (evens), '1.85' -> 1.85.

    Sofascore fractional values are profit per unit staked; a bare number
    without '/' that looks like odds shorter than 1.01 total return is
    treated as fractional evens-style (e.g. '1' == 1/1); values with a
    decimal point are decimal (total-return) odds already.
    """
    cleaned = text.strip()
    if not cleaned:
        raise OddsError("empty odds value")
    try:
        if "/" in cleaned:
            numerator_text, _, denominator_text = cleaned.partition("/")
            numerator = Decimal(numerator_text.strip())
            denominator = Decimal(denominator_text.strip())
            if numerator <= 0 or denominator <= 0:
                raise OddsError(f"non-positive fraction: {text!r}")
            return _ONE + numerator / denominator
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise OddsError(f"unparseable odds value: {text!r}") from exc
    if "." in cleaned:
        # Decimal (total return) odds, e.g. "1.85".
        if value <= _ONE:
            raise OddsError(f"decimal odds must exceed 1: {text!r}")
        return value
    # Bare integer without a slash: fractional profit ("1" = evens, "2" = 2/1).
    if value <= 0:
        raise OddsError(f"non-positive odds value: {text!r}")
    return _ONE + value


def decimal_odds_to_american(decimal_odds: Decimal) -> int:
    """Total-return decimal odds -> American int (abs >= 100 guaranteed)."""
    if decimal_odds <= _ONE:
        raise OddsError(f"decimal odds must exceed 1; got {decimal_odds}")
    profit = decimal_odds - _ONE
    if decimal_odds >= Decimal(2):
        raw = profit * _HUNDRED
    else:
        raw = -_HUNDRED / profit
    price = int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if -100 < price < 100:
        # Rounding landed in the illegal gap (decimal odds just under 2.00).
        price = 100 if price >= 0 else -100
    return validate_american(price)


@dataclass(frozen=True)
class SofaOdds:
    """The two-way winner prices extracted from one odds payload."""

    home_price: int   # American
    away_price: int   # American
    market_name: str
    is_live: bool


def parse_odds_payload(payload: Any) -> SofaOdds | None:
    """Extract the pre-match match-winner prices, or None when the payload
    carries no usable two-way market (some low-tier matches have no odds).
    Malformed structures raise OddsError — a wrong shape must not be
    silently priced."""
    if not isinstance(payload, Mapping):
        raise OddsError("odds payload is not an object")
    markets = payload.get("markets")
    if markets is None:
        return None
    if not isinstance(markets, list):
        raise OddsError("odds payload 'markets' is not an array")
    for market in markets:
        if not isinstance(market, Mapping):
            continue
        name = str(market.get("marketName", "")).strip().lower()
        if not any(candidate in name for candidate in _WINNER_MARKETS):
            continue
        choices = market.get("choices")
        if not isinstance(choices, list):
            continue
        prices: dict[str, int] = {}
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            label = str(choice.get("name", "")).strip()
            raw = choice.get("fractionalValue") or choice.get("value")
            if label in ("1", "2") and isinstance(raw, str):
                prices[label] = decimal_odds_to_american(
                    fractional_to_decimal_odds(raw))
        if set(prices) == {"1", "2"}:
            return SofaOdds(home_price=prices["1"], away_price=prices["2"],
                            market_name=str(market.get("marketName", "")),
                            is_live=bool(market.get("isLive", False)))
    return None
