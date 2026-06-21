import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager, adaptive_confirmation_ok, assess_scalp_candidate


class Store:
    enabled = False


class Notifier:
    enabled = True
    def __init__(self): self.messages=[]
    def send(self, msg): self.messages.append(msg); return True


class Client:
    def get_market(self, ticker): return {"status":"settled", "result":"yes"}


def snap(asset="BTC", ticker="T1", secs=885, p=0.72, close="2026-06-19T00:00:00+00:00"):
    now=time.time()
    candles=[]
    price=100.0
    for i in range(40):
        o=price
        price += 0.02
        candles.append({"start_time":now-200+i*5,"end_time":now-195+i*5,"open":o,"high":price+0.01,"low":o-0.01,"close":price})
    return {
        "asset":asset,"ticker":ticker,"market_state":"live","seconds_remaining":secs,"close_time":close,
        "estimated_fair_probability":p,"yes_bid":60,"yes_ask":61,"no_bid":39,"no_ask":40,"spread":1,
        "underlying_current":100.8,"target_value":100.0,"target_distance_pct":0.8,"target_type":"greater_or_equal",
        "underlying_candles_5s":candles,"model_confidence":0.9,"confirmation_group_count":3,"confirmation_score":72,
        "follow_through_confirmed":True,"entry_side":"YES","estimated_edge":8,"required_edge":5,
        "yes_ask_qty":50,"no_ask_qty":50,"data_age_seconds":1,"pattern_direction":"bullish","pattern_detected":"target reclaim",
        "taker_yes_volume_15s":20,"taker_no_volume_15s":5,"orderbook_imbalance":0.3,
    }


