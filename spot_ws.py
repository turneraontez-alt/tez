"""Optional low-latency spot feed over public exchange WebSockets.

Default OFF. When ``Q15_SPOT_WS_ENABLED`` is truthy, a background thread keeps a
real-time ticker subscription open to Coinbase (USD pairs) and OKX (USDT pairs)
and caches the latest tick per asset. ``spot_client.get_spot()`` prefers a fresh
tick from here and transparently falls back to the existing REST path whenever
the feed is disabled, disconnected, or the tick is older than
``Q15_SPOT_WS_MAX_AGE_SECONDS``.

READ-ONLY market data: this subscribes to public ticker channels only. It never
authenticates and never sends an order — same invariant as the rest of the app.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

try:
    import websockets
    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False

# Symbol map lives in spot_client; importing it here is safe because spot_client
# only imports this module lazily (inside get_spot), so there is no cycle.
from spot_client import SPOT_SOURCES  # asset -> (provider, symbol, quote)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"


def _enabled() -> bool:
    return str(os.environ.get("Q15_SPOT_WS_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}


def _max_age() -> float:
    try:
        return max(0.5, float(os.environ.get("Q15_SPOT_WS_MAX_AGE_SECONDS", "3") or 3))
    except (TypeError, ValueError):
        return 3.0


class SpotWebSocketFeed:
    """Background websocket subscriber that caches the latest tick per asset."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ticks: dict[str, dict] = {}  # asset -> {price, bid, ask, ts, source}
        self._connected = {"coinbase": False, "okx": False}
        self._last_error: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # symbol -> asset, grouped by provider
        self._coinbase = {sym: a for a, (src, sym, _q) in SPOT_SOURCES.items() if src == "coinbase"}
        self._okx = {sym: a for a, (src, sym, _q) in SPOT_SOURCES.items() if src == "okx"}

    def start(self) -> None:
        if not _HAVE_WS:
            logger.warning("Spot websocket disabled: `pip install websockets`")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="spot-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_tick(self, asset: str, max_age: float | None = None) -> dict | None:
        max_age = _max_age() if max_age is None else max_age
        now = time.time()
        with self._lock:
            tick = self._ticks.get(asset)
        if not tick:
            return None
        if now - tick["ts"] > max_age:
            return None
        return dict(tick)

    def health(self) -> dict:
        now = time.time()
        with self._lock:
            ages = {a: round(now - t["ts"], 3) for a, t in self._ticks.items()}
            return {
                "connected": dict(self._connected),
                "tick_age_seconds": ages,
                "last_error": dict(self._last_error),
            }

    # --- networking ---------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread guard
            logger.warning("Spot websocket thread exited: %s", exc)

    async def _run(self) -> None:
        tasks = []
        if self._coinbase:
            tasks.append(self._provider_loop("coinbase", COINBASE_WS, self._subscribe_coinbase, self._handle_coinbase))
        if self._okx:
            tasks.append(self._provider_loop("okx", OKX_WS, self._subscribe_okx, self._handle_okx))
        if tasks:
            await asyncio.gather(*tasks)

    async def _provider_loop(self, name, url, subscribe, handle) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=1000
                ) as socket:
                    with self._lock:
                        self._connected[name] = True
                        self._last_error.pop(name, None)
                    await subscribe(socket)
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=15.0)
                        except asyncio.TimeoutError:
                            continue
                        handle(message)
            except Exception as exc:
                with self._lock:
                    self._last_error[name] = str(exc)[:200]
                logger.warning("Spot websocket %s reconnecting after error: %s", name, exc)
            finally:
                with self._lock:
                    self._connected[name] = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    async def _subscribe_coinbase(self, socket) -> None:
        await socket.send(json.dumps({
            "type": "subscribe",
            "product_ids": sorted(self._coinbase.keys()),
            "channels": ["ticker"],
        }))

    async def _subscribe_okx(self, socket) -> None:
        args = [{"channel": "tickers", "instId": sym} for sym in sorted(self._okx.keys())]
        await socket.send(json.dumps({"op": "subscribe", "args": args}))

    def _handle_coinbase(self, raw) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("type") != "ticker":
            return
        asset = self._coinbase.get(data.get("product_id"))
        if asset:
            self._store(asset, data.get("price"), data.get("best_bid"), data.get("best_ask"),
                        f"Coinbase WS {data.get('product_id')}")

    def _handle_okx(self, raw) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        arg = data.get("arg") or {}
        for row in data.get("data") or []:
            inst = arg.get("instId") or row.get("instId")
            asset = self._okx.get(inst)
            if asset:
                self._store(asset, row.get("last"), row.get("bidPx"), row.get("askPx"), f"OKX WS {inst}")

    def _store(self, asset, price, bid, ask, source) -> None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        tick = {"price": price, "bid": _f(bid), "ask": _f(ask), "ts": time.time(), "source": source}
        with self._lock:
            self._ticks[asset] = tick


_feed: SpotWebSocketFeed | None = None
_feed_lock = threading.Lock()


def get_feed() -> SpotWebSocketFeed:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = SpotWebSocketFeed()
            _feed.start()
        return _feed


def get_ws_spot(asset: str, max_age: float | None = None) -> dict | None:
    """Return a fresh websocket tick in ``get_spot()`` shape, or ``None``.

    Returns ``None`` unless ``Q15_SPOT_WS_ENABLED`` is set, so callers fall back
    to the REST path by default. Never raises.
    """
    if not _enabled():
        return None
    try:
        tick = get_feed().get_tick(asset, max_age=max_age)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("spot ws lookup failed for %s: %s", asset, exc)
        return None
    if not tick:
        return None
    _src, _sym, quote = SPOT_SOURCES.get(asset, (None, None, None))
    return {
        "ok": True,
        "price": tick["price"],
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "ts": tick["ts"],
        "source": tick["source"],
        "quote": quote,
        "ws": True,
    }


def spot_ws_health() -> dict:
    """Diagnostic snapshot for ``/api/health`` (no secrets, cheap)."""
    info = {"enabled": _enabled(), "have_ws": _HAVE_WS, "max_age_seconds": _max_age()}
    if not _enabled() or _feed is None:
        return info
    try:
        info.update(_feed.health())
    except Exception as exc:  # pragma: no cover - defensive
        info["error"] = str(exc)[:200]
    return info
