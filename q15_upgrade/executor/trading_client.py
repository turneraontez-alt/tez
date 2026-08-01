"""Ultoim V2 EXECUTOR — Kalshi trading client (the ONLY place that can place a live order).

Uses the existing ``KalshiSigner`` (RSA-PSS, kalshi_auth.py) to authenticate POST/DELETE
to the Kalshi v2 portfolio endpoints. In ``dry_run`` (the default) ``place_order`` LOGS the
order it WOULD send and returns a simulated ack — NO network call, NO money. Going live
requires (a) cfg.enabled, (b) cfg.dry_run=False, (c) a working signer (KALSHI_API_KEY_ID +
KALSHI_PRIVATE_KEY), and (d) the kill switch off — all checked here, defence in depth.

Idempotency: every order carries a deterministic ``client_order_id`` so a retry can't
double-fill. All prices INTEGER CENTS. Narrow exceptions only.
"""
from __future__ import annotations

import logging
import urllib.parse
import uuid
from typing import Any, Mapping

logger = logging.getLogger("q15.executor.client")

try:  # requests is already a project dep (used by the read-only client)
    import requests
except Exception:  # pragma: no cover - import guard
    requests = None  # type: ignore

# Fixed namespace -> deterministic UUID from our client_order_id string, so retries of the
# same logical order carry the same id (Kalshi V2 requires a UUID client_order_id; idempotent).
_COID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _coid_uuid(raw: str) -> str:
    return str(uuid.uuid5(_COID_NAMESPACE, raw))


def _v2_side_price(side: str, action: str, price_cents: int) -> tuple[str, str]:
    """Map (yes/no, buy/sell, cents) -> Kalshi V2 (side 'bid'/'ask', dollar-string price).

    The V2 book is YES-denominated: a 'bid' buys YES, an 'ask' sells YES. Binary duality:
        buy NO  @ q  ==  sell YES @ (100-q)
        sell NO @ q  ==  buy  YES @ (100-q)
    so a NO trade is sent as the opposite YES side at the complementary price.

    *** DIRECTION ASSUMPTION — CONFIRM against the docs' `side` field semantics before any
        LIVE money: a wrong mapping = the exact opposite position. Dry-run/probe never risk
        this (they place $0.01 unfillable orders). ***
    """
    s = (side or "").lower()
    a = (action or "").lower()
    if s == "yes":
        v2_side = "bid" if a == "buy" else "ask"
        p = price_cents
    else:  # NO -> trade YES at the complementary price
        v2_side = "ask" if a == "buy" else "bid"
        p = 100 - price_cents
    p = max(1, min(99, int(p)))
    return v2_side, f"{p / 100:.4f}"


