"""The TT-Edge repository — every SQL statement in the pipeline lives here.

Dialect support: PostgreSQL (production; psycopg2, ``%s`` params) and SQLite
(default local file, in-memory tests; ``?`` params). Queries are written
once with ``%s`` placeholders and translated for SQLite — keep literal ``%``
out of SQL text. All statements are parameterized; no exceptions.

Conventions:

* Timestamps go in as timezone-aware datetimes, are stored as UTC
  (TIMESTAMPTZ / ISO-8601 TEXT), and come back timezone-aware.
* Money is integer cents. Probabilities/edges are exact decimal strings.
* Idempotent writes use ``INSERT ... ON CONFLICT DO NOTHING`` on the natural
  unique key and report whether the row was won — the caller treats a lost
  claim as "duplicate, skip", which is what makes rescans safe.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tt_edge.freshness import require_aware
from tt_edge.model.calibration import CalibrationFit, CalibrationTransform
from tt_edge.scrapers.events import ParsedEvent
from tt_edge.scrapers.odds import OddsSnapshot

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_DIALECT_TOKENS = {
    "sqlite": {"{PK}": "INTEGER PRIMARY KEY AUTOINCREMENT", "{TS}": "TEXT"},
    "postgres": {"{PK}": "BIGSERIAL PRIMARY KEY", "{TS}": "TIMESTAMPTZ"},
}


class RepoError(RuntimeError):
    """A repository-level invariant failed (not a driver error)."""


def _iso(value: datetime) -> str:
    return require_aware("timestamp", value).isoformat()


def _as_dt(value: Any) -> datetime | None:
    """Normalize a stored timestamp (str from SQLite, datetime from PG)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Postgres TIMESTAMPTZ always returns aware; a naive value means
            # someone stored into a plain TIMESTAMP — treat as UTC.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def connect(database_url: str) -> "TTEdgeRepo":
    """Open a repository on a postgres:// / postgresql:// URL or an SQLite
    path (``:memory:`` for tests). Migrations are NOT auto-applied — call
    :meth:`TTEdgeRepo.apply_migrations` (jobs do this at startup)."""
    if database_url.startswith(("postgres://", "postgresql://")):
        import psycopg2  # local import: sqlite deployments don't need it

        conn = psycopg2.connect(dsn=database_url)
        return TTEdgeRepo(conn, "postgres")
    path = database_url.removeprefix("sqlite:///")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return TTEdgeRepo(conn, "sqlite")


