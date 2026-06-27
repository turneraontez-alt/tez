from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from q15_upgrade.checkpoint_v94_unified import CheckpointPolicyV94Unified
from q15_upgrade.checkpoint_v95 import (
    CheckpointPolicyV95,
    READ_ONLY,
    VERSION,
    analyse_v95,
    apply_v95_policy,
    build_canonical_snapshot,
    build_v95_message,
    rank_analyses,
    _bridge_parent_inputs,
    _coinflip_confidence_shrink,
    format_telegram_message,
)
from q15_upgrade.ledger_v95 import CHAMPION_WEIGHTS, V95Ledger

NOW = 1_900_000_000.0


def candles(count=180, start=100.0, step=0.00010, cadence=5.0, oscillate=False):
    rows = []
    price = start
    for index in range(count):
        direction = -1.0 if oscillate and index % 2 else 1.0
        change = step * direction
        close = price * (1.0 + change)
        rows.append({
            "start_time": NOW - (count - index) * cadence,
            "end_time": NOW - (count - index - 1) * cadence,
            "open": price,
            "high": max(price, close) * 1.00020,
            "low": min(price, close) * 0.99980,
            "close": close,
            "volume": 10 + index,
        })
        price = close
    return rows


def context(direction="BULLISH"):
    return {
        "status": "PASS",
        "previous_15m": {"direction": direction, "coverage": 0.75, "candle_count": 132},
        "current_15m": {"direction": direction, "coverage": 0.70, "candle_count": 42},
    }


def public(price=101.0, agreement=0.9, age=2.0, momentum=0.001):
    return {
        "available": True,
        "composite_price": price,
        "source_count": 2,
        "source_agreement": agreement,
        "divergence_bps": 5.0,
        "fetched_at": NOW - age,
        "age_seconds": age,
        "price_returns": {"return_30s": momentum, "return_60s": momentum * 1.3, "return_180s": momentum * 2},
        "sources": {
            "coinbase": {
                "quality": 1.0,
                "flow": {"available": True, "imbalance": 0.35},
                "book": {"available": True, "imbalance": 0.20},
            },
            "kraken": {
                "quality": 0.9,
                "flow": {"available": True, "imbalance": 0.25},
                "book": {"available": True, "imbalance": 0.15},
            },
        },
        "derivatives": {"basis_bps": 4.0, "current_funding": 0.00005},
        "derivative_changes": {"mark_return_180s": 0.002, "open_interest_change_180s": 0.004},
    }


def snapshot(asset="BNB", checkpoint="10M", ask=52.0, target=100.0, spot=101.0, candle_rows=None):
    candle_rows = candle_rows if candle_rows is not None else candles()
    return {
        "asset": asset,
        "ticker": f"KX{asset}-{checkpoint}-{int(NOW)}",
        "checkpoint": checkpoint,
        "underlying_current": spot,
        "target_value": target,
        "seconds_remaining": 590 if checkpoint == "10M" else 840,
        "snapshot_time": NOW,
        "quote_timestamp": NOW,
        "underlying_candles_5s": candle_rows[-12:],
        "yes_bid": ask - 1,
        "yes_ask": ask,
        "yes_ask_size": 20,
        "no_bid": 99 - ask,
        "no_ask": 100 - (ask - 1),
        "no_ask_size": 20,
    }


class FakeNotifier:
    def __init__(self):
        self.enabled = True
        self.messages = []
        self._mid = 1000
        self.last_message_id = None
    def send(self, text, *args, **kwargs):
        self.messages.append(str(text))
        self._mid += 1
        self.last_message_id = self._mid
        return True
    def send_with_result(self, text):
        self.messages.append(str(text))
        self._mid += 1
        self.last_message_id = self._mid
        return {"ok": True, "delivered": True, "muted": False, "message_id": self._mid}


class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
    def query(self, sql, params=None):
        return list(self.rows)


class FakeHub:
    def __init__(self, value=None):
        self.value = value or public()
    def schedule(self, assets, now=None): pass
    def snapshot(self, asset, now=None): return dict(self.value)
    def health(self): return {"enabled": True, "fake": True}


