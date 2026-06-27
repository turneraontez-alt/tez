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


def _canon(ticker, secs=600.0, close=1600.0):
    return SimpleNamespace(ticker=ticker, seconds_remaining=secs, settlement_time=close)


def _analysis(side, bid, ask, yes_prob):
    return {
        "prediction_available": True,
        "prediction_side": side,
        "yes_probability": yes_prob,
        "model_yes_probability": yes_prob,
        "raw_yes_probability": yes_prob,
        "conservative_probability": max(yes_prob, 1.0 - yes_prob),
        "data_quality": 0.90,
        "quote": {
            "bid_cents": bid,
            "ask_cents": ask,
            "spread_cents": ask - bid,
            "depth_contracts": 25,
        },
    }


def _cand(asset, side, bid, ask, yes_prob, ticker=None, secs=600.0, close=1600.0):
    return extract_candidate(
        asset,
        _analysis(side, bid, ask, yes_prob),
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
    assert "Paper-only: tracking performance, no trade placed" in r.telegram.sent[0]


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


def test_btc_follow_applies_to_xrp_but_not_bnb(tmp_path):
    r = _runner(tmp_path)
    btc = _cand("BTC", "NO", 82, 84, 0.15, ticker="T-BTC")
    xrp = _cand("XRP", "YES", 24, 25, 0.50, ticker="T-XRP")
    bnb = _cand("BNB", "YES", 24, 25, 0.50, ticker="T-BNB")

    r._observe_sync(candidates=[btc, xrp, bnb], now=1000.0)

    rows = r.ledger.rows(r.config.model_version)
    assert len(rows) == 1
    assert rows[0]["asset"] == "XRP"
    assert rows[0]["rule_code"] == "HVF_BTC_FOLLOW_EXTREME"
    assert rows[0]["predicted_outcome"] == "NO"


def test_doge_is_excluded_from_clean_hvf_default_assets(tmp_path):
    r = _runner(tmp_path)
    doge = _cand("DOGE", "YES", 82, 84, 0.85, ticker="T-DOGE")

    r._observe_sync(candidates=[doge], now=1000.0)

    assert r.ledger.rows(r.config.model_version) == []


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
