"""Incremental multi-timeframe OHLCV candle engine.

Builds candles for two sources per asset:
  - "yes": the Kalshi YES contract, from real public trades (volume + taker
    side are exact).
  - "underlying": the spot price proxy, from polled price ticks (no per-tick
    volume, so volume stays 0 and trade_count counts ticks).

Candles are bucketed by time and updated incrementally; old buckets are
evicted past the retention window (>= 15 minutes per spec, kept a bit longer
to survive market rollover analysis).
"""

TIMEFRAMES = [1, 5, 15, 30, 60]
RETENTION_SECONDS = 1000  # ~16.5 min


def _new_candle(bucket, tf, price):
    return {
        "open": price, "high": price, "low": price, "close": price,
        "volume": 0.0, "trade_count": 0,
        "aggressive_yes_volume": 0.0, "aggressive_no_volume": 0.0,
        "start_time": bucket, "end_time": bucket + tf,
    }


class CandleBook:
    """Holds candles keyed by (source, timeframe) -> {bucket_start: candle}."""

    def __init__(self, timeframes=TIMEFRAMES):
        self.timeframes = timeframes
        self.books = {}  # (source, tf) -> dict[bucket_start] = candle

    def add(self, source, ts, price, volume=0.0, taker_side=None):
        # Q15_V5_CANDLE_ACCOUNTING: count the first event in each new bucket.
        if ts is None or price is None:
            return
        try:
            ts = float(ts)
            price = float(price)
            volume = float(volume or 0.0)
        except (TypeError, ValueError):
            return

        for tf in self.timeframes:
            key = (source, tf)
            book = self.books.setdefault(key, {})
            bucket = int(ts // tf) * tf
            candle = book.get(bucket)
            if candle is None:
                candle = _new_candle(bucket, tf, price)
                book[bucket] = candle

            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += volume
            candle["trade_count"] += 1
            if taker_side == "yes":
                candle["aggressive_yes_volume"] += volume
            elif taker_side == "no":
                candle["aggressive_no_volume"] += volume

    def evict(self, now):
        cutoff = now - RETENTION_SECONDS
        for key, book in self.books.items():
            stale = [b for b, c in book.items() if c["end_time"] < cutoff]
            for b in stale:
                del book[b]

    def get(self, source, tf, n=None, include_current=True, now=None):
        """Return candles for a source/timeframe, oldest-first.

        If include_current is False, the still-forming bucket (containing now)
        is excluded so callers can analyze only completed candles.
        """
        book = self.books.get((source, tf), {})
        items = sorted(book.values(), key=lambda c: c["start_time"])
        if not include_current and now is not None:
            cur_bucket = int(now // tf) * tf
            items = [c for c in items if c["start_time"] != cur_bucket]
        if n is not None:
            items = items[-n:]
        return [dict(c) for c in items]

    def latest_completed(self, source, tf, now):
        cur_bucket = int(now // tf) * tf
        book = self.books.get((source, tf), {})
        completed = [c for c in book.values() if c["start_time"] < cur_bucket]
        if not completed:
            return None
        return dict(max(completed, key=lambda c: c["start_time"]))
