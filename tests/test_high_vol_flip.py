from types import SimpleNamespace

from q15_upgrade.high_vol_flip.config import HighVolFlipConfig
from q15_upgrade.high_vol_flip.ledger import HighVolFlipLedger, kalshi_fee_cents
from q15_upgrade.high_vol_flip.runner import HighVolFlipRunner
from q15_upgrade.high_vol_flip.rules import extract_candidate


class _StubTelegram:
    def __init__(self):
        self.sent = []

    def status(self):
        return "configured"

    def send(self, text):
        self.sent.append(text)
        return {"ok": True, "delivered": True, "muted": False,
                "message_id": len(self.sent), "error": None}


def _cfg(tmp_path, **overrides):
    data = {
        "enabled": True,
        "telegram_enabled": True,
        "alert_telegram_enabled": True,
        "telegram_chat_id": "test-chat",
        "db_path": str(tmp_path / "hvf.sqlite3"),
        "mark_band_seconds": 25.0,
        "reconcile_every_seconds": 0.0,
    }
    data.update(overrides)
    return HighVolFlipConfig(**data)


def _runner(tmp_path, **overrides):
    r = HighVolFlipRunner(_cfg(tmp_path, **overrides))
    r.telegram = _StubTelegram()
    return r


def test_hvf_telegram_falls_back_to_current_chat(monkeypatch):
    monkeypatch.delenv("Q15_HVF_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("Q15_ULTOIM_V2_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "current-room")

    cfg = HighVolFlipConfig.from_env()

    assert cfg.telegram_chat_id == "current-room"


def _canon(ticker, secs=600.0, close=1600.0):
    return SimpleNamespace(ticker=ticker, seconds_remaining=secs, settlement_time=close)


def _analysis(side, bid, ask, yes_prob, quote_extra=None):
    quote = {
        "bid_cents": bid,
        "ask_cents": ask,
        "spread_cents": ask - bid,
        "depth_contracts": 25,
    }
    if quote_extra:
        quote.update(quote_extra)
    return {
        "prediction_available": True,
        "prediction_side": side,
        "yes_probability": yes_prob,
        "model_yes_probability": yes_prob,
        "raw_yes_probability": yes_prob,
        "conservative_probability": max(yes_prob, 1.0 - yes_prob),
        "data_quality": 0.90,
        "quote": quote,
    }


def _cand(asset, side, bid, ask, yes_prob, ticker=None, secs=600.0, close=1600.0,
          quote_extra=None):
    return extract_candidate(
        asset,
        _analysis(side, bid, ask, yes_prob, quote_extra=quote_extra),
        _canon(ticker or f"T-{asset}", secs=secs, close=close),
    )


def test_selected_side_no_quote_reconstructs_true_yes_no_prices():
    cand = _cand("XRP", "NO", 72, 75, 0.30)

    assert cand["no_bid_cents"] == 72
    assert cand["no_ask_cents"] == 75
    assert cand["yes_bid_cents"] == 25
    assert cand["yes_ask_cents"] == 28
    assert cand["dominant_side"] == "NO"


def test_hype_bullish_flash_sends_one_paper_alert_per_window(tmp_path):
    r = _runner(tmp_path)
    now = 1000.0
    hype = _cand("HYPE", "YES", 72, 75, 0.68)

    r._observe_sync(candidates=[hype], now=now)
    r._observe_sync(candidates=[hype], now=now + 1)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_HYPE_BULLISH_FLASH"
    assert rows[0]["predicted_outcome"] == "YES"
    assert rows[0]["delivery_status"] == "SENT"
    assert "HIGH VOLATILITY FLIP" in r.telegram.sent[0]
    assert "<pre>" in r.telegram.sent[0]
    assert "Depth ratio: missing" in r.telegram.sent[0]
    assert "Paper-only: tracking performance, no trade placed" in r.telegram.sent[0]


