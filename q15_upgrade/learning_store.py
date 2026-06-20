from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class LearningStore:
    """Postgres adapter layered on the existing SignalStore query/execute API."""

    def __init__(self, store):
        self.store = store
        self.enabled = bool(store and getattr(store, "enabled", False))
        self.last_error = None

    def migrate(self) -> bool:
        if not self.enabled:
            return False
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_decision_events (
                id BIGSERIAL PRIMARY KEY,
                decision_key TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                direction TEXT,
                strategy TEXT NOT NULL,
                decision_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                market_close TIMESTAMPTZ,
                policy_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                calibration_version TEXT,
                feature_schema_version TEXT NOT NULL,
                code_commit TEXT,
                learning_corpus_cutoff TIMESTAMPTZ,
                raw_win_probability DOUBLE PRECISION,
                calibrated_win_probability DOUBLE PRECISION,
                executable_ask DOUBLE PRECISION,
                executable_vwap DOUBLE PRECISION,
                quantity DOUBLE PRECISION,
                estimated_fee DOUBLE PRECISION,
                estimated_slippage DOUBLE PRECISION,
                maximum_entry_price DOUBLE PRECISION,
                required_edge DOUBLE PRECISION,
                predicted_edge DOUBLE PRECISION,
                confirmation_groups JSONB,
                confirmation_group_count INTEGER,
                confirmation_score DOUBLE PRECISION,
                model_data_quality_score DOUBLE PRECISION,
                seconds_remaining DOUBLE PRECISION,
                spread DOUBLE PRECISION,
                depth DOUBLE PRECISION,
                underlying_price DOUBLE PRECISION,
                target DOUBLE PRECISION,
                invalidation_level DOUBLE PRECISION,
                volatility_regime TEXT,
                feature_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                shadow BOOLEAN NOT NULL DEFAULT FALSE,
                suppressed BOOLEAN NOT NULL DEFAULT FALSE,
                suppression_reason TEXT,
                alert_sent BOOLEAN NOT NULL DEFAULT FALSE,
                outcome TEXT,
                realized_pnl DOUBLE PRECISION,
                settled_at TIMESTAMPTZ,
                settlement_source TEXT,
                valid_for_learning BOOLEAN NOT NULL DEFAULT TRUE,
                invalid_learning_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_ticker ON q15_decision_events(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_asset ON q15_decision_events(asset)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_strategy ON q15_decision_events(strategy)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_ts ON q15_decision_events(decision_timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_settled ON q15_decision_events(settled_at)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_policy ON q15_decision_events(policy_version)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_shadow ON q15_decision_events(shadow)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_suppressed ON q15_decision_events(suppressed)",
            "CREATE INDEX IF NOT EXISTS idx_q15_decision_valid ON q15_decision_events(valid_for_learning)",
            """
            CREATE TABLE IF NOT EXISTS q15_scalp_decision_events (
                id BIGSERIAL PRIMARY KEY,
                scalp_key TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                entry_price DOUBLE PRECISION,
                executable_vwap DOUBLE PRECISION,
                quantity DOUBLE PRECISION,
                estimated_fee DOUBLE PRECISION,
                estimated_slippage DOUBLE PRECISION,
                target_price DOUBLE PRECISION,
                stop_price DOUBLE PRECISION,
                time_remaining DOUBLE PRECISION,
                predicted_target_probability DOUBLE PRECISION,
                predicted_stop_probability DOUBLE PRECISION,
                predicted_seconds_to_target DOUBLE PRECISION,
                predicted_max_favorable_excursion DOUBLE PRECISION,
                predicted_max_adverse_excursion DOUBLE PRECISION,
                maximum_favorable_excursion DOUBLE PRECISION,
                maximum_adverse_excursion DOUBLE PRECISION,
                seconds_to_target DOUBLE PRECISION,
                seconds_to_stop DOUBLE PRECISION,
                exit_timestamp TIMESTAMPTZ,
                exit_price DOUBLE PRECISION,
                exit_reason TEXT,
                target_hit BOOLEAN,
                stop_hit BOOLEAN,
                expired_without_target_or_stop BOOLEAN,
                realized_net_pnl DOUBLE PRECISION,
                model_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL,
                feature_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                shadow BOOLEAN NOT NULL DEFAULT TRUE,
                valid_for_learning BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_q15_scalp_ticker ON q15_scalp_decision_events(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_q15_scalp_asset ON q15_scalp_decision_events(asset)",
            "CREATE INDEX IF NOT EXISTS idx_q15_scalp_entry_ts ON q15_scalp_decision_events(entry_timestamp)",
            """
            CREATE TABLE IF NOT EXISTS q15_model_versions (
                id BIGSERIAL PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                model_type TEXT NOT NULL,
                status TEXT NOT NULL,
                champion_or_challenger TEXT NOT NULL,
                params_json JSONB NOT NULL,
                metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                feature_schema_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                learning_corpus_cutoff TIMESTAMPTZ,
                trained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activated_at TIMESTAMPTZ,
                deactivated_at TIMESTAMPTZ,
                rollback_parent_version TEXT,
                notes TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_q15_model_type_status ON q15_model_versions(model_type, status)",
            """
            CREATE TABLE IF NOT EXISTS q15_segment_adjustments (
                segment_key TEXT PRIMARY KEY,
                dimension TEXT NOT NULL,
                n INTEGER NOT NULL,
                effective_n DOUBLE PRECISION NOT NULL,
                weighted_win_rate DOUBLE PRECISION,
                weighted_predicted_probability DOUBLE PRECISION,
                weighted_realized_pnl DOUBLE PRECISION,
                ev_lower_bound DOUBLE PRECISION,
                target_edge_adjustment DOUBLE PRECISION NOT NULL,
                active_edge_adjustment DOUBLE PRECISION NOT NULL,
                suppressed BOOLEAN NOT NULL DEFAULT FALSE,
                recovery_streak INTEGER NOT NULL DEFAULT 0,
                shadow_recovery_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
                reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS q15_end_predictions (
                id BIGSERIAL PRIMARY KEY,
                prediction_key TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                prediction_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                market_close TIMESTAMPTZ,
                version TEXT NOT NULL,
                predicted_result TEXT,
                chart_yes_probability DOUBLE PRECISION,
                blended_yes_probability DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                predicted_final_underlying DOUBLE PRECISION,
                predicted_range_low DOUBLE PRECISION,
                predicted_range_high DOUBLE PRECISION,
                current_underlying DOUBLE PRECISION,
                target_value DOUBLE PRECISION,
                seconds_remaining DOUBLE PRECISION,
                feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                outcome TEXT,
                settled_at TIMESTAMPTZ,
                valid_for_learning BOOLEAN NOT NULL DEFAULT TRUE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_q15_end_ticker ON q15_end_predictions(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_q15_end_ts ON q15_end_predictions(prediction_timestamp)",
            """
            CREATE TABLE IF NOT EXISTS q15_learning_events (
                id BIGSERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                version TEXT,
                details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ]
        ok = True
        for statement in statements:
            if not self._execute(statement):
                ok = False
        return ok

    def insert_decision(self, row: dict) -> bool:
        sql = """
            INSERT INTO q15_decision_events (
                decision_key,ticker,asset,side,direction,strategy,decision_timestamp,market_close,
                policy_version,model_version,calibration_version,feature_schema_version,code_commit,
                learning_corpus_cutoff,raw_win_probability,calibrated_win_probability,executable_ask,
                executable_vwap,quantity,estimated_fee,estimated_slippage,maximum_entry_price,
                required_edge,predicted_edge,confirmation_groups,confirmation_group_count,
                confirmation_score,model_data_quality_score,seconds_remaining,spread,depth,
                underlying_price,target,invalidation_level,volatility_regime,feature_snapshot_json,
                shadow,suppressed,suppression_reason,alert_sent,valid_for_learning,invalid_learning_reason
            ) VALUES (
                %s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s
            ) ON CONFLICT (decision_key) DO NOTHING
        """
        values = (
            row.get("decision_key"), row.get("ticker"), row.get("asset"), row.get("side"),
            row.get("direction"), row.get("strategy"), row.get("decision_timestamp"), row.get("market_close"),
            row.get("policy_version"), row.get("model_version"), row.get("calibration_version"),
            row.get("feature_schema_version"), row.get("code_commit"), row.get("learning_corpus_cutoff"),
            row.get("raw_win_probability"), row.get("calibrated_win_probability"), row.get("executable_ask"),
            row.get("executable_vwap"), row.get("quantity"), row.get("estimated_fee"),
            row.get("estimated_slippage"), row.get("maximum_entry_price"), row.get("required_edge"),
            row.get("predicted_edge"), json.dumps(row.get("confirmation_groups") or {}),
            row.get("confirmation_group_count"), row.get("confirmation_score"),
            row.get("model_data_quality_score"), row.get("seconds_remaining"), row.get("spread"),
            row.get("depth"), row.get("underlying_price"), row.get("target"), row.get("invalidation_level"),
            row.get("volatility_regime"), json.dumps(row.get("feature_snapshot_json") or {}),
            bool(row.get("shadow")), bool(row.get("suppressed")), row.get("suppression_reason"),
            bool(row.get("alert_sent")), bool(row.get("valid_for_learning", True)),
            row.get("invalid_learning_reason"),
        )
        return self._execute(sql, values)

    def settled_decisions(self, limit: int = 10000) -> list[dict]:
        return self._query(
            """
            SELECT * FROM q15_decision_events
            WHERE outcome IN ('win','loss') AND valid_for_learning=TRUE
            ORDER BY decision_timestamp ASC LIMIT %s
            """,
            (limit,),
        )

    def pending_decisions(self, limit: int = 1200) -> list[dict]:
        return self._query(
            """
            SELECT id,ticker,asset,side,executable_ask,estimated_fee,decision_timestamp
            FROM q15_decision_events
            WHERE outcome IS NULL AND valid_for_learning=TRUE
            ORDER BY decision_timestamp ASC LIMIT %s
            """,
            (limit,),
        )

    def settle_decision(self, decision_id: int, outcome: str, pnl: float, source: str) -> bool:
        return self._execute(
            """
            UPDATE q15_decision_events
            SET outcome=%s,realized_pnl=%s,settled_at=NOW(),settlement_source=%s
            WHERE id=%s AND outcome IS NULL
            """,
            (outcome, pnl, source, decision_id),
        )

    def insert_end_prediction(self, row: dict) -> bool:
        return self._execute(
            """
            INSERT INTO q15_end_predictions (
                prediction_key,ticker,asset,prediction_timestamp,market_close,version,predicted_result,
                chart_yes_probability,blended_yes_probability,confidence,predicted_final_underlying,
                predicted_range_low,predicted_range_high,current_underlying,target_value,seconds_remaining,
                feature_json,valid_for_learning
            ) VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (prediction_key) DO NOTHING
            """,
            (
                row.get("prediction_key"), row.get("ticker"), row.get("asset"), row.get("prediction_timestamp"),
                row.get("market_close"), row.get("version"), row.get("predicted_result"),
                row.get("chart_yes_probability"), row.get("blended_yes_probability"), row.get("confidence"),
                row.get("predicted_final_underlying"), row.get("predicted_range_low"), row.get("predicted_range_high"),
                row.get("current_underlying"), row.get("target_value"), row.get("seconds_remaining"),
                json.dumps(row.get("feature_json") or {}), bool(row.get("valid_for_learning", True)),
            ),
        )

    def pending_end_predictions(self, limit: int = 2000) -> list[dict]:
        return self._query(
            "SELECT id,ticker FROM q15_end_predictions WHERE outcome IS NULL ORDER BY prediction_timestamp LIMIT %s",
            (limit,),
        )

    def settle_end_prediction(self, prediction_id: int, outcome: str) -> bool:
        return self._execute(
            "UPDATE q15_end_predictions SET outcome=%s,settled_at=NOW() WHERE id=%s AND outcome IS NULL",
            (outcome, prediction_id),
        )

    def insert_scalp(self, row: dict) -> bool:
        return self._execute(
            """
            INSERT INTO q15_scalp_decision_events (
                scalp_key,ticker,asset,side,entry_timestamp,entry_price,executable_vwap,quantity,
                estimated_fee,estimated_slippage,target_price,stop_price,time_remaining,
                predicted_target_probability,predicted_stop_probability,predicted_seconds_to_target,
                predicted_max_favorable_excursion,predicted_max_adverse_excursion,model_version,
                policy_version,feature_schema_version,feature_snapshot_json,shadow,valid_for_learning
            ) VALUES (%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (scalp_key) DO NOTHING
            """,
            (
                row.get("scalp_key"), row.get("ticker"), row.get("asset"), row.get("side"),
                row.get("entry_timestamp"), row.get("entry_price"), row.get("executable_vwap"),
                row.get("quantity"), row.get("estimated_fee"), row.get("estimated_slippage"),
                row.get("target_price"), row.get("stop_price"), row.get("time_remaining"),
                row.get("predicted_target_probability"), row.get("predicted_stop_probability"),
                row.get("predicted_seconds_to_target"), row.get("predicted_max_favorable_excursion"),
                row.get("predicted_max_adverse_excursion"), row.get("model_version"), row.get("policy_version"),
                row.get("feature_schema_version"), json.dumps(row.get("feature_snapshot_json") or {}),
                bool(row.get("shadow", True)), bool(row.get("valid_for_learning", True)),
            ),
        )

    def settle_scalp(self, scalp_key: str, row: dict) -> bool:
        return self._execute(
            """
            UPDATE q15_scalp_decision_events SET
                maximum_favorable_excursion=%s,maximum_adverse_excursion=%s,seconds_to_target=%s,
                seconds_to_stop=%s,exit_timestamp=to_timestamp(%s),exit_price=%s,exit_reason=%s,
                target_hit=%s,stop_hit=%s,expired_without_target_or_stop=%s,realized_net_pnl=%s
            WHERE scalp_key=%s AND exit_timestamp IS NULL
            """,
            (
                row.get("maximum_favorable_excursion"), row.get("maximum_adverse_excursion"),
                row.get("seconds_to_target"), row.get("seconds_to_stop"), row.get("exit_timestamp"),
                row.get("exit_price"), row.get("exit_reason"), bool(row.get("target_hit")),
                bool(row.get("stop_hit")), bool(row.get("expired_without_target_or_stop")),
                row.get("realized_net_pnl"), scalp_key,
            ),
        )

    def settled_scalps(self, limit: int = 10000) -> list[dict]:
        return self._query(
            """
            SELECT * FROM q15_scalp_decision_events
            WHERE exit_timestamp IS NOT NULL AND valid_for_learning=TRUE
            ORDER BY entry_timestamp ASC LIMIT %s
            """,
            (limit,),
        )

    def load_active_model(self, model_type: str) -> dict | None:
        rows = self._query(
            """
            SELECT * FROM q15_model_versions
            WHERE model_type=%s AND status='active'
            ORDER BY activated_at DESC NULLS LAST, trained_at DESC LIMIT 1
            """,
            (model_type,),
        )
        return rows[0] if rows else None

    def insert_model_version(self, row: dict) -> bool:
        return self._execute(
            """
            INSERT INTO q15_model_versions (
                version,model_type,status,champion_or_challenger,params_json,metrics_json,
                feature_schema_version,policy_version,learning_corpus_cutoff,rollback_parent_version,notes
            ) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
            ON CONFLICT (version) DO UPDATE SET
                status=EXCLUDED.status,champion_or_challenger=EXCLUDED.champion_or_challenger,
                params_json=EXCLUDED.params_json,metrics_json=EXCLUDED.metrics_json,notes=EXCLUDED.notes
            """,
            (
                row.get("version"), row.get("model_type"), row.get("status", "challenger"),
                row.get("champion_or_challenger", "challenger"), json.dumps(row.get("params_json") or {}),
                json.dumps(row.get("metrics_json") or {}), row.get("feature_schema_version"),
                row.get("policy_version"), row.get("learning_corpus_cutoff"),
                row.get("rollback_parent_version"), row.get("notes"),
            ),
        )

    def activate_model(self, model_type: str, version: str) -> bool:
        ok1 = self._execute(
            "UPDATE q15_model_versions SET status='inactive',deactivated_at=NOW() WHERE model_type=%s AND status='active'",
            (model_type,),
        )
        ok2 = self._execute(
            """
            UPDATE q15_model_versions SET status='active',champion_or_challenger='champion',
            activated_at=NOW(),deactivated_at=NULL WHERE version=%s AND model_type=%s
            """,
            (version, model_type),
        )
        return ok1 and ok2

    def load_segment_adjustments(self) -> list[dict]:
        return self._query("SELECT * FROM q15_segment_adjustments", ())

    def upsert_segment(self, row: dict) -> bool:
        return self._execute(
            """
            INSERT INTO q15_segment_adjustments (
                segment_key,dimension,n,effective_n,weighted_win_rate,weighted_predicted_probability,
                weighted_realized_pnl,ev_lower_bound,target_edge_adjustment,active_edge_adjustment,
                suppressed,recovery_streak,shadow_recovery_metrics,reasons,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,NOW())
            ON CONFLICT (segment_key) DO UPDATE SET
                dimension=EXCLUDED.dimension,n=EXCLUDED.n,effective_n=EXCLUDED.effective_n,
                weighted_win_rate=EXCLUDED.weighted_win_rate,
                weighted_predicted_probability=EXCLUDED.weighted_predicted_probability,
                weighted_realized_pnl=EXCLUDED.weighted_realized_pnl,
                ev_lower_bound=EXCLUDED.ev_lower_bound,target_edge_adjustment=EXCLUDED.target_edge_adjustment,
                active_edge_adjustment=EXCLUDED.active_edge_adjustment,suppressed=EXCLUDED.suppressed,
                recovery_streak=EXCLUDED.recovery_streak,
                shadow_recovery_metrics=EXCLUDED.shadow_recovery_metrics,reasons=EXCLUDED.reasons,updated_at=NOW()
            """,
            (
                row.get("segment_key"), row.get("dimension"), row.get("n", 0), row.get("effective_n", 0.0),
                row.get("weighted_win_rate"), row.get("weighted_predicted_probability"),
                row.get("weighted_realized_pnl"), row.get("ev_lower_bound"),
                row.get("target_edge_adjustment", 0.0), row.get("active_edge_adjustment", 0.0),
                bool(row.get("suppressed")), int(row.get("recovery_streak", 0)),
                json.dumps(row.get("shadow_recovery_metrics") or {}), json.dumps(row.get("reasons") or []),
            ),
        )

    def record_event(self, event_type: str, version: str | None, details: dict) -> bool:
        return self._execute(
            "INSERT INTO q15_learning_events(event_type,version,details_json) VALUES (%s,%s,%s::jsonb)",
            (event_type, version, json.dumps(details or {})),
        )

    def recent_events(self, limit: int = 20) -> list[dict]:
        return self._query(
            "SELECT event_type,version,details_json,created_at FROM q15_learning_events ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def counts(self) -> dict:
        result = {}
        queries = {
            "decisions": "SELECT COUNT(*) AS n FROM q15_decision_events",
            "settled_decisions": "SELECT COUNT(*) AS n FROM q15_decision_events WHERE outcome IN ('win','loss')",
            "shadow_decisions": "SELECT COUNT(*) AS n FROM q15_decision_events WHERE shadow=TRUE",
            "scalps": "SELECT COUNT(*) AS n FROM q15_scalp_decision_events",
            "settled_scalps": "SELECT COUNT(*) AS n FROM q15_scalp_decision_events WHERE exit_timestamp IS NOT NULL",
            "end_predictions": "SELECT COUNT(*) AS n FROM q15_end_predictions",
        }
        for key, sql in queries.items():
            rows = self._query(sql)
            result[key] = int(rows[0].get("n") or 0) if rows else 0
        return result

    def _query(self, sql: str, params=None) -> list[dict]:
        if not self.enabled:
            return []
        try:
            rows = self.store.query(sql, params or ())
            return rows or []
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("LearningStore query failed")
            return []

    def _execute(self, sql: str, params=None) -> bool:
        if not self.enabled:
            return False
        try:
            ok = bool(self.store.execute(sql, params or ()))
            if not ok:
                self.last_error = "store.execute returned false"
            return ok
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("LearningStore execute failed")
            return False