class Tests(unittest.TestCase):
    def settings(self, **kwargs):
        values=dict(min_vote_samples=3, checkpoint_15_seconds=885, checkpoint_15_latest_seconds=720,
                    checkpoint_10_seconds=600, checkpoint_10_latest_seconds=480,
                    alert_15_at_seconds=870, alert_10_at_seconds=585)
        values.update(kwargs)
        return FocusSettings(**values)

    def test_two_predictions_freeze_and_same(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        s=snap()
        now=time.time()
        for i in range(5):
            s["seconds_remaining"]=890-i
            m.update({"BTC":s},now+i,{})
        s["seconds_remaining"]=885
        out=m.update({"BTC":s},now+6,{})["BTC"]
        self.assertEqual(out["q15_prediction_15m"]["side"],"YES")
        frozen=out["q15_prediction_15m"]["yes_probability"]
        for i in range(8):
            s["seconds_remaining"]=607-i
            s["estimated_fair_probability"]=0.75
            m.update({"BTC":s},now+20+i,{})
        s["seconds_remaining"]=600
        out=m.update({"BTC":s},now+30,{})["BTC"]
        self.assertEqual(out["q15_prediction_15m"]["yes_probability"],frozen)
        self.assertEqual(out["q15_prediction_agreement"],"SAME")
        self.assertEqual(out["q15_decision_state"],"AGREEMENT")
        self.assertEqual(out["q15_final_result"],"YES")

    def test_conflict_is_uncertain(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        s=snap(p=.75)
        now=time.time()
        for i in range(5):
            s["seconds_remaining"]=890-i
            m.update({"BTC":s},now+i,{})
        s["seconds_remaining"]=885
        m.update({"BTC":s},now+6,{})
        s["estimated_fair_probability"]=0.20
        s["yes_bid"],s["yes_ask"],s["no_bid"],s["no_ask"] = 19,20,80,81
        s["pattern_direction"]="bearish"; s["entry_side"]="NO"; s["orderbook_imbalance"]=-.5
        s["taker_yes_volume_15s"],s["taker_no_volume_15s"] = 1,30
        for i in range(10):
            s["seconds_remaining"]=609-i
            m.update({"BTC":s},now+20+i,{})
        s["seconds_remaining"]=600
        out=m.update({"BTC":s},now+31,{})["BTC"]
        self.assertIn(out["q15_prediction_10m"]["side"],{"NO","UNCERTAIN"})
        self.assertIn(out["q15_prediction_agreement"],{"CONFLICT","UNCERTAIN"})
        self.assertEqual(out["q15_final_result"],"UNCERTAIN")
        self.assertFalse(out["q15_new_entry_allowed"])

    def test_v9_canonical_probability_returns_tuple(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        s=snap()
        s["v9_uncalibrated_p_yes"]=0.82
        result=m._raw_yes_probability(s)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        raw_p, features = result
        self.assertAlmostEqual(raw_p, 0.82, places=6)
        self.assertIsInstance(features, dict)
        s["v9_uncalibrated_p_yes"]=82.0
        raw_p2, _ = m._raw_yes_probability(s)
        self.assertAlmostEqual(raw_p2, 0.82, places=6)
        now=time.time()
        for i in range(3):
            s["seconds_remaining"]=890-i
            m.update({"BTC":s},now+i,{})

    def test_v9_probability_dropped_when_snapshot_stale(self):
        # The canonical v9 probability overrides the live component blend, so a
        # value carried on a stale snapshot must not resurrect a prior lean.
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        fresh=snap()
        fresh["v9_uncalibrated_p_yes"]=0.82
        fresh_p,_=m._raw_yes_probability(fresh)
        self.assertAlmostEqual(fresh_p, 0.82, places=6)   # fresh: canonical used
        stale=snap()
        stale["v9_uncalibrated_p_yes"]=0.82
        stale["data_age_seconds"]=999                     # far beyond the budget
        stale_p,_=m._raw_yes_probability(stale)
        # Stale: the override is dropped and we fall back to the live blend, which
        # for this snapshot is not the injected 0.82.
        self.assertNotAlmostEqual(stale_p, 0.82, places=6)

    def test_top_three_only(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        now=time.time(); snaps={}
        for idx,a in enumerate(["BTC","ETH","SOL","XRP"]):
            snaps[a]=snap(a,f"T{idx}",p=.80-.02*idx)
        for i in range(5):
            for s in snaps.values(): s["seconds_remaining"]=890-i
            m.update(snaps,now+i,{})
        for s in snaps.values(): s["seconds_remaining"]=885
        m.update(snaps,now+6,{})
        for i in range(8):
            for s in snaps.values(): s["seconds_remaining"]=607-i
            m.update(snaps,now+20+i,{})
        for s in snaps.values(): s["seconds_remaining"]=600
        snaps["XRP"]["position_state"] = "ACTIVE"
        out=m.update(snaps,now+30,{})
        self.assertEqual(sum(1 for s in out.values() if s["q15_in_top3"]),3)
        outside = next(s for s in out.values() if not s["q15_in_top3"])
        self.assertTrue(outside["q15_new_entry_allowed"])

    def test_adaptive_confirmation_paths(self):
        strong = {"confirmation_group_count": 2, "confirmation_score": 80, "model_confidence": .82,
                  "estimated_edge": 9, "required_edge": 5}
        weak = dict(strong, confirmation_score=60)
        moderate = dict(strong, confirmation_group_count=3, confirmation_score=58, model_confidence=.55,
                        estimated_edge=5)
        self.assertTrue(adaptive_confirmation_ok(snapshot=strong))
        self.assertFalse(adaptive_confirmation_ok(snapshot=weak))
        self.assertTrue(adaptive_confirmation_ok(snapshot=moderate))

    def test_scalp_probability_and_ev_gate(self):
        strong=assess_scalp_candidate(target_net_cents=10,spread=1,reversal_score=92,observed_extension=22,time_remaining=300,entry_price=45,stop_price=39,estimated_fee=2)
        weak=assess_scalp_candidate(target_net_cents=10,spread=3,reversal_score=70,observed_extension=10,time_remaining=70,entry_price=45,stop_price=39,estimated_fee=2)
        self.assertGreater(strong["target_probability"],weak["target_probability"])
        self.assertGreater(strong["expected_net_ev_cents"],weak["expected_net_ev_cents"])
        self.assertTrue(strong["qualifies"])
        self.assertFalse(weak["qualifies"])

    def test_no_trade_alert_shows_top_three_rejections(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        diagnostics=[
            {"asset":"DOGE","candidate_side":"NO","side_probability":.736,"reasons":["15m: only 4/5 rolling observations"]},
            {"asset":"BTC","candidate_side":"YES","side_probability":.621,"reasons":["15m: consensus 69% < 72%"]},
            {"asset":"ETH","candidate_side":"NO","side_probability":.584,"reasons":["15m: side estimate 58.4% < 60%"]},
        ]
        message=m._format_checkpoint_alert("15m",None,diagnostics)
        self.assertIn("Closest candidates",message)
        self.assertIn("DOGE NO",message)
        self.assertIn("73.6%",message)
        self.assertIn("only 4/5",message)
        self.assertIn("BTC YES",message)
        self.assertIn("ETH NO",message)

    def test_final_ranking_prefers_net_ev_over_probability(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        now=time.time()
        high_prob=snap("BTC","TA",p=.86)
        high_prob["yes_bid"],high_prob["yes_ask"] = 59,60
        better_ev=snap("ETH","TB",p=.73)
        better_ev["yes_bid"],better_ev["yes_ask"] = 39,40
        snaps={"BTC":high_prob,"ETH":better_ev}
        for i in range(5):
            for row in snaps.values(): row["seconds_remaining"]=890-i
            m.update(snaps,now+i,{})
        for row in snaps.values(): row["seconds_remaining"]=885
        m.update(snaps,now+6,{})
        for i in range(8):
            for row in snaps.values(): row["seconds_remaining"]=607-i
            m.update(snaps,now+20+i,{})
        for row in snaps.values(): row["seconds_remaining"]=600
        m.update(snaps,now+30,{})
        ranking=m.focus_status()["rankings"][high_prob["close_time"]]["final"]
        self.assertEqual(ranking[0]["asset"],"ETH")
        self.assertGreater(ranking[0]["ranking_expected_value_cents"],ranking[1]["ranking_expected_value_cents"])

    def test_late_strong_is_shadow_only_and_never_entry_allowed(self):
        settings=self.settings(min_vote_samples=8, late_strong_min_probability=.70,
                               late_strong_min_consensus=.80, late_strong_min_score=65,
                               late_strong_min_net_edge_cents=5)
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), settings)
        s=snap(p=.50)
        now=time.time()
        for i in range(3):
            s["seconds_remaining"]=888-i
            m.update({"BTC":s},now+i,{})
        s["seconds_remaining"]=885
        first=m.update({"BTC":s},now+4,{})["BTC"]
        self.assertEqual(first["q15_prediction_15m"]["side"],"UNCERTAIN")
        s["estimated_fair_probability"]=.92
        s["yes_bid"],s["yes_ask"],s["no_bid"],s["no_ask"] = 45,46,54,55
        s["confirmation_score"]=85
        for i in range(12):
            s["seconds_remaining"]=611-i
            m.update({"BTC":s},now+20+i,{})
        s["seconds_remaining"]=600
        out=m.update({"BTC":s},now+40,{})["BTC"]
        self.assertEqual(out["q15_prediction_10m"]["side"],"YES")
        self.assertEqual(out["q15_decision_state"],"LATE_STRONG_SHADOW")
        self.assertFalse(out["q15_new_entry_allowed"])
        self.assertFalse(out["q15_late_strong_shadow"]["entry_allowed"])

    def test_regime_thresholds_are_shadow_only(self):
        m=TwoWindowFocusManager(Store(), Notifier(), None, Client(), self.settings())
        s=snap()
        s["volatility_regime"]="high volatility expansion"
        rec=m._regime_shadow_recommendation(s)
        self.assertEqual(rec["regime"],"HIGH_VOLATILITY")
        self.assertFalse(rec["applied_live"])
        self.assertGreater(rec["suggested_thresholds"]["min_vote_samples"],m.settings.min_vote_samples)


if __name__ == "__main__":
    unittest.main()


class EmptyCloseTimeClaim(unittest.TestCase):
    def _settings(self):
        return FocusSettings(min_vote_samples=3, checkpoint_15_seconds=885,
            checkpoint_15_latest_seconds=720, checkpoint_10_seconds=600,
            checkpoint_10_latest_seconds=480, alert_15_at_seconds=870, alert_10_at_seconds=585)

    def test_missing_close_time_creates_no_empty_claim_or_alert(self):
        m = TwoWindowFocusManager(Store(), Notifier(), None, Client(), self._settings())
        s = snap(secs=400, close="")          # past every checkpoint time, but no close_time
        m.update({"BTC": s}, time.time())
        self.assertFalse(
            any(k.endswith(":") for k in m._local_claims),
            f"degenerate empty-suffix claim keys were created: {m._local_claims}",
        )
        self.assertEqual(m.notifier.messages, [])   # nothing delivered for an unidentifiable market

    def test_valid_close_time_still_claims_and_alerts(self):
        m = TwoWindowFocusManager(Store(), Notifier(), None, Client(), self._settings())
        s = snap(secs=400, close="2026-06-19T00:00:00+00:00")
        m.update({"BTC": s}, time.time())
        self.assertTrue(any(k == "q15-two-window:7m:2026-06-19T00:00:00+00:00" for k in m._local_claims))
        self.assertTrue(m.notifier.messages)        # the 7m checkpoint alert fired