class TTEdgeRepo:
    """One connection, one dialect, every query in the pipeline."""

    def __init__(self, conn: Any, dialect: str) -> None:
        if dialect not in _DIALECT_TOKENS:
            raise RepoError(f"unsupported dialect {dialect!r}")
        self._conn = conn
        self.dialect = dialect

    # ── plumbing ─────────────────────────────────────────────────────────

    def _sql(self, query: str) -> str:
        if self.dialect == "sqlite":
            return query.replace("%s", "?")
        return query

    def _execute(self, query: str, params: tuple = ()) -> Any:
        cur = self._conn.cursor()
        cur.execute(self._sql(query), params)
        return cur

    def _fetchall(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self._execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _fetchone(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._fetchall(query, params)
        return rows[0] if rows else None

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    # ── migrations ───────────────────────────────────────────────────────

    def apply_migrations(self) -> list[str]:
        """Apply pending .sql migrations in name order.

        NOT transactional on SQLite: Python's sqlite3 runs DDL in autocommit,
        so a mid-file failure can leave earlier statements applied without a
        ledger row. Every migration statement must therefore be IDEMPOTENT
        (IF NOT EXISTS / additive) so a retry heals — that is a convention
        this repo enforces in review, stated in the migrations header."""
        tokens = _DIALECT_TOKENS[self.dialect]
        self._execute(
            "CREATE TABLE IF NOT EXISTS tt_schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {row["name"] for row in
                   self._fetchall("SELECT name FROM tt_schema_migrations")}
        ran: list[str] = []
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            ddl = path.read_text(encoding="utf-8")
            for token, replacement in tokens.items():
                ddl = ddl.replace(token, replacement)
            # Drop comment lines BEFORE splitting on ';' — comment prose may
            # contain semicolons (statements themselves must not).
            ddl = "\n".join(line for line in ddl.splitlines()
                            if not line.lstrip().startswith("--"))
            try:
                for statement in ddl.split(";"):
                    if statement.strip():
                        self._execute(statement)
                self._execute(
                    "INSERT INTO tt_schema_migrations (name, applied_at) "
                    "VALUES (%s, %s)",
                    (path.name, datetime.now(timezone.utc).isoformat()))
                self.commit()
            except Exception:
                self.rollback()
                raise
            ran.append(path.name)
        return ran

    # ── players / matches ────────────────────────────────────────────────

    def upsert_player(self, source_player_id: str, name: str,
                      now: datetime) -> None:
        self._execute(
            "INSERT INTO tt_players (source_player_id, name, created_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (source_player_id) DO UPDATE SET name = EXCLUDED.name",
            (source_player_id, name, _iso(now)))
        self.commit()

    def upsert_match(self, event: ParsedEvent, now: datetime) -> None:
        self._execute(
            "INSERT INTO tt_matches (source_event_id, tournament, "
            "home_source_id, away_source_id, home_name, away_name, "
            "start_time, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_event_id) DO UPDATE SET "
            "start_time = EXCLUDED.start_time, status = EXCLUDED.status, "
            "updated_at = EXCLUDED.updated_at",
            (event.source_event_id, event.tournament_name, event.home_id,
             event.away_id, event.home_name, event.away_name,
             _iso(event.start_time), event.status_type or "scheduled",
             _iso(now), _iso(now)))
        self.commit()

    # ── scrape archives ──────────────────────────────────────────────────

    def record_h2h_snapshot(self, match_source_id: str, payload_json: str,
                            fetched_at: datetime) -> None:
        self._execute(
            "INSERT INTO tt_h2h_snapshots (match_source_id, payload, fetched_at) "
            "VALUES (%s, %s, %s)",
            (match_source_id, payload_json, _iso(fetched_at)))
        self.commit()

    def record_form_snapshot(self, player_source_id: str, payload_json: str,
                             fetched_at: datetime) -> None:
        self._execute(
            "INSERT INTO tt_form_snapshots (player_source_id, payload, fetched_at) "
            "VALUES (%s, %s, %s)",
            (player_source_id, payload_json, _iso(fetched_at)))
        self.commit()

    # ── odds ─────────────────────────────────────────────────────────────

    def insert_odds_snapshot(self, snapshot: OddsSnapshot) -> bool:
        """Append one price observation. False = identical capture instant
        already stored (idempotent re-entry)."""
        cur = self._execute(
            "INSERT INTO tt_odds_snapshots (match_source_id, book, "
            "home_price, away_price, captured_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (match_source_id, book, captured_at) DO NOTHING",
            (snapshot.match_source_id, snapshot.book, snapshot.home_price,
             snapshot.away_price, _iso(snapshot.captured_at)))
        self.commit()
        return cur.rowcount == 1

    def odds_history(self, match_source_id: str,
                     book: str | None = None) -> list[OddsSnapshot]:
        """Price observations for a match, oldest first. Pass ``book`` to get
        a single book's series — mixing books would fabricate line movement
        (the scan always filters to the configured book)."""
        if book is None:
            rows = self._fetchall(
                "SELECT match_source_id, book, home_price, away_price, "
                "captured_at FROM tt_odds_snapshots WHERE match_source_id = %s "
                "ORDER BY captured_at ASC, id ASC",
                (match_source_id,))
        else:
            rows = self._fetchall(
                "SELECT match_source_id, book, home_price, away_price, "
                "captured_at FROM tt_odds_snapshots WHERE match_source_id = %s "
                "AND book = %s ORDER BY captured_at ASC, id ASC",
                (match_source_id, book))
        return [OddsSnapshot(match_source_id=row["match_source_id"],
                             book=row["book"],
                             home_price=int(row["home_price"]),
                             away_price=int(row["away_price"]),
                             captured_at=_as_dt(row["captured_at"]))
                for row in rows]

    # ── predictions (calibration corpus) ─────────────────────────────────

    def insert_prediction(self, *, match_source_id: str, market: str,
                          predicted_on: str, model_p_home: str,
                          fair_p_home: str | None, decision: str,
                          reasons_json: str, created_at: datetime) -> bool:
        """One prediction row per match/market/scan-day. False = duplicate."""
        cur = self._execute(
            "INSERT INTO tt_predictions (match_source_id, market, "
            "predicted_on, model_p_home, fair_p_home, decision, reasons, "
            "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (match_source_id, market, predicted_on) DO NOTHING",
            (match_source_id, market, predicted_on, model_p_home, fair_p_home,
             decision, reasons_json, _iso(created_at)))
        self.commit()
        return cur.rowcount == 1

    def ungraded_predictions_with_results(self) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT p.id, p.model_p_home, r.winner_side "
            "FROM tt_predictions p JOIN tt_results r "
            "ON p.match_source_id = r.match_source_id "
            "WHERE p.outcome_home IS NULL")

    def grade_prediction(self, prediction_id: int, outcome_home: int,
                         graded_at: datetime) -> None:
        self._execute(
            "UPDATE tt_predictions SET outcome_home = %s, graded_at = %s "
            "WHERE id = %s",
            (outcome_home, _iso(graded_at), prediction_id))
        self.commit()

    def graded_prediction_pairs(self) -> list[tuple[float, float]]:
        """(model_p_home, outcome_home) for every graded prediction —
        the calibration fit input."""
        rows = self._fetchall(
            "SELECT model_p_home, outcome_home FROM tt_predictions "
            "WHERE outcome_home IS NOT NULL ORDER BY id ASC")
        return [(float(row["model_p_home"]), float(row["outcome_home"]))
                for row in rows]

    # ── recommendations ──────────────────────────────────────────────────

    def insert_recommendation(self, *, match_source_id: str, market: str,
                              side: str, pick_name: str, model_p: str,
                              fair_p: str, edge: str, price_american: int,
                              stake_cents: int, bankroll_cents: int,
                              recommended_at: datetime, recommended_on: str,
                              basis_json: str, data_ages_json: str) -> int | None:
        """Claim the recommendation slot for this match/market/day. Returns
        the new row id, or None when the claim was already taken (duplicate
        rescan — do NOT alert again)."""
        cur = self._execute(
            "INSERT INTO tt_recommendations (match_source_id, market, side, "
            "pick_name, model_p, fair_p, edge, price_american, stake_cents, "
            "bankroll_cents, status, recommended_at, recommended_on, basis, "
            "data_ages) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "'open', %s, %s, %s, %s) "
            "ON CONFLICT (match_source_id, market, recommended_on) DO NOTHING",
            (match_source_id, market, side, pick_name, model_p, fair_p, edge,
             price_american, stake_cents, bankroll_cents, _iso(recommended_at),
             recommended_on, basis_json, data_ages_json))
        self.commit()
        if cur.rowcount != 1:
            return None
        row = self._fetchone(
            "SELECT id FROM tt_recommendations WHERE match_source_id = %s "
            "AND market = %s AND recommended_on = %s",
            (match_source_id, market, recommended_on))
        if row is None:  # pragma: no cover - the insert above just succeeded
            raise RepoError("recommendation vanished after insert")
        return int(row["id"])

    def recommendation_by_key(self, match_source_id: str, market: str,
                              recommended_on: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM tt_recommendations WHERE match_source_id = %s "
            "AND market = %s AND recommended_on = %s",
            (match_source_id, market, recommended_on))
        if row is not None:
            row["recommended_at"] = _as_dt(row["recommended_at"])
            row["alert_delivered_at"] = _as_dt(row["alert_delivered_at"])
        return row

    def mark_alert_delivered(self, recommendation_id: int,
                             delivered_at: datetime) -> None:
        """Send-then-claim: called ONLY after Telegram confirmed delivery."""
        self._execute(
            "UPDATE tt_recommendations SET alert_delivered_at = %s "
            "WHERE id = %s AND alert_delivered_at IS NULL",
            (_iso(delivered_at), recommendation_id))
        self.commit()

    def open_recommendation_count(self) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS n FROM tt_recommendations WHERE status = 'open'")
        return int(row["n"]) if row else 0

    def open_recommendations_with_results(self) -> list[dict[str, Any]]:
        """Open recommendations whose match already has an official result."""
        return self._fetchall(
            "SELECT rec.id, rec.match_source_id, rec.side, rec.pick_name, "
            "rec.price_american, rec.stake_cents, res.winner_side "
            "FROM tt_recommendations rec JOIN tt_results res "
            "ON rec.match_source_id = res.match_source_id "
            "WHERE rec.status = 'open'")

    def settle_recommendation(self, recommendation_id: int, status: str,
                              profit_cents: int, settled_at: datetime) -> bool:
        """Settle exactly once — a second settle attempt is a no-op (the
        WHERE status = 'open' guard makes regrades idempotent)."""
        if status not in ("won", "lost", "void"):
            raise RepoError(f"invalid settlement status {status!r}")
        cur = self._execute(
            "UPDATE tt_recommendations SET status = %s, profit_cents = %s, "
            "settled_at = %s WHERE id = %s AND status = 'open'",
            (status, profit_cents, _iso(settled_at), recommendation_id))
        self.commit()
        return cur.rowcount == 1

    def settle_batch(self, settlements: list[tuple[int, str, int]],
                     settled_at: datetime, *,
                     move_bankroll: bool) -> tuple[list[int], int, int | None]:
        """Settle many recommendations AND move the bankroll in ONE
        transaction, so a crash cannot apply P&L without its settlements (or
        vice versa) — the ``status = 'open'`` guard would otherwise make the
        divergence permanent on rerun.

        ``settlements`` is (recommendation_id, status, profit_cents). Returns
        (settled_ids, applied_profit_cents, new_bankroll_cents). Rows already
        settled are skipped and contribute no P&L. The bankroll moves only by
        the profit of rows settled in THIS call, clamped at zero, and only
        when ``move_bankroll`` and a bankroll row exists."""
        settled_ids: list[int] = []
        applied_profit = 0
        try:
            for recommendation_id, status, profit_cents in settlements:
                if status not in ("won", "lost", "void"):
                    raise RepoError(f"invalid settlement status {status!r}")
                cur = self._execute(
                    "UPDATE tt_recommendations SET status = %s, "
                    "profit_cents = %s, settled_at = %s "
                    "WHERE id = %s AND status = 'open'",
                    (status, profit_cents, _iso(settled_at),
                     recommendation_id))
                if cur.rowcount == 1:
                    settled_ids.append(recommendation_id)
                    applied_profit += profit_cents
            new_bankroll: int | None = None
            if move_bankroll and settled_ids:
                row = self._fetchone(
                    "SELECT cents FROM tt_bankroll WHERE id = 1")
                if row is not None:
                    new_bankroll = max(0, int(row["cents"]) + applied_profit)
                    self._execute(
                        "UPDATE tt_bankroll SET cents = %s, updated_at = %s "
                        "WHERE id = 1",
                        (new_bankroll, _iso(settled_at)))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return settled_ids, applied_profit, new_bankroll

    # ── results ──────────────────────────────────────────────────────────

    def record_result(self, *, match_source_id: str, winner_side: str,
                      home_score: int | None, away_score: int | None,
                      source: str, recorded_at: datetime) -> bool:
        """Record the official result once. False = already recorded."""
        if winner_side not in ("home", "away", "void"):
            raise RepoError(f"invalid winner_side {winner_side!r}")
        cur = self._execute(
            "INSERT INTO tt_results (match_source_id, winner_side, home_score, "
            "away_score, source, recorded_at) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (match_source_id) DO NOTHING",
            (match_source_id, winner_side, home_score, away_score, source,
             _iso(recorded_at)))
        self.commit()
        return cur.rowcount == 1

    # ── bankroll ─────────────────────────────────────────────────────────

    def get_bankroll_cents(self) -> int | None:
        row = self._fetchone("SELECT cents FROM tt_bankroll WHERE id = 1")
        return int(row["cents"]) if row else None

    def set_bankroll_cents(self, cents: int, now: datetime) -> None:
        if cents < 0:
            raise RepoError(f"bankroll cannot be negative; got {cents}")
        self._execute(
            "INSERT INTO tt_bankroll (id, cents, updated_at) "
            "VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET cents = EXCLUDED.cents, "
            "updated_at = EXCLUDED.updated_at",
            (cents, _iso(now)))
        self.commit()

    # ── calibration versions ─────────────────────────────────────────────

    def insert_calibration_version(self, fit: CalibrationFit, *,
                                   method: str, fitted_at: datetime) -> int:
        """Store a fitted transform INACTIVE; returns its version number."""
        row = self._fetchone(
            "SELECT COALESCE(MAX(version), 0) AS v FROM tt_calibration_versions")
        version = int(row["v"]) + 1
        self._execute(
            "INSERT INTO tt_calibration_versions (version, method, intercept, "
            "slope, n_rows, brier_identity, brier_fitted, converged, active, "
            "fitted_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)",
            (version, method, repr(fit.transform.intercept),
             repr(fit.transform.slope), fit.n_rows, repr(fit.brier_identity),
             repr(fit.brier_fitted), 1 if fit.converged else 0,
             _iso(fitted_at)))
        self.commit()
        return version

    def activate_calibration(self, version: int) -> None:
        """Manual promotion: exactly one active version at a time."""
        row = self._fetchone(
            "SELECT version FROM tt_calibration_versions WHERE version = %s",
            (version,))
        if row is None:
            raise RepoError(f"calibration version {version} does not exist")
        self._execute("UPDATE tt_calibration_versions SET active = 0")
        self._execute(
            "UPDATE tt_calibration_versions SET active = 1 WHERE version = %s",
            (version,))
        self.commit()

    def active_calibration(self) -> CalibrationTransform | None:
        row = self._fetchone(
            "SELECT intercept, slope FROM tt_calibration_versions "
            "WHERE active = 1 ORDER BY version DESC")
        if row is None:
            return None
        return CalibrationTransform(intercept=float(row["intercept"]),
                                    slope=float(row["slope"]))