def test_old_hvf_telegram_switch_does_not_send_without_alert_opt_in(tmp_path):
    r = _runner(tmp_path, alert_telegram_enabled=False)
    hype = _cand("HYPE", "YES", 72, 75, 0.68)

    r._observe_sync(candidates=[hype], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_HYPE_BULLISH_FLASH"
    assert rows[0]["delivery_status"] == "MUTED"
    assert r.telegram.sent == []


def test_v3_can_own_bnb_notifications_without_disabling_hvf_tracking(tmp_path):
    r = _runner(tmp_path, suppress_bnb_telegram_for_v3=True)
    row = {
        "created_at": 1000.0,
        "model_version": r.config.model_version,
        "record_kind": "HIGH_VOL_FLIP_ALERT",
        "asset": "BNB",
        "ticker": "T-BNB",
        "interval": "10M",
        "window_key": 1,
        "close_time": 1600.0,
        "seconds_remaining": 600.0,
        "predicted_outcome": "NO",
        "model_predicted_side": "NO",
        "rule_code": "HVF_OWN_STRONG_SELECTED",
        "rule_name": "OWN_STRONG_SELECTED",
        "reason_codes": "HVF_OWN_STRONG_SELECTED",
        "selected_side": "NO",
        "selected_ask_cents": 80.0,
        "entry_ask_cents": 80.0,
        "paper_only": True,
    }

    r._record_and_send(row)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["asset"] == "BNB"
    assert rows[0]["delivery_status"] == "MUTED"
    assert rows[0]["delivery_error"] == "v3_bnb_combined_owns_notification"
    assert r.telegram.sent == []


def test_hype_early_bullish_flip_is_opt_in_research_entry(tmp_path):
    r = _runner(tmp_path, early_entry_enabled=True, hype_early_enabled=True)
    hype = _cand("HYPE", "YES", 55, 60, 0.60)

    r._observe_sync(candidates=[hype], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_HYPE_EARLY_BULLISH_FLIP"
    assert rows[0]["selected_bid_cents"] == 55
    assert rows[0]["selected_ask_cents"] == 60


def test_own_early_flip_is_watch_only_by_default(tmp_path):
    r = _runner(tmp_path)
    xrp = _cand("XRP", "NO", 55, 60, 0.42, ticker="T-XRP")

    r._observe_sync(candidates=[xrp], now=1000.0)

    assert r.ledger.rows(r.config.model_version) == []
    watch = r.ledger.watch_rows(r.config.model_version)
    assert len(watch) == 1
    assert watch[0]["record_kind"] == "EARLY_FLIP_WATCH"
    assert watch[0]["rule_code"] == "HVF_OWN_EARLY_FLIP"
    assert watch[0]["predicted_outcome"] == "NO"
    assert watch[0]["entry_ask_cents"] == 60
    assert watch[0]["delivery_status"] == "RECORDED"
    assert r.telegram.sent == []


def test_own_early_flip_watch_grades_without_blocking_later_alert(tmp_path):
    r = _runner(tmp_path)
    close = 1600.0
    early = _cand("XRP", "NO", 55, 60, 0.42, ticker="T-XRP", secs=720, close=close)
    later = _cand("XRP", "NO", 72, 75, 0.30, ticker="T-XRP", secs=600, close=close)

    r._observe_sync(candidates=[early], now=1000.0)
    r._observe_sync(candidates=[later], now=1120.0)

    alerts = r.ledger.rows(r.config.model_version)
    watch = r.ledger.watch_rows(r.config.model_version)
    assert len(watch) == 1
    assert len(alerts) == 1
    assert alerts[0]["rule_code"] == "HVF_OWN_NO_FLASH"
    assert alerts[0]["delivery_status"] == "SENT"

    assert r.ledger.resolve(r.config.model_version, "T-XRP", "NO", now=1700.0) == 2
    watch = r.ledger.watch_rows(r.config.model_version)
    assert watch[0]["correct"] == 1
    sb = r.ledger.scoreboard(r.config.model_version)
    assert sb["total_alerts"] == 1
    assert sb["early_watch"]["total_records"] == 1
    assert sb["early_watch"]["resolved"] == 1
    assert sb["early_watch"]["overall"]["wins"] == 1


def test_own_early_flip_can_be_enabled_for_research_entry(tmp_path):
    r = _runner(tmp_path, early_entry_enabled=True)
    xrp = _cand("XRP", "NO", 55, 60, 0.42, ticker="T-XRP")

    r._observe_sync(candidates=[xrp], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_OWN_EARLY_FLIP"
    assert rows[0]["predicted_outcome"] == "NO"
    assert rows[0]["entry_ask_cents"] == 60


def test_btc_early_follow_lag_catches_alt_before_70s(tmp_path):
    r = _runner(tmp_path, early_entry_enabled=True, btc_early_follow_enabled=True)
    btc = _cand("BTC", "NO", 76, 78, 0.20, ticker="T-BTC")
    xrp = _cand("XRP", "YES", 40, 45, 0.50, ticker="T-XRP")

    r._observe_sync(candidates=[btc, xrp], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_BTC_EARLY_FOLLOW_LAG"
    assert rows[0]["predicted_outcome"] == "NO"
    assert rows[0]["selected_bid_cents"] == 55
    assert rows[0]["selected_ask_cents"] == 60


def test_default_early_tuning_blocks_noisy_hype_and_btc_lag_rules(tmp_path):
    r = _runner(tmp_path)
    btc = _cand("BTC", "NO", 76, 78, 0.20, ticker="T-BTC")
    hype = _cand("HYPE", "YES", 55, 60, 0.60, ticker="T-HYPE")
    doge = _cand("DOGE", "YES", 40, 45, 0.50, ticker="T-DOGE")

    r._observe_sync(candidates=[btc, hype, doge], now=1000.0)

    assert r.ledger.rows(r.config.model_version) == []


def test_more_fire_strict_uses_checkpoint_jump_depth_and_excludes_bnb_hype(tmp_path):
    r = _runner(
        tmp_path,
        assets=frozenset({"SOL", "BNB", "HYPE"}),
        intervals=frozenset({"9M"}),
        more_fire_strict_enabled=True,
        more_fire_strict_assets=frozenset({"SOL", "BNB", "HYPE"}),
        more_fire_strict_intervals=frozenset({"12M"}),
        max_alerts_per_window=2,
    )
    close = 1800.0
    sol_prev = _cand(
        "SOL", "YES", 60, 63, 0.58, ticker="T-SOL", secs=780, close=close,
        quote_extra={"yes_bid_depth_contracts": 150, "yes_ask_depth_contracts": 40},
    )
    bnb_prev = _cand("BNB", "YES", 60, 63, 0.58, ticker="T-BNB", secs=780, close=close)
    hype_prev = _cand("HYPE", "YES", 60, 63, 0.58, ticker="T-HYPE", secs=780, close=close)
    r._observe_sync(candidates=[sol_prev, bnb_prev, hype_prev], now=1000.0)

    sol_now = _cand(
        "SOL", "YES", 66, 69, 0.62, ticker="T-SOL", secs=720, close=close,
        quote_extra={
            "yes_bid_depth_contracts": 150,
            "yes_ask_depth_contracts": 40,
            "kalshi_depth_status": "ok",
            "kalshi_depth_missing_reason": None,
            "kalshi_depth_retry_used": True,
            "kalshi_taker_yes_volume_15s": 12.0,
            "kalshi_taker_no_volume_15s": 3.0,
            "kalshi_taker_net_yes_volume_15s": 9.0,
            "spot_depth_status": "ok",
            "spot_depth_missing_reason": None,
            "spot_depth_source": "OKX SOL-USDT",
            "spot_depth_age_seconds": 3.0,
            "spot_depth_bid_depth_levels": 900.0,
            "spot_depth_ask_depth_levels": 300.0,
            "spot_depth_imbalance": 0.5,
            "spot_depth_trade_buy_qty_5s": 1.0,
            "spot_depth_trade_sell_qty_5s": 0.25,
            "spot_depth_trade_net_qty_5s": 0.75,
            "spot_depth_trade_net_qty_15s": 5.0,
            "spot_depth_trade_buy_qty_60s": 12.0,
            "spot_depth_trade_sell_qty_60s": 4.0,
            "spot_depth_trade_net_qty_60s": 8.0,
            "spot_depth_last_trade_side": "buy",
            "spot_depth_last_trade_size": 0.5,
        },
    )
    bnb_now = _cand("BNB", "YES", 66, 69, 0.62, ticker="T-BNB", secs=720, close=close)
    hype_now = _cand("HYPE", "YES", 66, 69, 0.62, ticker="T-HYPE", secs=720, close=close)
    r._observe_sync(candidates=[sol_now, bnb_now, hype_now], now=1060.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["asset"] == "SOL"
    assert rows[0]["rule_code"] == "HVF_MORE_FIRE_STRICT"
    assert rows[0]["record_kind"] == "MORE_FIRE_STRICT_ALERT"
    assert rows[0]["previous_interval"] == "13M"
    assert rows[0]["selected_mid_jump_cents"] == 6.0
    assert rows[0]["yes_ask_depth_contracts"] == 40
    assert rows[0]["selected_depth_ratio"] == 3.75
    assert rows[0]["kalshi_depth_status"] == "ok"
    assert rows[0]["kalshi_depth_retry_used"] == 1
    assert rows[0]["kalshi_taker_net_yes_volume_15s"] == 9.0
    assert rows[0]["spot_depth_status"] == "ok"
    assert rows[0]["spot_depth_source"] == "OKX SOL-USDT"
    assert rows[0]["spot_depth_bid_depth_levels"] == 900.0
    assert rows[0]["spot_depth_ask_depth_levels"] == 300.0
    assert rows[0]["spot_depth_imbalance"] == 0.5
    assert rows[0]["spot_depth_trade_net_qty_5s"] == 0.75
    assert rows[0]["spot_depth_trade_net_qty_15s"] == 5.0
    assert rows[0]["spot_depth_trade_net_qty_60s"] == 8.0
    assert rows[0]["spot_depth_last_trade_side"] == "buy"
    assert "MORE-FIRE STRICT" in r.telegram.sent[0]
    assert "Depth ratio: 3.75" in r.telegram.sent[0]


def test_more_fire_strict_requires_yes_depth_ratio(tmp_path):
    r = _runner(
        tmp_path,
        assets=frozenset({"SOL"}),
        intervals=frozenset({"9M"}),
        more_fire_strict_enabled=True,
        more_fire_strict_intervals=frozenset({"12M"}),
    )
    close = 1800.0
    prev = _cand(
        "SOL", "YES", 60, 63, 0.58, ticker="T-SOL", secs=780, close=close,
        quote_extra={"yes_bid_depth_contracts": 31, "yes_ask_depth_contracts": 44},
    )
    now = _cand(
        "SOL", "YES", 66, 69, 0.62, ticker="T-SOL", secs=720, close=close,
        quote_extra={"yes_bid_depth_contracts": 31, "yes_ask_depth_contracts": 44},
    )

    r._observe_sync(candidates=[prev], now=1000.0)
    r._observe_sync(candidates=[now], now=1060.0)

    assert r.ledger.rows(r.config.model_version) == []
    assert r.ledger.watch_rows(r.config.model_version) == []


def test_more_fire_strict_requires_prior_checkpoint(tmp_path):
    r = _runner(
        tmp_path,
        assets=frozenset({"SOL"}),
        intervals=frozenset({"9M"}),
        more_fire_strict_enabled=True,
        more_fire_strict_intervals=frozenset({"12M"}),
    )
    sol_now = _cand(
        "SOL", "YES", 66, 69, 0.62, ticker="T-SOL", secs=720, close=1800.0,
        quote_extra={"yes_bid_depth_contracts": 150, "yes_ask_depth_contracts": 40},
    )

    r._observe_sync(candidates=[sol_now], now=1060.0)

    assert r.ledger.rows(r.config.model_version) == []


def test_top_two_window_cap_keeps_board_from_spamming_every_asset(tmp_path):
    r = _runner(tmp_path, early_entry_enabled=True, max_alerts_per_window=2)
    cands = [
        _cand("XRP", "NO", 56, 60, 0.41, ticker="T-XRP"),
        _cand("ETH", "NO", 55, 60, 0.42, ticker="T-ETH"),
        _cand("SOL", "NO", 54, 60, 0.43, ticker="T-SOL"),
    ]

    r._observe_sync(candidates=cands, now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 2
    assert {row["rule_code"] for row in rows} == {"HVF_OWN_EARLY_FLIP"}


def test_first_confirmed_rule_wins_over_stacked_rules(tmp_path):
    r = _runner(tmp_path)
    btc = _cand("BTC", "NO", 82, 84, 0.15, ticker="T-BTC")
    xrp = _cand("XRP", "NO", 72, 75, 0.30, ticker="T-XRP")

    r._observe_sync(candidates=[btc, xrp], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["asset"] == "XRP"
    assert rows[0]["rule_code"] == "HVF_OWN_NO_FLASH"


def test_btc_follow_applies_to_xrp_when_explicitly_enabled_but_not_bnb_or_doge(tmp_path):
    r = _runner(tmp_path, btc_follow_enabled=True, assets=frozenset({"XRP", "BNB", "DOGE"}))
    btc = _cand("BTC", "NO", 82, 84, 0.15, ticker="T-BTC")
    xrp = _cand("XRP", "YES", 24, 25, 0.50, ticker="T-XRP")
    bnb = _cand("BNB", "YES", 24, 25, 0.50, ticker="T-BNB")
    doge = _cand("DOGE", "YES", 24, 25, 0.50, ticker="T-DOGE")

    r._observe_sync(candidates=[btc, xrp, bnb, doge], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["asset"] == "XRP"
    assert rows[0]["rule_code"] == "HVF_BTC_FOLLOW_EXTREME"
    assert rows[0]["predicted_outcome"] == "NO"


def test_doge_is_excluded_from_clean_hvf_default_assets(tmp_path):
    r = _runner(tmp_path, assets=frozenset({"DOGE"}))
    doge = _cand("DOGE", "YES", 82, 84, 0.85, ticker="T-DOGE")

    r._observe_sync(candidates=[doge], now=1000.0)

    assert r.ledger.rows(r.config.model_version) == []


def test_depth_veto_blocks_known_bad_depth_but_allows_missing_depth(tmp_path):
    r = _runner(tmp_path, assets=frozenset({"SOL"}), max_alerts_per_window=2)
    bad_depth = _cand(
        "SOL", "YES", 82, 84, 0.85, ticker="T-SOL-BAD", close=1600.0,
        quote_extra={"yes_bid_depth_contracts": 5, "yes_ask_depth_contracts": 100},
    )
    missing_depth = _cand(
        "SOL", "YES", 82, 84, 0.85, ticker="T-SOL-MISS", close=2500.0,
    )

    r._observe_sync(candidates=[bad_depth], now=1000.0)
    assert r.ledger.rows(r.config.model_version) == []

    r._observe_sync(candidates=[missing_depth], now=1900.0)
    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["rule_code"] == "HVF_OWN_STRONG_SELECTED"
    assert rows[0]["selected_depth_ratio"] is None


def test_fee_aware_grading_and_scoreboard(tmp_path):
    led = HighVolFlipLedger(str(tmp_path / "ledger.sqlite3"))
    led.record_alert({
        "created_at": 1.0,
        "model_version": "high-vol-flip-v1",
        "asset": "XRP",
        "ticker": "T-XRP",
        "interval": "10M",
        "window_key": 7,
        "predicted_outcome": "YES",
        "rule_code": "HVF_BTC_DIVERGENCE_ACCEL_WATCH",
        "rule_name": "BTC_DIVERGENCE_ACCEL_WATCH",
        "entry_ask_cents": 72.0,
        "entry_fee_cents": kalshi_fee_cents(72.0),
    })

    assert led.resolve("high-vol-flip-v1", "T-XRP", "YES", now=2.0) == 1
    row = led.rows("high-vol-flip-v1")[0]
    assert row["correct"] == 1
    assert row["hypothetical_pnl_cents"] == 100.0 - 72.0 - kalshi_fee_cents(72.0)

    sb = led.scoreboard("high-vol-flip-v1")
    assert sb["overall"]["wins"] == 1
    assert sb["overall"]["net_pnl_cents"] == row["hypothetical_pnl_cents"]
    assert sb["by_asset"]["XRP"]["avg_entry_price_cents"] == 72.0
