"""Point-in-time feature construction for Q15 MarketLead.

The engine records evidence only. It has no entry rule, notification path, or
order surface. Missing lanes remain explicit and never receive neutral values.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from statistics import median
from typing import Any, Mapping

from ..lineage import lineage_stamp
from .config import MarketLeadConfig

FEATURE_SCHEMA_VERSION = "q15-marketlead-evidence-v2"
LEAD_LAG_RULE_VERSION = "external-lead-kalshi-lag-v1"


def feature_lineage_config(config: MarketLeadConfig) -> dict[str, Any]:
    """Single canonical definition of the inputs that shape evidence rows."""
    return {
        "mark_seconds": config.mark_seconds,
        "min_proxy_sources": config.min_proxy_sources,
        "min_venue_sources": config.min_venue_sources,
        "proxy_sources": sorted(config.proxy_sources),
        "require_live_proxy_sources": config.require_live_proxy_sources,
        "source_stale_seconds": config.source_stale_seconds,
        "transport_stale_seconds": config.transport_stale_seconds,
        "source_future_tolerance_seconds": (
            config.source_future_tolerance_seconds
        ),
        "max_source_spread_bps": config.max_source_spread_bps,
        "sync_tolerance_seconds": config.sync_tolerance_seconds,
        "max_proxy_dispersion_bps": config.max_proxy_dispersion_bps,
        "history_seconds": config.history_seconds,
        "lead_lag_rule_version": LEAD_LAG_RULE_VERSION,
        "lead_lag_pressure_max": config.lead_lag_pressure_max,
        "kalshi_stale_seconds": config.kalshi_stale_seconds,
        "paper_limit_cents": config.paper_limit_cents,
    }


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _side_value(value: float | None, side: str, *, invert: bool = False) -> float | None:
    if value is None:
        return None
    sign = 1.0 if str(side).upper() == "YES" else -1.0
    return value * (-sign if invert else sign)


class MarketLeadFeatureEngine:
    """Maintain bounded venue history and build one immutable evidence row."""

    def __init__(self, config: MarketLeadConfig):
        self.config = config
        self._venue_history: dict[tuple[str, str], deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=512)
        )
        self._leader_history: dict[str, deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=128)
        )

    def _update_sources(
        self, asset: str, public: Mapping[str, Any], now: float
    ) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for name, raw in _mapping(public.get("sources")).items():
            source = _mapping(raw)
            price = _num(source.get("price"))
            timestamp = _num(source.get("timestamp"))
            if timestamp is None:
                timestamp = _num(public.get("fetched_at"))
            if price is None or price <= 0 or timestamp is None:
                continue
            if timestamp > now + max(0.0, self.config.source_future_tolerance_seconds):
                continue
            age = max(0.0, now - timestamp)
            if age > max(0.0, self.config.source_stale_seconds):
                continue
            transport = str(source.get("transport") or "rest_public")
            best_bid = _num(source.get("best_bid"))
            best_ask = _num(source.get("best_ask"))
            spread_bps = _num(source.get("spread_bps"))
            if transport.startswith("websocket_"):
                message_age = _num(source.get("transport_message_age_seconds"))
                if not source.get("transport_connected"):
                    continue
                if (
                    message_age is None
                    or message_age > max(0.0, self.config.transport_stale_seconds)
                ):
                    continue
                # The BOOK's own age, not the transport's. `timestamp` above is
                # `sample_timestamp`, stamped at read time, so `age` is always ~0 and can
                # never see a stale book; heartbeats likewise keep `message_age` low after a
                # level2 subscription silently dies. This is the only gate that rejects a
                # frozen book being served as a live venue at quality 1.0.
                book_stale_limit = max(0.0, getattr(self.config, "book_stale_seconds", 0.0))
                book_age = _num(source.get("book_update_age_seconds"))
                if book_stale_limit and book_age is not None and book_age > book_stale_limit:
                    continue
                if (
                    best_bid is None
                    or best_ask is None
                    or best_bid <= 0
                    or best_ask <= best_bid
                ):
                    continue
                if spread_bps is None:
                    mid = (best_bid + best_ask) / 2.0
                    spread_bps = (best_ask - best_bid) / mid * 10_000.0
                if spread_bps > max(0.0, self.config.max_source_spread_bps):
                    continue
            key = (asset, str(name))
            history = self._venue_history[key]
            if not history or timestamp > history[-1][0]:
                history.append((timestamp, price))
            cutoff = now - max(60.0, self.config.history_seconds)
            while history and history[0][0] < cutoff:
                history.popleft()
            fresh.append({
                "name": str(name),
                "price": price,
                "timestamp": timestamp,
                "age_seconds": age,
                "quality": _num(source.get("quality")) or 0.5,
                "transport": transport,
                "instrument": source.get("instrument"),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": spread_bps,
                "sequence": source.get("sequence"),
                "transport_connected": source.get("transport_connected"),
                "transport_message_age_seconds": _num(
                    source.get("transport_message_age_seconds")
                ),
                "book_update_timestamp": _num(source.get("book_update_timestamp")),
                "book_update_age_seconds": _num(
                    source.get("book_update_age_seconds")
                ),
                "flow": _num(_mapping(source.get("flow")).get("imbalance")),
                "book": _num(_mapping(source.get("book")).get("imbalance")),
                "microprice_bps": _num(_mapping(source.get("book")).get("microprice_bps")),
            })
        return fresh

    @staticmethod
    def _synchronized_cluster(
        sources: list[dict[str, Any]], tolerance_seconds: float
    ) -> list[dict[str, Any]]:
        """Choose the largest all-pairs time-aligned cluster, newest on ties."""
        ordered = sorted(sources, key=lambda row: float(row["timestamp"]))
        best: list[dict[str, Any]] = []
        best_score: tuple[int, float, float] = (-1, -1.0, -1.0)
        tolerance = max(0.0, tolerance_seconds)
        for start, first in enumerate(ordered):
            cluster = [
                row
                for row in ordered[start:]
                if float(row["timestamp"]) - float(first["timestamp"]) <= tolerance
            ]
            score = (
                len(cluster),
                sum(float(row["quality"]) for row in cluster),
                max(float(row["timestamp"]) for row in cluster),
            )
            if score > best_score:
                best = cluster
                best_score = score
        return best

    def _return_bps(self, asset: str, source: str, now: float, horizon: int) -> float | None:
        history = self._venue_history.get((asset, source))
        if not history:
            return None
        latest_at, latest = history[-1]
        if now - latest_at > self.config.source_stale_seconds:
            return None
        target = now - horizon
        prior = [row for row in history if row[0] <= target]
        if not prior or prior[-1][1] <= 0:
            return None
        return (latest / prior[-1][1] - 1.0) * 10_000.0

    @staticmethod
    def _weighted_proxy(sources: list[dict[str, Any]]) -> float | None:
        expanded: list[float] = []
        for source in sources:
            copies = max(1, min(10, int(round(float(source["quality"]) * 10.0))))
            expanded.extend([float(source["price"])] * copies)
        return median(expanded) if expanded else None

    def build(
        self,
        *,
        asset: str,
        analysis: Mapping[str, Any],
        canonical: Any,
        now: float,
        official_index: Mapping[str, Any] | None,
        kalshi: Mapping[str, Any] | None,
        live_sources: Mapping[str, Any] | None = None,
        seconds_remaining: float | None = None,
    ) -> dict[str, Any]:
        asset = str(asset).upper()
        public = _mapping(getattr(canonical, "public", {}))
        live = _mapping(live_sources)
        merged_raw_sources = dict(_mapping(public.get("sources")))
        live_names: list[str] = []
        for name, source in _mapping(live.get("sources")).items():
            if not isinstance(source, Mapping):
                continue
            normalized_name = str(name).lower()
            merged_raw_sources[normalized_name] = dict(source)
            live_names.append(normalized_name)
        merged_public = {**dict(public), "sources": merged_raw_sources}
        fresh_sources = self._update_sources(asset, merged_public, now)
        venue_sources = self._synchronized_cluster(
            fresh_sources, self.config.sync_tolerance_seconds
        )
        synchronized = len(venue_sources) >= self.config.min_venue_sources
        proxy_candidates = [
            row
            for row in fresh_sources
            if str(row["name"]).lower() in self.config.proxy_sources
            and (
                not self.config.require_live_proxy_sources
                or str(row["transport"]).startswith("websocket_")
            )
        ]
        proxy_sources = self._synchronized_cluster(
            proxy_candidates, self.config.sync_tolerance_seconds
        )
        proxy_synchronized = len(proxy_sources) >= self.config.min_proxy_sources
        proxy_candidate_price = (
            self._weighted_proxy(proxy_sources) if proxy_synchronized else None
        )
        prices = [float(row["price"]) for row in proxy_sources]
        dispersion_bps = None
        if (
            proxy_candidate_price is not None
            and proxy_candidate_price > 0
            and len(prices) >= 2
        ):
            dispersion_bps = (
                (max(prices) - min(prices)) / proxy_candidate_price * 10_000.0
            )
        dispersion_ok = (
            dispersion_bps is None
            or dispersion_bps <= max(0.0, self.config.max_proxy_dispersion_bps)
        )
        proxy = proxy_candidate_price if dispersion_ok else None

        venue_rows: list[dict[str, Any]] = []
        for source in venue_sources:
            returns = {
                horizon: self._return_bps(asset, source["name"], now, horizon)
                for horizon in (5, 15, 30)
            }
            r15 = returns[15]
            weighted_sum = 0.0
            active_weight = 0.0
            if r15 is not None:
                weighted_sum += 0.60 * math.tanh(r15 / 5.0)
                active_weight += 0.60
            if source["flow"] is not None:
                weighted_sum += 0.25 * float(source["flow"])
                active_weight += 0.25
            if source["microprice_bps"] is not None:
                weighted_sum += 0.15 * math.tanh(float(source["microprice_bps"]) / 2.0)
                active_weight += 0.15
            impulse = weighted_sum / active_weight if active_weight > 0 else None
            venue_rows.append({**source, "returns_bps": returns, "impulse": impulse})

        impulses = [float(row["impulse"]) for row in venue_rows if row["impulse"] is not None]
        venue_impulse = sum(impulses) / len(impulses) if impulses else None
        aligned_fraction = None
        if venue_impulse is not None and impulses:
            direction = 1.0 if venue_impulse >= 0 else -1.0
            aligned_fraction = sum(value * direction > 0 for value in impulses) / len(impulses)
        leader = None
        if impulses:
            leader_row = max(
                (row for row in venue_rows if row["impulse"] is not None),
                key=lambda row: abs(float(row["impulse"])),
            )
            leader = str(leader_row["name"])
            history = self._leader_history[asset]
            if not history or history[-1][1] != leader or now - history[-1][0] >= 1.0:
                history.append((now, leader))
            while history and history[0][0] < now - 60.0:
                history.popleft()
        leader_history = self._leader_history.get(asset, ())
        leader_persistence = (
            None if not leader or not leader_history
            else sum(name == leader for _, name in leader_history) / len(leader_history)
        )

        index = _mapping(official_index)
        index_price = _num(index.get("index_px"))
        proxy_gap_bps = (
            None if proxy is None or index_price is None or index_price <= 0
            else (proxy / index_price - 1.0) * 10_000.0
        )
        predicted_side = str(analysis.get("prediction_side") or "").upper()
        yes_is_higher = bool(getattr(canonical, "yes_is_higher", True))
        orientation = 1.0 if yes_is_higher else -1.0
        side_sign = 1.0 if predicted_side == "YES" else -1.0
        proxy_lead_side_bps = (
            None if proxy_gap_bps is None else proxy_gap_bps * orientation * side_sign
        )
        strike_price = _num(getattr(canonical, "threshold", None))
        proxy_distance_side_bps = (
            None if proxy is None or strike_price is None or strike_price <= 0
            else math.log(proxy / strike_price) * 10_000.0 * orientation * side_sign
        )
        venue_impulse_side = _side_value(
            None if venue_impulse is None else venue_impulse * orientation,
            predicted_side,
        )

        quote = _mapping(analysis.get("quote"))
        entry_ask = _num(analysis.get("entry_ask_cents"))
        if entry_ask is None:
            entry_ask = _num(quote.get("ask_cents"))
        kalshi_row = dict(kalshi or {})
        kalshi_available = bool(kalshi_row.get("available"))
        kalshi_book_age = _num(kalshi_row.get("book_age_seconds"))
        kalshi_event_age = _num(kalshi_row.get("event_age_seconds"))
        kalshi_fresh = bool(
            kalshi_book_age is not None
            and kalshi_event_age is not None
            and kalshi_book_age <= max(0.0, self.config.kalshi_stale_seconds)
            and kalshi_event_age <= max(0.0, self.config.kalshi_stale_seconds)
        )
        pressure_yes = _num(kalshi_row.get("book_delta_pressure_yes_15s"))
        trade_yes = _num(kalshi_row.get("trade_imbalance_yes_15s"))
        micro_edge_yes = _num(kalshi_row.get("yes_microprice_edge_cents"))
        pressure_parts = [value for value in (pressure_yes, trade_yes) if value is not None]
        if micro_edge_yes is not None:
            pressure_parts.append(math.tanh(micro_edge_yes / 1.5))
        kalshi_pressure_yes = (
            sum(pressure_parts) / len(pressure_parts) if pressure_parts else None
        )
        kalshi_pressure_side = _side_value(
            None if kalshi_pressure_yes is None else kalshi_pressure_yes * orientation,
            predicted_side,
        )
        selected_ask = _num(
            kalshi_row.get("yes_ask_cents" if predicted_side == "YES" else "no_ask_cents")
        )
        touched = bool(
            selected_ask is not None and selected_ask <= self.config.paper_limit_cents
        )

        missing: list[str] = []
        if proxy is None or len(proxy_sources) < self.config.min_proxy_sources:
            missing.append("RTI_PROXY_INCOMPLETE")
        if proxy_synchronized and not dispersion_ok:
            missing.append("RTI_PROXY_DISPERSION_HIGH")
        if venue_impulse is None or not synchronized:
            missing.append("VENUE_IMPULSE_INCOMPLETE")
        if not kalshi_available or kalshi_pressure_side is None:
            missing.append("KALSHI_EVENTS_INCOMPLETE")
        elif not kalshi_fresh:
            missing.append("KALSHI_EVENTS_STALE")
        limitations: list[str] = []
        if index_price is None:
            limitations.append("OFFICIAL_INDEX_GAP_UNAVAILABLE")
        evidence_status = "READY" if not missing else "PARTIAL"
        joint_alignment = bool(
            evidence_status == "READY"
            and proxy_distance_side_bps is not None and proxy_distance_side_bps > 0
            and venue_impulse_side is not None and venue_impulse_side > 0
            and kalshi_pressure_side is not None and kalshi_pressure_side > 0
        )
        # A lead is useful before Kalshi has followed it. Requiring Kalshi
        # pressure to agree (the legacy joint-alignment cohort) selected moves
        # that were often already priced in. This prospective cohort instead
        # requires external price/impulse agreement while Kalshi still lags.
        lead_lag_candidate = bool(
            evidence_status == "READY"
            and proxy_distance_side_bps is not None and proxy_distance_side_bps > 0
            and venue_impulse_side is not None and venue_impulse_side > 0
            and kalshi_pressure_side is not None
            and kalshi_pressure_side <= self.config.lead_lag_pressure_max
        )
        feature_payload = {
            "proxy": {
                "price": proxy,
                "official_index": index_price,
                "gap_bps": proxy_gap_bps,
                "lead_side_bps": proxy_lead_side_bps,
                "distance_side_bps": proxy_distance_side_bps,
                "dispersion_bps": dispersion_bps,
                "synchronized": proxy_synchronized,
                "configured_sources": sorted(self.config.proxy_sources),
                "used_sources": [row["name"] for row in proxy_sources],
                "require_live_sources": self.config.require_live_proxy_sources,
            },
            "venue": {
                "impulse": venue_impulse,
                "impulse_side": venue_impulse_side,
                "aligned_fraction": aligned_fraction,
                "leader": leader,
                "leader_persistence": leader_persistence,
                "sources": venue_rows,
            },
            "source_selection": {
                "live_sources_received": sorted(live_names),
                "live_diagnostics": _mapping(live.get("diagnostics")),
                "fresh_sources": [row["name"] for row in fresh_sources],
                "venue_cluster": [row["name"] for row in venue_sources],
                "proxy_candidates": [row["name"] for row in proxy_candidates],
                "source_stale_seconds": self.config.source_stale_seconds,
                "transport_stale_seconds": self.config.transport_stale_seconds,
                "source_future_tolerance_seconds": (
                    self.config.source_future_tolerance_seconds
                ),
                "max_source_spread_bps": self.config.max_source_spread_bps,
                "sync_tolerance_seconds": self.config.sync_tolerance_seconds,
                "max_proxy_dispersion_bps": self.config.max_proxy_dispersion_bps,
            },
            "kalshi": kalshi_row,
            "kalshi_freshness": {
                "book_age_seconds": kalshi_book_age,
                "event_age_seconds": kalshi_event_age,
                "max_age_seconds": self.config.kalshi_stale_seconds,
                "fresh": kalshi_fresh,
            },
            "candidate": {
                "rule_version": LEAD_LAG_RULE_VERSION,
                "lead_lag_candidate": lead_lag_candidate,
                "kalshi_pressure_side_max": self.config.lead_lag_pressure_max,
                "proxy_distance_aligned": (
                    proxy_distance_side_bps is not None
                    and proxy_distance_side_bps > 0
                ),
                "venue_impulse_aligned": (
                    venue_impulse_side is not None and venue_impulse_side > 0
                ),
                "kalshi_lagging": (
                    kalshi_pressure_side is not None
                    and kalshi_pressure_side <= self.config.lead_lag_pressure_max
                ),
            },
        }
        return {
            "system_version": self.config.system_version,
            "asset": asset,
            "ticker": str(getattr(canonical, "ticker", "") or ""),
            "window_key": int(float(getattr(canonical, "settlement_time", now) or now) // 900),
            "mark_seconds": self.config.mark_seconds,
            "observed_at": now,
            "close_time": _num(getattr(canonical, "settlement_time", None)),
            "seconds_remaining": (
                _num(seconds_remaining)
                if seconds_remaining is not None
                else _num(getattr(canonical, "seconds_remaining", None))
            ),
            "predicted_side": predicted_side or None,
            "entry_ask_cents": entry_ask,
            "spread_cents": _num(quote.get("spread_cents")),
            "spot_price": _num(getattr(canonical, "spot", None)),
            "strike_price": _num(getattr(canonical, "threshold", None)),
            "official_index_price": index_price,
            "rti_proxy_price": proxy,
            "proxy_gap_bps": proxy_gap_bps,
            "proxy_lead_side_bps": proxy_lead_side_bps,
            "proxy_distance_side_bps": proxy_distance_side_bps,
            "rti_proxy_source_count": len(proxy_sources),
            "rti_proxy_sources_json": json.dumps(
                [row["name"] for row in proxy_sources], separators=(",", ":")
            ),
            "venue_source_count": len(venue_sources),
            "venue_dispersion_bps": dispersion_bps,
            "venue_impulse": venue_impulse,
            "venue_impulse_side": venue_impulse_side,
            "venue_aligned_fraction": aligned_fraction,
            "venue_leader": leader,
            "venue_leader_persistence": leader_persistence,
            "kalshi_available": 1 if kalshi_available else 0,
            "kalshi_book_age_seconds": kalshi_book_age,
            "kalshi_event_age_seconds": kalshi_event_age,
            "kalshi_yes_bid_cents": _num(kalshi_row.get("yes_bid_cents")),
            "kalshi_yes_ask_cents": _num(kalshi_row.get("yes_ask_cents")),
            "kalshi_microprice_yes_cents": _num(kalshi_row.get("yes_microprice_cents")),
            "kalshi_microprice_edge_yes_cents": micro_edge_yes,
            "kalshi_pressure_yes_5s": _num(kalshi_row.get("book_delta_pressure_yes_5s")),
            "kalshi_pressure_yes_15s": pressure_yes,
            "kalshi_pressure_yes_30s": _num(kalshi_row.get("book_delta_pressure_yes_30s")),
            "kalshi_trade_imbalance_yes_15s": trade_yes,
            "kalshi_pressure_side": kalshi_pressure_side,
            "paper_limit_cents": self.config.paper_limit_cents,
            "paper_queue_ahead_contracts": _num(
                kalshi_row.get(
                    "yes_bid_queue_at_or_above_limit"
                    if predicted_side == "YES"
                    else "no_bid_queue_at_or_above_limit"
                )
            ),
            "paper_limit_touched": 1 if touched else 0,
            "paper_limit_touch_at": now if touched else None,
            "paper_touch_price_cents": selected_ask if touched else None,
            "execution_status": "TOUCHED" if touched else "WAITING_TOUCH",
            "joint_alignment": 1 if joint_alignment else 0,
            "lead_lag_candidate": 1 if lead_lag_candidate else 0,
            "evidence_status": evidence_status,
            "missing_reasons_json": json.dumps(missing, separators=(",", ":")),
            "limitations_json": json.dumps(limitations, separators=(",", ":")),
            "features_json": json.dumps(feature_payload, sort_keys=True, separators=(",", ":")),
            **lineage_stamp(
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                config=feature_lineage_config(self.config),
            ),
        }


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "LEAD_LAG_RULE_VERSION",
    "feature_lineage_config",
    "MarketLeadFeatureEngine",
]
