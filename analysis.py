"""Per-asset engine: ingests raw data, maintains candle/orderbook/trade state,
and builds the full /api/snapshot record with all spec item-9 fields.
"""
import math
import time
import statistics
from collections import deque
from datetime import datetime, timezone

from candles import CandleBook, RETENTION_SECONDS
from orderbook import parse_orderbook
import wick as wick_mod
import patterns as patterns_mod
import confirmation as confirm_mod
import entry as entry_mod
import data_warnings as warn_mod

PRIMARY_TF = 5  # analysis timeframe


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _seconds_remaining(close_time_str):
    if not close_time_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return max(0, int((close_dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return None


def _trim(candles, n):
    return candles[-n:] if len(candles) > n else candles


class AssetEngine:
    def __init__(self, asset):
        self.asset = asset
        self.ticker = None
        self.candles = CandleBook()
        self.ob_tracker = None
        self.seen_trade_ids = set()
        self.trades = deque()
        self.spot_ticks = deque()
        self.last_trade_ts = None
        self.last_update_ts = None
        self.completed_window = None
        self.pending_pattern = None   # follow-through "setup" (prior candle)
        self.cur_pattern = None       # pattern on the current completed candle
        self.cur_bucket = None        # bucket of the current completed candle
        from orderbook import OrderbookTracker
        self.ob_tracker = OrderbookTracker()

    # -- lifecycle -------------------------------------------------------
    def ensure_market(self, ticker):
        if ticker != self.ticker:
            if self.ticker is not None:
                # Preserve a summary of the completed window through rollover.
                self.completed_window = {
                    "ticker": self.ticker,
                    "candles_5s": self.candles.get("yes", 5),
                    "underlying_5s": self.candles.get("underlying", 5),
                    "preserved_at": _iso_now(),
                }
            self.candles = CandleBook()
            from orderbook import OrderbookTracker
            self.ob_tracker = OrderbookTracker()
            self.seen_trade_ids.clear()
            self.trades.clear()
            self.spot_ticks.clear()
            self.last_trade_ts = None
            self.pending_pattern = None
            self.cur_pattern = None
            self.cur_bucket = None
            self.ticker = ticker

    # -- ingest ----------------------------------------------------------
    def ingest_trades(self, new_trades, now):
        fresh = [t for t in new_trades if t.get("id") and t["id"] not in self.seen_trade_ids]
        fresh.sort(key=lambda t: t["ts"])
        for t in fresh:
            self.seen_trade_ids.add(t["id"])
            self.candles.add("yes", t["ts"], t["yes_cents"], t["count"], t.get("taker_side"))
            self.trades.append(t)
            if self.last_trade_ts is None or t["ts"] > self.last_trade_ts:
                self.last_trade_ts = t["ts"]
        cutoff = now - RETENTION_SECONDS
        while self.trades and self.trades[0]["ts"] < cutoff:
            old = self.trades.popleft()
            self.seen_trade_ids.discard(old.get("id"))

    def ingest_spot(self, spot, now):
        if spot and spot.get("ok") and spot.get("price"):
            ts = spot.get("ts", now)
            self.candles.add("underlying", ts, spot["price"], 0.0, None)
            self.spot_ticks.append((ts, spot["price"]))
            cutoff = now - RETENTION_SECONDS
            while self.spot_ticks and self.spot_ticks[0][0] < cutoff:
                self.spot_ticks.popleft()

    # -- derived metrics -------------------------------------------------
    def _flow(self, now, lo, hi):
        """Sum YES/NO taker volume for trades in [now-hi, now-lo)."""
        yes = no = cnt = 0.0
        for t in self.trades:
            age = now - t["ts"]
            if lo <= age < hi:
                cnt += 1
                if t.get("taker_side") == "yes":
                    yes += t["count"]
                elif t.get("taker_side") == "no":
                    no += t["count"]
        return yes, no, cnt

    def _yes_volume(self, now, lo, hi):
        v = 0.0
        for t in self.trades:
            age = now - t["ts"]
            if lo <= age < hi:
                v += t["count"]
        return v

    def _underlying_vol_per_s(self, now, lookback=120):
        candles = self.candles.get("underlying", 1, n=lookback, now=now)
        closes = [c["close"] for c in candles if c["close"] > 0]
        if len(closes) < 10:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 5:
            return None
        try:
            return statistics.pstdev(rets)
        except Exception:
            return None

    def underlying_change_pct(self, now, secs):
        if not self.spot_ticks:
            return None
        cur = self.spot_ticks[-1][1]
        past = None
        for ts, p in reversed(self.spot_ticks):
            if now - ts >= secs:
                past = p
                break
        if past and past > 0:
            return (cur - past) / past * 100
        return None

    def _recent_extremes(self, now, skip=2, window=10):
        candles = self.candles.get("underlying", PRIMARY_TF, n=window + skip, now=now, include_current=False)
        if len(candles) <= skip:
            return None, None
        prior = candles[:-skip] if skip > 0 else candles
        if not prior:
            return None, None
        return max(c["high"] for c in prior), min(c["low"] for c in prior)

    # -- snapshot --------------------------------------------------------
    def build_snapshot(self, asset, market, ob_data, ob_delta, spot, broader,
                       ws_connected, now):
        self.last_update_ts = now
        close_time = market.get("close_time", "")
        secs = _seconds_remaining(close_time)

        # target
        target = market.get("floor_strike")
        try:
            target = float(target) if target is not None else None
        except (TypeError, ValueError):
            target = None
        target_type = market.get("strike_type", "greater_or_equal")

        underlying = spot.get("price") if spot and spot.get("ok") else None
        target_distance_pct = None
        if underlying and target:
            target_distance_pct = round((underlying - target) / target * 100, 4)

        # candles
        yes_1s = self.candles.get("yes", 1, now=now)
        yes_5s = self.candles.get("yes", 5, now=now)
        yes_15s = self.candles.get("yes", 15, now=now)
        u_5s = self.candles.get("underlying", PRIMARY_TF, now=now)

        u_latest = self.candles.latest_completed("underlying", PRIMARY_TF, now)
        u_completed = self.candles.get("underlying", PRIMARY_TF, now=now, include_current=False)
        u_prev = u_completed[-2] if len(u_completed) >= 2 else None
        y_latest = self.candles.latest_completed("yes", PRIMARY_TF, now)

        w = wick_mod.wick_metrics(u_latest, target, target_type) if u_latest else None
        w_prev = wick_mod.wick_metrics(u_prev, target, target_type) if u_prev else None

        yes_flow_15, no_flow_15, cnt_15 = self._flow(now, 0, 15)
        vol_15 = self._yes_volume(now, 0, 15)
        vol_prev_15 = self._yes_volume(now, 15, 30)
        recent_high, recent_low = self._recent_extremes(now)

        ctx = {
            "asset": asset,
            "ticker": self.ticker,
            "target": target,
            "target_type": target_type,
            "underlying": underlying,
            "spot": spot,
            "u_latest": u_latest, "u_prev": u_prev, "y_latest": y_latest,
            "wick": w, "wick_prev": w_prev,
            "taker_yes_15s": yes_flow_15, "taker_no_15s": no_flow_15,
            "trades_15s_count": cnt_15,
            "vol_15s": vol_15, "vol_prev_15s": vol_prev_15,
            "ob": ob_delta or {},
            "spread": ob_data.get("spread"),
            "yes_bid": ob_data.get("yes_bid"), "yes_ask": ob_data.get("yes_ask"),
            "yes_bid_qty": ob_data.get("yes_bid_qty"), "no_bid_qty": ob_data.get("no_bid_qty"),
            "seconds_remaining": secs,
            "underlying_vol_per_s": self._underlying_vol_per_s(now),
            "broader": broader or {},
            "recent_high": recent_high, "recent_low": recent_low,
            "ws_connected": ws_connected,
            "data_age_s": 0.0,
            "last_trade_age_s": (now - self.last_trade_ts) if self.last_trade_ts else None,
        }

        primary, all_patterns = patterns_mod.detect_patterns(ctx)
        direction = primary["direction"] if primary else None

        # Follow-through (spec item 5): a wick/pattern candle never confirms
        # itself. Confirmation requires the *next* completed candle to continue
        # in the prior pattern's direction. We remember the pattern of the
        # current completed candle (cur_pattern) and only promote it to the
        # follow-through "setup" (pending_pattern) when a NEW candle completes.
        # That keeps the confirmation stable for the whole life of the
        # confirming candle instead of flickering on every refresh.
        cur_bucket = u_latest["start_time"] if u_latest else None
        this_pattern = None
        if primary and direction in ("bullish", "bearish") and u_latest is not None:
            this_pattern = {"bucket": cur_bucket, "direction": direction,
                            "close": u_latest["close"]}
        if u_latest is not None and cur_bucket != self.cur_bucket:
            # New completed candle: the prior candle's pattern (may be None)
            # becomes the setup the new candle must follow through on.
            self.pending_pattern = self.cur_pattern
            self.cur_bucket = cur_bucket
            self.cur_pattern = this_pattern
        elif u_latest is not None and this_pattern is not None:
            # Same candle re-evaluated: keep the setup stable, but retain a
            # pattern first seen anywhere within this candle's lifetime.
            self.cur_pattern = this_pattern

        follow_through = False
        pend = self.pending_pattern
        if pend and direction in ("bullish", "bearish") \
                and cur_bucket is not None and cur_bucket > pend["bucket"] \
                and pend["direction"] == direction:
            moved = u_latest["close"] - pend["close"]
            if (direction == "bullish" and moved > 0) or \
               (direction == "bearish" and moved < 0):
                follow_through = True
        ctx["follow_through"] = follow_through

        reasons = confirm_mod.get_confirmations(ctx, direction) if direction else []
        confidence = confirm_mod.confidence_components(ctx, direction, reasons)
        entry = entry_mod.decide_entry(ctx, primary, reasons, confidence)
        warns = warn_mod.data_quality_warnings(ctx)

        # invalidation level (underlying price where the read is wrong)
        invalidation = None
        if direction == "bullish":
            cands = [v for v in (u_latest["low"] if u_latest else None, recent_low) if v is not None]
            invalidation = round(min(cands), 6) if cands else None
        elif direction == "bearish":
            cands = [v for v in (u_latest["high"] if u_latest else None, recent_high) if v is not None]
            invalidation = round(max(cands), 6) if cands else None

        recent_trades = self._recent_trades_out()
        opportunity_score = (vol_15 + abs(yes_flow_15 - no_flow_15) * 2
                             + confidence.get("reversal_confidence", 0))

        flags = []
        if secs is not None and secs < 90:
            flags.append("Closing soon")
        if ctx["spread"] is not None and ctx["spread"] > 8:
            flags.append("Wide spread")
        if ob_data.get("yes_bid") is None:
            flags.append("No order book")

        target_display = f"${target:,.2f}" if target else (market.get("yes_sub_title") or "")

        return {
            # identity / existing
            "asset": asset,
            "ticker": self.ticker,
            "market_state": "live",
            "seconds_remaining": secs,
            "close_time": close_time,
            "target": target_display,
            "yes_bid": ob_data.get("yes_bid"),
            "yes_ask": ob_data.get("yes_ask"),
            "no_bid": ob_data.get("no_bid"),
            "no_ask": ob_data.get("no_ask"),
            "spread": ob_data.get("spread"),
            "volume": market.get("_volume", 0),
            "flags": flags,
            "opportunity_score": round(opportunity_score, 1),
            "last_updated": _iso_now(),
            "cycling": False,
            "error": None,

            # spec item 9 — new fields
            "underlying_current": round(underlying, 6) if underlying else None,
            "underlying_source": spot.get("source") if spot else None,
            "target_value": target,
            "target_distance_pct": target_distance_pct,
            "candles_1s": _trim(yes_1s, 120),
            "candles_5s": _trim(yes_5s, 120),
            "candles_15s": _trim(yes_15s, 60),
            "underlying_candles_5s": _trim(u_5s, 120),
            "latest_wick_metrics": w,
            "pattern_detected": primary["name"] if primary else "none",
            "pattern_direction": direction or "none",
            "pattern_detail": primary["detail"] if primary else None,
            "all_patterns": [p["name"] for p in all_patterns],
            "reversal_confidence": confidence.get("reversal_confidence", 0.0),
            "confidence_components": {k: v for k, v in confidence.items() if k != "reversal_confidence"},
            "confirmation_reasons": reasons,
            "is_confirmed": len(reasons) >= 2,
            "invalidation_level": invalidation,
            "entry_status": entry["entry_status"],
            "entry_side": entry["entry_side"],
            "estimated_fair_probability": entry["estimated_fair_probability"],
            "fair_probability_method": entry["fair_probability_method"],
            "fee_adjusted_break_even": entry["fee_adjusted_break_even"],
            "estimated_edge": entry["estimated_edge"],
            "entry_ask": entry["entry_ask"],
            "entry_fee": entry["entry_fee"],
            "recent_trades": recent_trades,
            "taker_yes_volume_15s": round(yes_flow_15, 2),
            "taker_no_volume_15s": round(no_flow_15, 2),
            "yes_bid_qty": ob_data.get("yes_bid_qty"),
            "yes_ask_qty": ob_data.get("yes_ask_qty"),
            "no_bid_qty": ob_data.get("no_bid_qty"),
            "no_ask_qty": ob_data.get("no_ask_qty"),
            "depth_3c_yes": ob_data.get("depth_3c_yes"),
            "depth_3c_no": ob_data.get("depth_3c_no"),
            "orderbook_imbalance": (ob_delta or {}).get("orderbook_imbalance"),
            "bid_liquidity_added": (ob_delta or {}).get("bid_liquidity_added", 0.0),
            "bid_liquidity_removed": (ob_delta or {}).get("bid_liquidity_removed", 0.0),
            "ask_liquidity_added": (ob_delta or {}).get("ask_liquidity_added", 0.0),
            "ask_liquidity_removed": (ob_delta or {}).get("ask_liquidity_removed", 0.0),
            "data_quality_warnings": warns,

            # backward-compat sparkline (YES 1s closes, last 60s)
            "price_history_60s": self._price_history_60s(now),
            "is_estimate": True,
        }

    def _recent_trades_out(self, n=12):
        trades = list(self.trades)[-40:]
        sizes = [t["count"] for t in trades]
        med = statistics.median(sizes) if len(sizes) >= 5 else 0
        out = []
        for t in reversed(trades[-n:]):
            c = t["count"]
            large = c >= 100 or (med > 0 and c >= 3 * med) or t.get("is_block")
            yc = t["yes_cents"]
            out.append({
                "time": t.get("created_time", ""),
                "ts": t["ts"],
                "contracts": round(c, 2),
                "yes_price": yc,
                "no_price": (100 - yc) if yc is not None else None,
                "taker_side": t.get("taker_side"),
                "is_large": bool(large),
                "is_block": bool(t.get("is_block")),
            })
        return out

    def _price_history_60s(self, now):
        candles = self.candles.get("yes", 1, n=60, now=now)
        return [[round(now - c["start_time"], 1), c["close"]] for c in candles]