class V95Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()
        # Clear the module-level "latest" caches a run_cycle leaves behind so a
        # test that drives run_cycle cannot bleed state into a later test.
        import q15_upgrade.checkpoint_v95 as _cp
        with _cp._LATEST_LOCK:
            _cp._LATEST_ANALYSES.clear()
            _cp._LATEST_RANKING.clear()
            _cp._LATEST_LEDGER.clear()
            _cp._LATEST_CHECKPOINT = "UNKNOWN"

    def canonical(self, row=None, cached=None, pub=None, ctx=None, checkpoint="10M"):
        row = row or snapshot(checkpoint=checkpoint)
        return build_canonical_snapshot(
            row, asset=str(row.get("asset") or "BNB"), checkpoint=checkpoint, now=NOW,
            cached_candles=cached if cached is not None else candles(),
            context=ctx if ctx is not None else context(),
            public=pub if pub is not None else public(),
        )

    def test_canonical_cache_is_shared_source(self):
        canonical = self.canonical(cached=candles(180))
        self.assertGreaterEqual(len(canonical.candles), 180)
        self.assertTrue(canonical.core_valid)

    def test_public_price_freshness_decay_is_configurable(self):
        import os
        # A public composite 20s old contributes to data_quality through its
        # freshness-weighted public_quality term. Shrinking the decay constant
        # collapses that freshness, so the same snapshot yields a lower
        # data_quality — proving the previously-hardcoded constant is now tunable.
        pub = public(age=20.0)
        try:
            os.environ.pop("Q15_V95_PUBLIC_PRICE_DECAY_SECONDS", None)
            default_snap = self.canonical(pub=pub)
            os.environ["Q15_V95_PUBLIC_PRICE_DECAY_SECONDS"] = "2"
            tight_snap = self.canonical(pub=pub)
            self.assertGreater(default_snap.data_quality, tight_snap.data_quality + 0.03)
        finally:
            os.environ.pop("Q15_V95_PUBLIC_PRICE_DECAY_SECONDS", None)

    def test_multi_horizon_drops_implausible_public_return(self):
        from q15_upgrade.checkpoint_v95 import _multi_horizon_returns
        # A percent-scaled (or already-log) public return outside (-1, 1) would make
        # log1p raise/-inf; it must be dropped and the candle return stand alone.
        good = self.canonical(pub=public(age=2.0, momentum=0.001))
        good_ret = _multi_horizon_returns(good)
        bad_pub = public(age=2.0)
        bad_pub["price_returns"] = {"return_30s": -50.0, "return_60s": 0.001}
        bad = self.canonical(pub=bad_pub)
        bad_ret = _multi_horizon_returns(bad)
        for key, val in bad_ret.items():
            if val is not None:
                self.assertTrue(math.isfinite(val), f"{key} must stay finite")
        # The candle-only return for 30s (public dropped) equals the pure candle path.
        self.assertIsNotNone(bad_ret["return_30s"])

    def test_public_dict_matches_asdict_value_without_deepcopy(self):
        # public_dict() is the hot path inside analyse_v95 (built into every
        # result's "canonical" key, ~600 candles + multi-source dicts). It must
        # stay a shallow field copy, NOT dataclasses.asdict (which recursively
        # deep-copies and dominated ~78% of analyse_v95). This locks both that
        # the value still equals the asdict reference AND that it is cheap:
        # nested containers are fresh (shallow) copies but the candle rows are
        # shared references (no per-row deepcopy).
        from dataclasses import asdict
        canonical = self.canonical(cached=candles(180))
        reference = asdict(canonical)
        reference["candles"] = list(canonical.candles)
        public = canonical.public_dict()
        self.assertEqual(public, reference)
        self.assertIsInstance(public["candles"], list)
        self.assertIsInstance(public["core_errors"], tuple)
        # Cheap copy: candle rows are shared, not deep-copied per row.
        self.assertIs(public["candles"][0], canonical.candles[0])
        # Top-level mutable containers are independent of the frozen dataclass.
        self.assertIsNot(public["context"], canonical.context)

    def test_stale_core_fails_closed(self):
        row = snapshot()
        row["snapshot_time"] = NOW - 120
        canonical = self.canonical(row=row)
        self.assertFalse(canonical.core_valid)
        self.assertIn("stale_core_snapshot", canonical.core_errors)

    def test_timestamp_alignment_is_reported(self):
        row = snapshot()
        row["quote_timestamp"] = NOW - 7
        canonical = self.canonical(row=row, pub=public(age=3))
        self.assertIsNotNone(canonical.alignment_seconds)
        self.assertGreaterEqual(canonical.alignment_seconds, 7)

    def test_above_threshold_produces_yes_lean(self):
        row = snapshot(spot=102.0, target=100.0)
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertTrue(result["prediction_available"])
        self.assertGreater(result["yes_probability"], 0.5)

    def test_below_threshold_produces_no_lean(self):
        row = snapshot(spot=98.0, target=100.0)
        result = analyse_v95(row, self.canonical(row=row, pub=public(price=98.0, momentum=-0.001), ctx=context("BEARISH")), self.ledger)
        self.assertLess(result["yes_probability"], 0.5)
        self.assertEqual(result["prediction_side"], "NO")

    def test_unknown_depth_penalty_is_gated_and_off_by_default(self):
        # Strip the Kalshi ask-size depth so _kalshi_depth() returns None (no
        # orderbook either). Default OFF: a missing/unverifiable book must NOT
        # silently penalize liquidity. ON: it is discounted by the configured
        # factor so an unconfirmed book ranks below a confirmed-liquid one. This
        # only moves trade_quality ranking; it is never an entry gate.
        row = snapshot(spot=102.0, target=100.0)
        row.pop("yes_ask_size", None)
        row.pop("no_ask_size", None)
        base = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertIsNone(base["quote"]["ask_depth"], "depth should be unknown")
        lq_off = base["liquidity_quality"]
        with patch.dict(os.environ, {"Q15_V95_PENALIZE_UNKNOWN_DEPTH": "true",
                                     "Q15_V95_UNKNOWN_DEPTH_FACTOR": "0.5"}):
            penalized = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertAlmostEqual(penalized["liquidity_quality"], lq_off * 0.5, places=6)

    def test_known_depth_is_unaffected_by_unknown_depth_flag(self):
        # With real depth present, the flag is a no-op (the penalty branch only
        # fires when depth is None).
        row = snapshot(spot=102.0, target=100.0)  # keeps yes_ask_size/no_ask_size
        off = analyse_v95(row, self.canonical(row=row), self.ledger)
        with patch.dict(os.environ, {"Q15_V95_PENALIZE_UNKNOWN_DEPTH": "true"}):
            on = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertIsNotNone(off["quote"]["ask_depth"])
        self.assertAlmostEqual(off["liquidity_quality"], on["liquidity_quality"], places=6)

    def test_yes_ask_qty_alias_records_known_depth(self):
        row = snapshot(spot=102.0, target=100.0)
        row.pop("yes_ask_size", None)
        row["yes_ask_qty"] = 33.0

        result = analyse_v95(row, self.canonical(row=row), self.ledger)

        self.assertEqual(result["quote"]["ask_depth"], 33.0)
        self.assertEqual(result["quote"]["depth_contracts"], 33.0)
        self.assertEqual(result["quote"]["yes_ask_depth_contracts"], 33.0)

    def test_raw_model_signal_is_independent_of_kalshi_price(self):
        # The model's own (raw) probability must not depend on the Kalshi price...
        low = snapshot(ask=25)
        high = snapshot(ask=85)
        a = analyse_v95(low, self.canonical(row=low), self.ledger)
        b = analyse_v95(high, self.canonical(row=high), self.ledger)
        self.assertAlmostEqual(a["raw_yes_probability"], b["raw_yes_probability"], places=12)
        # ...but the final anchored probability now reflects the market price, and
        # the edge still differs with the ask.
        self.assertNotEqual(a["yes_probability"], b["yes_probability"])
        self.assertNotEqual(a["net_edge_cents"], b["net_edge_cents"])

    def test_market_anchoring_shrinks_toward_market_price(self):
        # Model is very bullish; a cheap YES (market ~ low) pulls the prob down.
        row = snapshot(ask=20)   # yes mid ~ 19.5 -> market-implied YES ~ 0.195
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertTrue(result["market_anchor"]["applied"])
        self.assertLess(result["yes_probability"], result["model_yes_probability"])
        self.assertLessEqual(result["market_implied_yes_probability"], 0.25)

    def test_anchoring_disabled_restores_pure_model(self):
        os.environ["Q15_V95_MARKET_ANCHOR_STRENGTH"] = "0"
        try:
            low = snapshot(ask=25)
            high = snapshot(ask=85)
            a = analyse_v95(low, self.canonical(row=low), self.ledger)
            b = analyse_v95(high, self.canonical(row=high), self.ledger)
            self.assertAlmostEqual(a["yes_probability"], b["yes_probability"], places=12)
            self.assertFalse(a["market_anchor"]["applied"])
        finally:
            os.environ.pop("Q15_V95_MARKET_ANCHOR_STRENGTH", None)

    def test_missing_optional_data_still_produces_prediction(self):
        row = snapshot(candle_rows=[])
        canonical = self.canonical(row=row, cached=[], pub={"available": False}, ctx={})
        result = analyse_v95(row, canonical, self.ledger)
        self.assertTrue(result["prediction_available"])
        self.assertLess(result["data_quality"], 0.7)

    def test_extreme_probability_is_shrunk_when_quality_low(self):
        row = snapshot(spot=110.0, candle_rows=[])
        canonical = self.canonical(row=row, cached=[], pub={"available": False}, ctx={})
        result = analyse_v95(row, canonical, self.ledger)
        self.assertLessEqual(result["yes_probability"], 0.90)

    def test_flow_unavailable_is_missing_not_opposition(self):
        row = snapshot()
        canonical = self.canonical(row=row, pub={"available": False})
        result = analyse_v95(row, canonical, self.ledger)
        details = result["feature_details"]["flow"]
        self.assertEqual(details["available_sources"], 0)
        self.assertEqual(result["feature_quality"]["flow"], 0.0)

    def test_threshold_interaction_detects_crossings(self):
        rows = candles(180, oscillate=True)
        row = snapshot(spot=rows[-1]["close"], target=100.0, candle_rows=rows)
        canonical = self.canonical(row=row, cached=rows)
        result = analyse_v95(row, canonical, self.ledger)
        self.assertGreater(result["feature_details"]["threshold_interaction"]["crossings"], 3)

    def test_ranking_is_trade_economics_first(self):
        analyses = {
            "SOL": {"trade_decision":"WATCH_PRICE","net_edge_cents":-0.2,"conservative_probability":.53,"data_quality":.7,"trade_quality":.5,"prediction_side":"YES","ticker":"A"},
            "BNB": {"trade_decision":"ENTRY_RECOMMENDED","net_edge_cents":20.3,"conservative_probability":.74,"data_quality":.7,"trade_quality":.9,"prediction_side":"YES","ticker":"B"},
            "HYPE": {"trade_decision":"WATCH_PRICE","net_edge_cents":1.2,"conservative_probability":.89,"data_quality":.5,"trade_quality":.6,"prediction_side":"YES","ticker":"C"},
        }
        self.assertEqual([row["asset"] for row in rank_analyses(analyses)], ["BNB", "HYPE", "SOL"])

    def test_apply_policy_preserves_side_consistency(self):
        row = snapshot()
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        output = apply_v95_policy(dict(row), result)
        self.assertEqual(output["selected_side"], result["prediction_side"])
        self.assertEqual(output["entry_allowed"], result["entry_allowed"])

    def _record(self, ticker, checkpoint="15M", yes=.65, challenger=.64, baseline=.60, side="YES", quality=.8):
        return self.ledger.record_prediction(
            ticker=ticker, asset="BNB", checkpoint=checkpoint, created_at=NOW, close_time=NOW+600,
            predicted_side=side, raw_yes_probability=yes, calibrated_yes_probability=yes,
            challenger_yes_probability=challenger, baseline_yes_probability=baseline,
            selected_probability=yes if side=="YES" else 1-yes,
            conservative_probability=.58, data_quality=quality, evidence_quality=.7,
            trade_quality=.6, trade_decision="WATCH_PRICE", regime="NORMAL",
            features={name: 0.4 for name in CHAMPION_WEIGHTS if name != "intercept"},
            contributions={}, quote={"ask_cents":55},
        )

    def test_ledger_deduplicates_contract_checkpoint(self):
        self.assertTrue(self._record("T1")[1])
        self.assertFalse(self._record("T1")[1])
        self.assertEqual(self.ledger.status()["unique_predictions"], 1)

    def test_ten_minute_outcome_updates_primary_shadow(self):
        before = self.ledger.challenger_weights("10M")
        self._record("T10", checkpoint="10M")
        self.ledger.resolve_ticker("T10", "NO", NOW+700)
        after = self.ledger.challenger_weights("10M")
        self.assertNotEqual(before, after)
        self.assertEqual(self.ledger.status()["shadow_updates_by_checkpoint"]["10M"], 1)

    def test_fifteen_minute_learning_enabled_by_default(self):
        # 15M shadow learning now defaults ON (observational; champion stays
        # frozen). The challenger is allowed to learn from a 15M resolution.
        self.assertTrue(self.ledger.learning_enabled_by_checkpoint["15M"])
        self._record("T15", checkpoint="15M")
        self.ledger.resolve_ticker("T15", "NO", NOW+700)
        self.assertTrue(self.ledger.status()["production_weights_frozen"])

    def test_fifteen_minute_learning_can_be_disabled(self):
        # Opt-out: with 15M learning off, a resolution leaves the challenger frozen.
        self.ledger.learning_enabled_by_checkpoint["15M"] = False
        before = self.ledger.challenger_weights("15M")
        self._record("T15off", checkpoint="15M")
        self.ledger.resolve_ticker("T15off", "NO", NOW+700)
        self.assertEqual(before, self.ledger.challenger_weights("15M"))
        self.assertEqual(self.ledger.status()["shadow_updates_by_checkpoint"]["15M"], 0)
        self.assertTrue(self.ledger.status()["production_weights_frozen"])

    def test_checkpoint_learners_are_isolated(self):
        before_15 = self.ledger.challenger_weights("15M")
        self._record("ISO", checkpoint="10M")
        self.ledger.resolve_ticker("ISO", "NO", NOW+700)
        self.assertEqual(before_15, self.ledger.challenger_weights("15M"))

    def test_per_result_shadow_delta_is_bounded(self):
        before = self.ledger.challenger_weights("10M")
        self._record("TB", checkpoint="10M", yes=.90)
        self.ledger.resolve_ticker("TB", "NO", NOW+700)
        after = self.ledger.challenger_weights("10M")
        cap = self.ledger.per_result_cap_by_checkpoint["10M"]
        self.assertLessEqual(max(abs(after[k]-before[k]) for k in before), cap + 1e-12)

    def test_calibration_inactive_before_minimum(self):
        result = self.ledger.calibrate(.70, "15M", "BNB")
        self.assertFalse(result["active"])

    def test_calibration_activates_after_resolved_sample(self):
        for index in range(self.ledger.minimum_calibration_rows):
            ticker = f"CAL{index}"
            probability = .70 if index % 2 == 0 else .60
            self._record(ticker, yes=probability, challenger=probability, baseline=.55)
            self.ledger.resolve_ticker(ticker, "YES" if index % 3 else "NO", NOW+index+1)
        result = self.ledger.calibrate(.70, "15M", "BNB")
        self.assertTrue(result["active"])
        self.assertEqual(result["rows"], self.ledger.minimum_calibration_rows)

    def test_metrics_are_version_scoped_and_include_brier(self):
        self._record("M1")
        self.ledger.resolve_ticker("M1", "YES", NOW+1)
        metrics = self.ledger.metrics()
        self.assertEqual(metrics["overall"]["resolved"], 1)
        self.assertIn("champion_brier", metrics["overall"])

    def test_old_model_rows_cannot_train_new_checkpoint_learner(self):
        prediction_id, _ = self._record("OLDMODEL", checkpoint="10M")
        with self.ledger._lock, closing(self.ledger._connect()) as connection:
            connection.execute("UPDATE predictions SET model_version=? WHERE prediction_id=?", ("q15-v9.5-old", prediction_id))
            connection.commit()
        before = self.ledger.challenger_weights("10M")
        self.ledger.resolve_ticker("OLDMODEL", "NO", NOW+700)
        self.assertEqual(before, self.ledger.challenger_weights("10M"))

    def test_primary_learning_checkpoint_is_ten_minute(self):
        status = self.ledger.status()
        self.assertEqual(status["primary_learning_checkpoint"], "10M")
        self.assertTrue(status["learning_enabled_by_checkpoint"]["10M"])
        # 15M now learns too (observational); 7M stays off until its schema lands.
        self.assertTrue(status["learning_enabled_by_checkpoint"]["15M"])
        self.assertFalse(status["learning_enabled_by_checkpoint"]["7M"])

    def test_pattern_similarity_is_checkpoint_scoped(self):
        result = self.ledger.pattern_similarity({name: 0.1 for name in CHAMPION_WEIGHTS if name != "intercept"}, "YES", "10M")
        self.assertIn("shadow_adjustment", result)

    def test_notification_gate_deduplicates_initial(self):
        key = "V95|10M|T|123"
        permit = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="WATCH", fingerprint="a", now=NOW)
        self.assertIsNotNone(permit)
        self.ledger.complete_notification(event_key=permit, success=True, now=NOW)
        self.assertIsNone(self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="WATCH", fingerprint="b", now=NOW+1))

    def test_notification_gate_allows_entry_transition(self):
        key = "V95|10M|T|124"
        permit = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="WATCH", fingerprint="a", now=NOW)
        self.ledger.complete_notification(event_key=permit, success=True, now=NOW)
        transition = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="ENTRY_RECOMMENDED", fingerprint="b", now=NOW+1)
        self.assertIsNotNone(transition)
        self.assertIn("WATCH->ENTRY_RECOMMENDED", transition)

    def test_failed_notification_is_retryable(self):
        key = "V95|15M|T|125"
        permit = self.ledger.reserve_notification(event_key=key, checkpoint="15M", state="WATCH", fingerprint="a", now=NOW)
        self.ledger.complete_notification(event_key=permit, success=False, now=NOW)
        retry = self.ledger.reserve_notification(event_key=key, checkpoint="15M", state="WATCH", fingerprint="a", now=NOW+31)
        self.assertEqual(retry, key)

    def test_notification_gate_allows_entry_withdrawal_after_entry(self):
        key = "V95|10M|T|126"
        first = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="WATCH", fingerprint="a", now=NOW)
        self.ledger.complete_notification(event_key=first, success=True, now=NOW)
        entry = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="ENTRY_RECOMMENDED", fingerprint="b", now=NOW+1)
        self.ledger.complete_notification(event_key=entry, success=True, now=NOW+1)
        self.assertEqual(self.ledger.notification_state(key), "ENTRY_RECOMMENDED")
        withdrawal = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="ENTRY_WITHDRAWN", fingerprint="c", now=NOW+2)
        self.assertIsNotNone(withdrawal)
        self.assertIn("ENTRY_RECOMMENDED->ENTRY_WITHDRAWN", withdrawal)

    def test_failed_transition_is_retryable(self):
        key = "V95|10M|T|127"
        first = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="WATCH", fingerprint="a", now=NOW)
        self.ledger.complete_notification(event_key=first, success=True, now=NOW)
        transition = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="ENTRY_RECOMMENDED", fingerprint="b", now=NOW+1)
        self.ledger.complete_notification(event_key=transition, success=False, now=NOW+1)
        retry = self.ledger.reserve_notification(event_key=key, checkpoint="10M", state="ENTRY_RECOMMENDED", fingerprint="b", now=NOW+32)
        self.assertEqual(retry, transition)


    def test_message_is_unified_and_explainable(self):
        row = snapshot()
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        analyses = {"BNB": result}
        message = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())
        self.assertIn("V9.5 CHECK", message)         # formatter guard + identity
        self.assertIn("BNB", message)                # the pick
        self.assertIn("Top picks", message)          # hourly-report-style table
        self.assertIn("<pre>", message)              # aligned monospace block
        # The recommended entry (BNB qualifies) leads as the BEST ENTRY block, the
        # single source of truth shared with the detail below.
        self.assertIn("🏆 BEST ENTRY — BNB", message)
        self.assertIn("Entry status: RECOMMENDED", message)
        self.assertNotIn("Three requirements", message)

    def test_message_shows_market_implied_probability(self):
        # ask=52 -> yes mid 51.5 -> market-implied YES ~ 0.515; the pick is YES,
        # so the table's "Mkt" column shows the market-implied prob (~52%) next to
        # the model prob.
        row = snapshot(ask=52.0)
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertEqual(result["prediction_side"], "YES")
        analyses = {"BNB": result}
        message = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())
        self.assertIn("Mkt", message)   # market column header
        self.assertIn("52%", message)   # market-implied prob for the YES side

    def test_message_omits_market_implied_when_no_quote(self):
        # With no Kalshi quote the market-implied prob is None, so the "vs mkt"
        # annotation is dropped rather than rendering a noisy "vs mkt n/a".
        row = snapshot(ask=52.0)
        for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            row.pop(key, None)
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertIsNone(result["market_implied_yes_probability"])
        analyses = {"BNB": result}
        message = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())
        self.assertNotIn("vs mkt", message)
        self.assertIn("BNB", message)

    def test_read_only_markers(self):
        row = snapshot()
        result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertTrue(READ_ONLY)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["automatic_production_learning"])

    def test_source_contains_no_order_submission_surface(self):
        source = Path(__file__).parents[1].joinpath("q15_upgrade/checkpoint_v95.py").read_text().lower()
        forbidden = ("place_order(", "create_order(", "submit_order(", "cancel_order(")
        self.assertFalse(any(token in source for token in forbidden))

    def test_policy_one_second_cycles_send_once(self):
        notifier = FakeNotifier()
        store = FakeStore()
        with patch.dict(os.environ, {"Q15_V95_LEDGER_DB": str(Path(self.temp.name)/"policy.sqlite3")}, clear=False):
            with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
                policy = CheckpointPolicyV95(store)
        policy.market_data = FakeHub()
        policy._candle_cache = {"BNB": {row["start_time"]: row for row in candles()}}
        policy._latest_context = {"BNB": context()}
        policy._context_lock = threading.RLock()
        policy._candles = lambda asset: list(policy._candle_cache[asset].values())
        def parent(self, snapshots, now, ws_health, focus_manager, calibrated_edge, wrapped):
            wrapped.send("old v94 message")
            return snapshots
        with patch.object(CheckpointPolicyV94Unified, "run_cycle", parent):
            row = snapshot()
            for offset in range(5):
                policy.run_cycle({"BNB": dict(row)}, NOW+offset, {}, None, None, notifier)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("V9.5 CHECK", notifier.messages[0])

    def test_compact_panel_writes_official_record(self):
        # The compact panel (default) sends a V9.5 CHECK panel every checkpoint and
        # records the delivered call in the immutable official record with its
        # Telegram message_id.
        from contextlib import closing
        notifier = FakeNotifier()
        store = FakeStore()
        with patch.dict(os.environ, {"Q15_V95_LEDGER_DB": str(Path(self.temp.name)/"official.sqlite3")}, clear=False):
            with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
                policy = CheckpointPolicyV95(store)
        policy.market_data = FakeHub()
        policy._candle_cache = {"BNB": {row["start_time"]: row for row in candles()}}
        policy._latest_context = {"BNB": context()}
        policy._context_lock = threading.RLock()
        policy._candles = lambda asset: list(policy._candle_cache[asset].values())
        with patch.object(CheckpointPolicyV94Unified, "run_cycle", lambda self, snapshots, *a: snapshots):
            row = snapshot(spot=102.0, target=100.0)  # above strike -> YES lean
            for offset in range(4):
                policy.run_cycle({"BNB": dict(row)}, NOW + offset, {}, None, None, notifier)
        self.assertTrue(any("V9.5 CHECK" in m for m in notifier.messages))
        with closing(policy.ledger._connect()) as conn:
            rows = list(conn.execute(
                "SELECT interval, record_type, predicted_side, message_id "
                "FROM sent_predictions WHERE record_type='interval'"))
        self.assertEqual(len(rows), 1, "exactly one interval official record for the deduped panel")
        self.assertEqual(str(rows[0]["interval"]), "10M")
        self.assertIsNotNone(rows[0]["message_id"])

    def test_cycle_recap_sends_once_on_settlement(self):
        # A settled contract that was delivered earns one CYCLE CLOSED recap, and
        # the per-ticker dedup prevents a second recap on a later cycle.
        notifier = FakeNotifier()
        with patch.dict(os.environ, {"Q15_V95_LEDGER_DB": str(Path(self.temp.name)/"recap.sqlite3")}, clear=False):
            with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
                policy = CheckpointPolicyV95(FakeStore())
        led = policy.ledger
        # Seed a delivered + settled contract directly in the ledger.
        led.record_prediction(
            ticker="KXBTC-T", asset="BTC", checkpoint="10M", created_at=NOW,
            close_time=NOW + 600, predicted_side="YES",
            raw_yes_probability=0.7, calibrated_yes_probability=0.7,
            challenger_yes_probability=0.7, baseline_yes_probability=0.5,
            selected_probability=0.7, conservative_probability=0.62,
            data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
            trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
            features={"momentum": 0.3}, contributions={}, quote={"ask_cents": 50}, rank=1,
        )
        led.resolve_ticker("KXBTC-T", "YES")
        led.record_sent_prediction(
            contract_id="KXBTC-T", asset="BTC", interval="10M", record_type="interval",
            predicted_side="YES", probability=0.7, manipulation_probability=None,
            entry_decision="ENTRY_RECOMMENDED", sent_at=NOW + 1, close_time=NOW + 600, message_id=9,
        )
        events = [{"ticker": "KXBTC-T", "asset": "BTC", "checkpoint": "10M",
                   "official_result": "YES", "correct": True}]
        s1, _ = policy._send_cycle_recaps(events, notifier, NOW + 601)
        s2, _ = policy._send_cycle_recaps(events, notifier, NOW + 602)  # deduped
        self.assertEqual(s1, 1)
        self.assertEqual(s2, 0)
        recaps = [m for m in notifier.messages if "CYCLE CLOSED" in m]
        self.assertEqual(len(recaps), 1)
        self.assertIn("10M:  YES ✓", recaps[0])
        self.assertIn("— RUNNING RECORD —", recaps[0])

    def test_policy_records_unique_prediction(self):
        notifier = FakeNotifier()
        with patch.dict(os.environ, {"Q15_V95_LEDGER_DB": str(Path(self.temp.name)/"policy2.sqlite3")}, clear=False):
            with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
                policy = CheckpointPolicyV95(FakeStore())
        policy.market_data = FakeHub()
        policy._latest_context = {"BNB": context()}
        policy._context_lock = threading.RLock()
        policy._candles = lambda asset: candles()
        with patch.object(CheckpointPolicyV94Unified, "run_cycle", lambda self, snapshots, *args: snapshots):
            row = snapshot(checkpoint="15M")
            policy.run_cycle({"BNB": row}, NOW, {}, None, None, notifier)
            policy.run_cycle({"BNB": row}, NOW+1, {}, None, None, notifier)
        self.assertEqual(policy.ledger.status()["unique_predictions"], 1)

    def test_production_calibration_is_opt_in(self):
        row = snapshot()
        with patch.object(self.ledger, "calibrate", return_value={"probability": 0.51, "active": True, "rows": 100}):
            with patch.dict(os.environ, {"Q15_V95_PRODUCTION_CALIBRATION_ENABLED": "false"}, clear=False):
                result = analyse_v95(row, self.canonical(row=row), self.ledger)
        self.assertNotAlmostEqual(result["yes_probability"], 0.51, places=8)
        self.assertFalse(result["calibration"]["production_enabled"])

    def test_pattern_similarity_is_diagnostic_after_ten(self):
        for index in range(10):
            ticker = f"PAT{index}"
            self._record(ticker, checkpoint="10M", yes=.65, challenger=.64, baseline=.60)
            self.ledger.resolve_ticker(ticker, "YES" if index < 6 else "NO", NOW+index+1)
        pattern = self.ledger.pattern_similarity({name: .4 for name in CHAMPION_WEIGHTS if name != "intercept"}, "YES", "10M")
        self.assertTrue(pattern["active"])
        self.assertFalse(pattern["shadow_influence_active"])
        self.assertEqual(pattern["production_adjustment"], 0.0)

    def test_result_review_is_created(self):
        self._record("REVIEW1", yes=.70)
        result = self.ledger.resolve_ticker("REVIEW1", "NO", NOW+1)
        self.assertEqual(result["resolved"], 1)
        self.assertIn("review", result["events"][0])
        self.assertFalse(result["events"][0]["review"]["correct"])

    def test_challenger_is_never_auto_promoted(self):
        metrics = self.ledger.metrics()
        self.assertFalse(metrics["automatic_promotion"])



    def test_runtime_bridge_injects_cached_candles_and_momentum(self):
        class BridgePolicy:
            def __init__(self):
                self.market_data = FakeHub({"available": False})
            def _candles(self, asset):
                return candles(180)
        raw = snapshot(candle_rows=[])
        bridged, status = _bridge_parent_inputs(BridgePolicy(), {"BNB": raw}, NOW)
        self.assertGreaterEqual(len(bridged["BNB"]["underlying_candles_5s"]), 180)
        self.assertTrue(bridged["BNB"]["q15_v9_5_bridge_momentum_available"])
        self.assertEqual(status["assets_with_candles"], 1)

    def test_runtime_bridge_injects_public_flow_and_book(self):
        class BridgePolicy:
            def __init__(self):
                self.market_data = FakeHub(public())
            def _candles(self, asset):
                return candles(180)
        raw = snapshot(candle_rows=[])
        bridged, status = _bridge_parent_inputs(BridgePolicy(), {"BNB": raw}, NOW)
        self.assertIn("taker_buy_volume", bridged["BNB"])
        self.assertIn("taker_sell_volume", bridged["BNB"])
        self.assertIn("orderbook_imbalance", bridged["BNB"])
        self.assertEqual(status["public_flow_injected"], 1)
        self.assertEqual(status["public_book_injected"], 1)

    def test_legacy_report_is_marked_suppressed_before_first_v95_cycle(self):
        legacy = "🛑 10M CHECK — NO TRADE\nThree requirements: Wicks FAIL | Flow FAIL | Value FAIL"
        rendered = format_telegram_message(legacy)
        self.assertIn("V9.5 STARTUP", rendered)
        self.assertNotIn("Three requirements", rendered)