class KalshiTradingClient:
    def __init__(self, cfg, signer: Any | None = None, session: Any | None = None):
        self.cfg = cfg
        if signer is None:
            from kalshi_auth import KalshiSigner  # local import: only when executor runs
            signer = KalshiSigner()
        self.signer = signer
        self._session = session or (requests.Session() if requests is not None else None)

    @property
    def live_ready(self) -> tuple[bool, str]:
        """Can this client send a REAL order right now? (and why not)."""
        if not self.cfg.enabled:
            return False, "executor disabled"
        if self.cfg.kill_switch:
            return False, "kill switch on"
        if self.cfg.dry_run:
            return False, "dry-run"
        if self._session is None:
            return False, "requests unavailable"
        if not getattr(self.signer, "available", False):
            return False, f"signer unavailable: {getattr(self.signer, 'error', '?')}"
        return True, "ready"

    # -- low-level signed request -------------------------------------------------
    def _request(self, method: str, suffix: str, body: Mapping[str, Any] | None = None,
                 timeout: tuple = (3.05, 6)) -> dict[str, Any]:
        url = self.cfg.base_url.rstrip("/") + suffix
        sign_path = urllib.parse.urlparse(url).path  # path WITHOUT query, per the signer
        headers = self.signer.sign(method, sign_path)
        headers["Content-Type"] = "application/json"
        try:
            resp = self._session.request(method, url, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as exc:  # type: ignore[union-attr]
            # A transport failure is NOT proof the order was rejected. Only a
            # ConnectTimeout guarantees nothing was ever sent; a ReadTimeout or a
            # mid-flight ConnectionError can mean Kalshi accepted and filled the
            # order while we never saw the ack. Flag that ambiguity so the caller
            # books the exposure instead of silently freeing the risk budget.
            certain_not_sent = isinstance(exc, requests.exceptions.ConnectTimeout)
            logger.error("kalshi %s %s failed: %s (uncertain=%s)",
                         method, suffix, exc, not certain_not_sent)
            return {"ok": False, "uncertain": not certain_not_sent,
                    "error": f"request_failed: {exc.__class__.__name__}"}
        if resp.status_code >= 400:
            logger.error("kalshi %s %s -> %s: %s", method, suffix, resp.status_code, resp.text[:300])
            return {"ok": False, "status": resp.status_code, "error": resp.text[:300]}
        try:
            return {"ok": True, "status": resp.status_code, "data": resp.json()}
        except ValueError:
            return {"ok": True, "status": resp.status_code, "data": {}}

    # -- read --------------------------------------------------------------------
    def get_balance_cents(self) -> int | None:
        r = self._request("GET", "/portfolio/balance")
        if r.get("ok"):
            bal = (r.get("data") or {}).get("balance")
            return int(bal) if bal is not None else None
        return None

    def get_positions(self) -> list[dict[str, Any]]:
        r = self._request("GET", "/portfolio/positions")
        if r.get("ok"):
            return (r.get("data") or {}).get("market_positions") or []
        return []

    def get_orders(self) -> list[dict[str, Any]]:
        """Read account orders for final fill reconciliation."""
        r = self._request("GET", "/portfolio/orders")
        if r.get("ok"):
            return (r.get("data") or {}).get("orders") or []
        return []

    # -- write (the live-money path) — Kalshi V2 events/orders schema -------------
    def place_order(self, *, ticker: str, side: str, count: int, price_cents: int,
                    action: str = "buy", client_order_id: str) -> dict[str, Any]:
        """Place (dry-run: LOG) a limit order on the V2 endpoint. ``side`` 'no'/'yes';
        ``action`` buy/sell. Maps to the V2 single-book bid/ask + dollar-string schema."""
        v2_side, price_str = _v2_side_price(side, action, int(price_cents))
        # A sell is always a defensive CLOSE: reduce_only so it can never flip us short.
        # Kalshi REJECTS a reduce_only order that is good-till-canceled (HTTP 400
        # "reduce_only can only be used with IoC orders"), which silently killed every
        # defensive exit. Send the close immediate-or-cancel: it is the right semantics for a
        # close anyway (take the liquidity resting now, never leave a sell working into
        # expiry), and reduce_only caps the count to the position actually held. Entries
        # (buys) stay good-till-canceled so a near-miss limit can still fill in the window.
        is_reduce = (action or "").lower() == "sell"
        body = {
            "ticker": ticker,
            "client_order_id": _coid_uuid(client_order_id),  # deterministic UUID (idempotent)
            "side": v2_side,
            "count": f"{int(count):.2f}",                    # string quantity, e.g. "10.00"
            "price": price_str,                              # fixed-point dollars, e.g. "0.6500"
            "time_in_force": "immediate_or_cancel" if is_reduce else "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",  # required by the V2 endpoint
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": is_reduce,                         # a close only ever reduces
            "subaccount": 0,
            "exchange_index": 0,
        }
        # Defence in depth: the dry-run / disabled / kill paths NEVER touch the network.
        ready, why = self.live_ready
        if not ready:
            logger.info("[DRY-RUN/%s] would place (v2): %s", why, body)
            return {"ok": True, "dry_run": True, "reason": why, "would_place": body}
        logger.warning("[LIVE] placing v2 order: %s", body)
        return self._request("POST", "/portfolio/events/orders", body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        ready, why = self.live_ready
        if not ready:
            logger.info("[DRY-RUN/%s] would cancel order %s", why, order_id)
            return {"ok": True, "dry_run": True, "reason": why}
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")
