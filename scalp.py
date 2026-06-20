"""Short-term scalp engine — bounce / mean-reversion calls (read-only).

A scalp is NOT a settlement bet. It looks for a contract whose YES price has
over-extended in one direction over the last ~30s and is just starting to snap
back, then proposes a small take-profit BEFORE the market's expiry. It never
places an order — it only emits a ⚡SCALP alert and records the call so its
hit-rate can be measured and reported alongside the directional entries.

Open scalps are tracked in-memory and resolved live every cycle against the
latest quote (take-profit hit / stop hit / exit-before-close). On resolution
the row's outcome + realized P&L (in cents) is written to Postgres. SCALP rows
are deliberately excluded from the settlement-based learning corpus (they don't
resolve on settlement) — see db.pending_settlements / representative_settled.
"""
import logging

from entry import kalshi_fee_cents

logger = logging.getLogger(__name__)

DISCLAIMER = ("Model-based estimate, not financial advice. "
              "Read-only monitor \u2014 no orders are placed.")


def _closes(candles):
    out = []
    for c in candles or []:
        v = c.get("close")
        if v is not None:
            out.append(float(v))
    return out


class ScalpEngine:
    """Detects and tracks short-term reversion scalps. Never trades."""

    LOOKBACK = 6  # 5s candles -> ~30s window used to measure the over-extension

    def __init__(self, store, notifier, config):
        self.store = store
        self.notifier = notifier
        self.cfg = config
        self.open = {}          # asset -> open scalp dict
        self.cooldown = {}      # asset -> ts until which no new scalp
        self.scalps_generated = 0

    # -- public ---------------------------------------------------------
    def evaluate(self, snapshots, now):
        if not self.cfg.scalp_enabled:
            return
        for asset, snap in snapshots.items():
            if not isinstance(snap, dict):
                continue
            # Q15_V5_FAIL_CLOSED: when the V5 freshness gate marks the snapshot's
            # market data invalid (stale/crossed/missing book), take NO scalp
            # action — neither open a new scalp nor score/resolve an open one on
            # untrustworthy quotes. The position is simply held until valid data
            # returns. This honors enforce_fail_closed so no scalp alert (open or
            # resolution) can fire on invalid data.
            if snap.get("v5_data_valid") is False or snap.get("signal_suppressed"):
                continue
            try:
                # 1) resolve an existing open scalp against the live quote
                if asset in self.open:
                    self._resolve(asset, snap, now)
                # 2) otherwise look for a fresh scalp
                if asset not in self.open:
                    self._maybe_open(asset, snap, now)
            except Exception as e:  # never let one asset break the loop
                logger.error(f"scalp {asset}: {e}")

    def record(self):
        """Win/loss tally + realized P&L for all settled scalp calls."""
        rows = self.store.query(
            "SELECT outcome, COUNT(*) AS n, "
            "COALESCE(SUM(realized_return),0) AS total, AVG(realized_return) AS avg "
            "FROM signals WHERE state='SCALP' AND outcome IN ('win','loss') "
            "GROUP BY outcome"
        )
        wins = losses = 0
        total = 0.0
        for r in rows:
            n = int(r["n"])
            total += float(r["total"] or 0)
            if r["outcome"] == "win":
                wins = n
            elif r["outcome"] == "loss":
                losses = n
        tot = wins + losses
        return {
            "wins": wins,
            "losses": losses,
            "total": tot,
            "win_rate": round(wins / tot, 3) if tot else None,
            "total_realized_cents": round(total, 2),
            "open": len(self.open),
        }

    def open_positions(self):
        return [dict(v) for v in self.open.values()]

    # -- detection ------------------------------------------------------
    def _maybe_open(self, asset, snap, now):
        cfg = self.cfg
        if now < self.cooldown.get(asset, 0):
            return
        if snap.get("market_state") != "live":
            return
        secs = snap.get("seconds_remaining")
        if secs is None or secs < cfg.scalp_min_secs or secs > cfg.scalp_max_secs:
            return
        spread = snap.get("spread")
        if spread is None or spread > cfg.scalp_max_spread:
            return
        yes_bid, yes_ask = snap.get("yes_bid"), snap.get("yes_ask")
        if yes_bid is None or yes_ask is None:
            return

        closes = _closes(snap.get("candles_5s"))
        if len(closes) < self.LOOKBACK + 1:
            return
        window = closes[-(self.LOOKBACK + 1):]
        first, last = window[0], window[-1]
        hi, lo = max(window), min(window)

        flow_yes = snap.get("taker_yes_volume_15s") or 0.0
        flow_no = snap.get("taker_no_volume_15s") or 0.0
        if (flow_yes + flow_no) < cfg.scalp_min_trades_15s:
            return
        rc = snap.get("reversal_confidence") or 0.0
        required_scalp_confidence = max(80.0, float(cfg.scalp_min_confidence))
        if rc < required_scalp_confidence:
            return

        side = direction = None
        rev = cfg.scalp_min_reversal_cents
        # Bounce UP: price fell hard then ticked back off the low -> buy YES
        drop = first - lo
        bounce_off = last - lo
        if drop >= cfg.scalp_min_move_cents and bounce_off >= rev and last < first:
            side, direction = "YES", "bounce up"
        # Fade DOWN: price spiked then pulled back off the high -> buy NO
        if side is None:
            rise = hi - first
            pullback = hi - last
            if rise >= cfg.scalp_min_move_cents and pullback >= rev and last > first:
                side, direction = "NO", "fade down"
        if side is None:
            return

        # A scalp alert now requires a projected NET gain of at least 10 cents
        # from the executable ask, after the estimated Kalshi fee. This is a
        # model target, not a guarantee that the market will reach it.
        target_net_cents = max(10.0, float(cfg.scalp_target_cents))
        yes_mid = (yes_bid + yes_ask) / 2.0
        if side == "YES":
            mark_at_signal = yes_mid
            entry_exec = yes_ask
            observed_extension = drop
        else:
            mark_at_signal = 100 - yes_mid
            entry_exec = 100 - yes_bid
            observed_extension = rise
        if entry_exec is None or entry_exec <= 0:
            return
        fee = float(kalshi_fee_cents(entry_exec))
        projected_move_cents = target_net_cents + fee
        target = entry_exec + projected_move_cents
        # Require enough prior extension to make a 10c net mean-reversion target
        # plausible, and reject contracts too close to 100c to have room.
        minimum_extension = target_net_cents + float(spread or 0.0) + fee
        if observed_extension < minimum_extension or target >= 99.0:
            return
        entry = entry_exec  # score from executable cost, not the midpoint
        stop_distance = max(6.0, target_net_cents * 0.6)
        stop = max(1.0, entry_exec - stop_distance)

        flow_total = float(flow_yes + flow_no)
        flow_skew = ((float(flow_yes) - float(flow_no)) / flow_total) if flow_total > 0 else 0.0
        learning_features = {
            "target_net_cents": target_net_cents,
            "spread": float(spread or 0.0),
            "reversal_score": float(rc or 0.0),
            "observed_extension": float(observed_extension or 0.0),
            "time_remaining": float(secs or 0.0),
            "entry_price": float(entry_exec),
            "stop_price": float(stop),
            "estimated_fee": float(fee),
            "flow_skew": flow_skew,
            "target_distance": float(target - entry_exec),
        }
        learning_assessment = None
        if hasattr(self, "learning_v2") and self.learning_v2 is not None:
            learning_assessment = self.learning_v2.assess_scalp(learning_features)
            if not learning_assessment.get("qualifies"):
                return

        sig = {
            "asset": asset,
            "ticker": snap.get("ticker"),
            "side": side,
            "direction": direction,
            "entry": round(entry, 1),          # executable entry cost for P&L scoring
            "entry_exec": round(entry_exec, 1),  # executable price (shown in alert)
            "mark": round(mark_at_signal, 1),  # current side midpoint
            "target": round(target, 1),
            "stop": round(stop, 1),
            "fee": round(fee, 2),
            "target_net_cents": round(target_net_cents, 2),
            "projected_move_cents": round(projected_move_cents, 2),
            "spread": spread,
            "secs": secs,
            "rc": round(rc, 1),
            "market_close": snap.get("close_time"),
            "opened_at": now,
            "peak": round(entry, 1),
            "trough": round(entry, 1),
            "learning_features": learning_features,
            "learning_assessment": learning_assessment or {},
        }
        self._open(asset, sig, now)

    def _open(self, asset, sig, now):
        cfg = self.cfg
        # one scalp per ticker+side per cooldown window across dev+prod instances
        win = int(now // max(1.0, cfg.scalp_cooldown_s))
        claim_key = f"scalp:{sig['ticker']}:{sig['side']}:{win}"
        sent = False
        if cfg.alerts_enabled and self.notifier.enabled:
            if not self.store.claim_event(claim_key):
                return  # another instance owns this scalp; don't duplicate it

        sid = self.store.insert_signal({
            "asset": asset,
            "ticker": sig["ticker"],
            "market_close": sig["market_close"],
            "side": sig["side"],
            "state": "SCALP",
            "signal_type": "scalp",
            "edge": sig.get("target_net_cents", 10.0),  # projected net gain in cents
            # Reversal score is data quality, not a calibrated probability.
            "fair_prob": round(float((sig.get("learning_assessment") or {}).get("target_probability") or 0.5), 4),
            "win_prob": round(float((sig.get("learning_assessment") or {}).get("target_probability") or 0.5), 4),
            "break_even": sig["entry_exec"] + sig["fee"],
            "entry_ask": sig["entry_exec"],
            "fee": sig["fee"],
            "spread": sig["spread"],
            "seconds_remaining": sig["secs"],
            "sent": False,  # flipped to True below only if the alert is delivered
        })
        sig["signal_id"] = sid

        try:
            settled = int((self.record() or {}).get("total") or 0)
        except Exception:
            settled = 0
        sig["actionable"] = bool(
            getattr(cfg, "scalp_actionable_alerts", False)
            and settled >= getattr(cfg, "scalp_min_settled_for_actionable", 30)
        )
        sig["settled_scalps"] = settled
        if hasattr(self, "learning_v2") and self.learning_v2 is not None:
            try:
                self.learning_v2.record_scalp_open(sig, now)
            except Exception:
                pass

        if cfg.alerts_enabled and self.notifier.enabled:
            if self.notifier.send(self._format(asset, sig)):
                sent = True
                self.scalps_generated += 1
                if sid is not None:
                    self.store.execute(
                        "UPDATE signals SET sent=TRUE WHERE id=%s", (sid,)
                    )
        self.open[asset] = sig
        logger.info(
            f"SCALP {asset} {sig['side']} {sig['direction']} entry={sig['entry']} "
            f"tgt={sig['target']} stop={sig['stop']} sent={sent}"
        )

    # -- resolution -----------------------------------------------------
    def _resolve(self, asset, snap, now):
        # Q15_V5_EXECUTABLE_SCALP_EXIT: score exits at executable bids, not midpoint.
        sig = self.open[asset]
        yes_bid = snap.get("yes_bid")
        yes_ask = snap.get("yes_ask")
        no_bid = snap.get("no_bid")
        if no_bid is None and yes_ask is not None:
            no_bid = 100.0 - float(yes_ask)

        secs = snap.get("seconds_remaining")
        rolled = (
            snap.get("market_state") != "live"
            or snap.get("ticker") != sig["ticker"]
        )

        executable_bid = None
        if not rolled:
            raw_bid = yes_bid if sig["side"] == "YES" else no_bid
            try:
                executable_bid = float(raw_bid) if raw_bid is not None else None
            except (TypeError, ValueError):
                executable_bid = None

        if executable_bid is not None:
            sig["mark"] = round(executable_bid, 1)
            if sig.get("peak") is None or executable_bid > sig["peak"]:
                sig["peak"] = round(executable_bid, 1)
            if sig.get("trough") is None or executable_bid < sig["trough"]:
                sig["trough"] = round(executable_bid, 1)

        outcome = exit_reason = None
        exit_price = executable_bid
        held = now - sig.get("opened_at", now)

        if executable_bid is not None:
            if executable_bid >= sig["target"]:
                outcome, exit_reason, exit_price = "win", "take_profit", sig["target"]
            elif (
                executable_bid <= sig["stop"]
                and held >= max(0.0, self.cfg.scalp_min_hold_secs)
            ):
                # A stop is not a guaranteed fill. A gap is scored at current bid.
                outcome, exit_reason, exit_price = "loss", "stop", executable_bid

        if outcome is None and (rolled or (secs is not None and secs <= 20)):
            if exit_price is None:
                exit_price = sig.get("mark") or sig["entry"]
            exit_reason = "rollover" if rolled else "exit_before_close"

        if exit_reason is None:
            return

        entry_fee = float(sig.get("fee") or 0.0)
        exit_fee = float(kalshi_fee_cents(exit_price))
        realized = round(exit_price - sig["entry"] - entry_fee - exit_fee, 2)
        if outcome is None:
            outcome = "win" if realized > 0 else "loss"
        sig["exit_fee"] = round(exit_fee, 2)

        if hasattr(self, "learning_v2") and self.learning_v2 is not None:
            try:
                self.learning_v2.record_scalp_resolution(
                    sig, now, exit_price, exit_reason, realized
                )
            except Exception:
                logger.exception("record_scalp_resolution failed")

        if sig.get("signal_id") is not None:
            self.store.update_outcome(sig["signal_id"], outcome, realized)
        self.cooldown[asset] = now + self.cfg.scalp_cooldown_s
        self.open.pop(asset, None)
        logger.info(
            f"SCALP RESOLVED {asset} {sig['side']} {outcome} ({exit_reason}) "
            f"px={exit_price} realized={realized:+.1f}c"
        )

        if (
            self.cfg.alerts_enabled
            and self.notifier.enabled
            and sig.get("signal_id") is not None
            and self.store.claim_event(f"scalpres:{sig['signal_id']}")
        ):
            icon = "✅" if outcome == "win" else "❌"
            self.notifier.send(
                f"{icon} <b>SCALP {outcome.upper()}</b> — {asset} {sig['side']}\n"
                f"{exit_reason.replace('_', ' ')} @ {exit_price:.1f}¢ "
                f"(entry {sig['entry']:.1f}¢) → {realized:+.1f}¢ net "
                f"after entry+exit fee estimates"
            )

    # -- formatting -----------------------------------------------------
    def _format(self, asset, sig):
        actionable = bool(sig.get("actionable"))
        if actionable:
            header = f"⚡ <b>SCALP SIGNAL — {asset} {sig['side']}</b> ({sig['direction']})"
            action = f"Preferred limit {sig['entry_exec']:.1f}¢ or lower"
        else:
            header = f"🧪 <b>PAPER SCALP — {asset} {sig['side']}</b> ({sig['direction']})"
            action = (
                "Do not place a real-money trade from this alert. "
                "Actionable scalp wording requires SCALP_ACTIONABLE_ALERTS=true "
                "and at least 30 settled scalp signals."
            )
        assessment = sig.get("learning_assessment") or {}
        probability_line = ""
        if assessment.get("target_probability") is not None:
            probability_line = (
                f"Estimated P(hit +10¢ before stop): "
                f"<b>{float(assessment['target_probability']) * 100:.1f}%</b> • "
                f"modeled EV {float(assessment.get('expected_net_ev_cents') or 0):+.1f}¢\n"
            )
        return (
            f"{header}\n"
            f"{action}\n"
            f"{probability_line}"
            f"Contract: <code>{sig['ticker']}</code>\n"
            f"Executable entry: <b>{sig['entry_exec']:.1f}¢</b>\n"
            f"Projected take-profit: <b>{sig['target']:.1f}¢</b> "
            f"(+{sig['target_net_cents']:.1f}¢ net after estimated fee)\n"
            f"Stop reference: {sig['stop']:.1f}¢ <i>(mid)</i>\n"
            f"Reversal data-quality score: {sig['rc']:.0f}% • spread {sig['spread']:.1f}¢\n"
            f"Exit before expiry ({int(sig['secs'])}s left)\n"
            f"<i>Projected move is not guaranteed. {DISCLAIMER}</i>"
        )