class CoinflipConfidenceShrinkTests(unittest.TestCase):
    """The near-coin-flip over-confidence guard (default OFF, side-preserving)."""

    def test_disabled_by_default_is_a_no_op(self):
        os.environ.pop("Q15_V95_COINFLIP_SHRINK_STRENGTH", None)
        for p in (0.5, 0.55, 0.59, 0.62, 0.90, 0.41, 0.10):
            out, info = _coinflip_confidence_shrink(p)
            self.assertEqual(out, p)
            self.assertFalse(info["active"])

    def test_shrinks_toward_half_inside_the_band(self):
        with patch.dict(os.environ, {"Q15_V95_COINFLIP_SHRINK_STRENGTH": "1.0",
                                     "Q15_V95_COINFLIP_SHRINK_BAND": "0.10"}, clear=False):
            out_yes, info = _coinflip_confidence_shrink(0.55)
            self.assertTrue(info["active"])
            self.assertLess(out_yes, 0.55)       # pulled toward 0.5
            self.assertGreaterEqual(out_yes, 0.5)  # but never across it
            out_no, _ = _coinflip_confidence_shrink(0.45)
            self.assertGreater(out_no, 0.45)
            self.assertLessEqual(out_no, 0.5)

    def test_confident_calls_outside_band_are_untouched(self):
        with patch.dict(os.environ, {"Q15_V95_COINFLIP_SHRINK_STRENGTH": "1.0",
                                     "Q15_V95_COINFLIP_SHRINK_BAND": "0.10"}, clear=False):
            for p in (0.61, 0.75, 0.95, 0.39, 0.20):  # |p-0.5| >= band -> weight 0
                out, _ = _coinflip_confidence_shrink(p)
                self.assertAlmostEqual(out, p, places=9)

    def test_never_flips_the_predicted_side(self):
        # For any strength/band and any probability, a YES (>=0.5) stays >=0.5 and a
        # NO (<0.5) stays <0.5 — so a previously-correct pick can never be flipped.
        with patch.dict(os.environ, {"Q15_V95_COINFLIP_SHRINK_STRENGTH": "1.0",
                                     "Q15_V95_COINFLIP_SHRINK_BAND": "0.25"}, clear=False):
            for i in range(1, 100):
                p = i / 100.0
                out, _ = _coinflip_confidence_shrink(p)
                self.assertEqual(p >= 0.5, out >= 0.5, f"side flipped at p={p}")

    def test_integration_analyse_v95_preserves_side_when_enabled(self):
        # Driving the live path with the guard ON must not change the predicted
        # side relative to the default (OFF) run on the identical snapshot.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = V95Ledger(Path(temp.name) / "shrink.sqlite3")
        row = snapshot(checkpoint="10M")
        canon = build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=NOW,
            cached_candles=candles(), context=context(), public=public(),
        )
        os.environ.pop("Q15_V95_COINFLIP_SHRINK_STRENGTH", None)
        base_a = analyse_v95(row, canon, ledger)
        with patch.dict(os.environ, {"Q15_V95_COINFLIP_SHRINK_STRENGTH": "1.0"}, clear=False):
            shrunk_a = analyse_v95(row, canon, ledger)
        self.assertEqual(base_a["prediction_side"], shrunk_a["prediction_side"])
        # Probability moves no further from 0.5 than the baseline (de-confidence).
        self.assertLessEqual(abs(shrunk_a["yes_probability"] - 0.5),
                             abs(base_a["yes_probability"] - 0.5) + 1e-9)


if __name__ == "__main__":
    unittest.main()
