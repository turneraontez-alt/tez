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
from typing import Any, Mapping

logger = logging.getLogger("q15.executor.client")

try:  # requests is already a project dep (used by the read-only client)
    import requests
except Exception:  # pragma: no cover - import guard
    requests = None  # type: ignore


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
            logger.error("kalshi %s %s failed: %s", method, suffix, exc)
            return {"ok": False, "error": f"request_failed: {exc.__class__.__name__}"}
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

    # -- write (the live-money path) ---------------------------------------------
    def place_order(self, *, ticker: str, side: str, count: int, price_cents: int,
                    action: str = "buy", client_order_id: str) -> dict[str, Any]:
        """Place (dry-run: LOG) a limit order. ``side`` 'no'/'yes'; ``action`` buy/sell."""
        side_l = (side or "").lower()
        price_field = "no_price" if side_l == "no" else "yes_price"
        body = {
            "ticker": ticker,
            "action": action,
            "side": side_l,
            "count": int(count),
            "type": "limit",
            price_field: int(price_cents),
            "client_order_id": client_order_id,
        }
        # Defence in depth: the dry-run / disabled / kill paths NEVER touch the network.
        ready, why = self.live_ready
        if not ready:
            logger.info("[DRY-RUN/%s] would place: %s", why, body)
            return {"ok": True, "dry_run": True, "reason": why, "would_place": body}
        logger.warning("[LIVE] placing order: %s", body)
        return self._request("POST", "/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        ready, why = self.live_ready
        if not ready:
            logger.info("[DRY-RUN/%s] would cancel order %s", why, order_id)
            return {"ok": True, "dry_run": True, "reason": why}
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
