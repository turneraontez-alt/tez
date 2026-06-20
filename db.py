"""Postgres-backed signal/alert persistence.

Durable across redeploys (Replit built-in PostgreSQL). The schema is managed
at publish time (no startup DDL here): this module only reads and writes rows.
If DATABASE_URL is absent, the store degrades gracefully to disabled and the
rest of the app keeps running.
"""
import os
import logging
import threading

logger = logging.getLogger(__name__)

try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    from psycopg2.extras import RealDictCursor, Json
    _HAVE_PG = True
except Exception:  # pragma: no cover - import guard
    _HAVE_PG = False


_INSERT_COLS = [
    "asset", "ticker", "market_close", "side", "state", "signal_type",
    "edge", "fair_prob", "break_even", "entry_ask", "fee", "spread",
    "confirmations", "confirmation_count", "data_age", "seconds_remaining",
    "target_distance_pct", "sent", "suppressed_reason", "correlation_group",
    "is_alternative", "required_edge", "learning_adjustment", "win_prob",
]


class SignalStore:
    def __init__(self):
        self.pool = None
        self.enabled = False
        self._lock = threading.Lock()
        url = os.environ.get("DATABASE_URL")
        if not url:
            logger.warning("DATABASE_URL not set; signal persistence disabled")
            return
        if not _HAVE_PG:
            logger.warning("psycopg2 unavailable; signal persistence disabled")
            return
        try:
            self.pool = ThreadedConnectionPool(1, 8, dsn=url)
            self.enabled = True
            logger.info("SignalStore connected to Postgres")
        except Exception as e:
            logger.error(f"DB pool init failed: {e}")

    # -- connection helpers ---------------------------------------------
    def _conn(self):
        conn = self.pool.getconn()
        # Every method here runs exactly one statement (one execute, then
        # commit), so there is no multi-statement transaction to preserve.
        # autocommit removes the separate COMMIT network round-trip, which
        # roughly halves Postgres round-trips on the hot per-asset cycle path
        # (v91 pre-enrich does ~5 queries/asset/cycle, plus settlement
        # reconcile). Pooled connections are returned clean, so enabling it is
        # safe; commit()/rollback() become harmless no-ops under autocommit.
        if not conn.autocommit:
            conn.autocommit = True
        return conn

    def _release(self, conn):
        try:
            self.pool.putconn(conn)
        except Exception:
            pass

    # -- writes ---------------------------------------------------------
    def insert_signal(self, rec):
        """Insert a signal row. rec keys map to _INSERT_COLS. Returns id|None."""
        if not self.enabled:
            return None
        vals = []
        for c in _INSERT_COLS:
            v = rec.get(c)
            if c == "confirmations" and v is not None:
                v = Json(v)
            elif c in ("is_alternative", "sent") and v is None:
                v = False  # NOT NULL columns; never insert SQL NULL
            vals.append(v)
        placeholders = ", ".join(["%s"] * len(_INSERT_COLS))
        cols = ", ".join(_INSERT_COLS)
        sql = f"INSERT INTO signals ({cols}) VALUES ({placeholders}) RETURNING id"
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(sql, vals)
                sid = cur.fetchone()[0]
            conn.commit()
            return sid
        except Exception as e:
            logger.error(f"insert_signal failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return None
        finally:
            if conn:
                self._release(conn)

    def update_outcome(self, signal_id, outcome, realized_return):
        if not self.enabled:
            return False
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE signals SET outcome=%s, realized_return=%s, "
                    "settled_at=NOW() WHERE id=%s",
                    (outcome, realized_return, signal_id),
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_outcome failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        finally:
            if conn:
                self._release(conn)

    def execute(self, sql, params=None):
        """Run a write statement (no result set). Returns True on success.

        Use this for UPDATE/DELETE/INSERT-without-RETURNING; `query` is SELECT
        only (it calls fetchall and would raise "no results to fetch" here).
        """
        if not self.enabled:
            return False
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"execute failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        finally:
            if conn:
                self._release(conn)

    # -- reads ----------------------------------------------------------
    def query(self, sql, params=None):
        """Run a read query and return a list of dict rows ([] on failure)."""
        if not self.enabled:
            return []
        conn = None
        try:
            conn = self._conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params or ())
                rows = [dict(r) for r in cur.fetchall()]
            conn.commit()  # close the read txn cleanly
            return rows
        except Exception as e:
            logger.error(f"query failed: {e}")
            if conn:
                try:
                    conn.rollback()  # don't return an aborted txn to the pool
                except Exception:
                    pass
            return []
        finally:
            if conn:
                self._release(conn)

    def recent_alerts(self, limit=100):
        return self.query(
            "SELECT id, ts, asset, ticker, side, state, signal_type, edge, "
            "fair_prob, entry_ask, fee, spread, confirmation_count, sent, "
            "suppressed_reason, correlation_group, is_alternative, outcome, "
            "realized_return, settled_at, seconds_remaining "
            "FROM signals ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    def pending_settlements(self, limit=600):
        """All pending settlement-held directional signals (sent OR not).

        Includes un-sent WATCH/CANDIDATE rows so the bot learns 24/7 from
        hypothetical ("paper") entries even when no alert was delivered. SCALP
        signals are excluded — they resolve on an intra-window take-profit, not
        on the market's settlement, and are reconciled by the scalp engine.
        """
        return self.query(
            "SELECT id, ticker, side, entry_ask, fee, state FROM signals "
            "WHERE outcome = 'pending' AND ticker IS NOT NULL "
            "AND side IN ('YES','NO') "
            "AND state IN ('ENTRY CONFIRMED','ENTRY CANDIDATE','WATCH') "
            "ORDER BY ticker, ts ASC LIMIT %s",
            (limit,),
        )

    def count_alerts(self):
        rows = self.query("SELECT COUNT(*) AS n FROM signals WHERE sent = TRUE")
        return int(rows[0]["n"]) if rows else 0

    def entry_record(self):
        """Win/loss tally for entry alerts actually sent to Telegram.

        Counts only settled (win/loss) entry signals that were delivered, so
        the figure reflects the bot's real, public track record.
        """
        rows = self.query(
            "SELECT outcome, COUNT(*) AS n FROM signals "
            "WHERE sent = TRUE AND state IN ('ENTRY CONFIRMED', 'ENTRY CANDIDATE') "
            "AND outcome IN ('win', 'loss') GROUP BY outcome"
        )
        wins = losses = 0
        for r in rows:
            if r["outcome"] == "win":
                wins = int(r["n"])
            elif r["outcome"] == "loss":
                losses = int(r["n"])
        total = wins + losses
        return {
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_rate": (wins / total) if total else None,
        }

    # -- learning support ----------------------------------------------
    def update_loss_reasons(self, signal_id, reasons):
        """Attach a JSON list of attributed loss reasons to a settled signal."""
        if not self.enabled:
            return False
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE signals SET loss_reasons=%s WHERE id=%s",
                    (Json(reasons), signal_id),
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"update_loss_reasons failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        finally:
            if conn:
                self._release(conn)

    def representative_settled(self, limit=4000):
        """One settled hypothetical entry per (ticker, side): the earliest
        directional signal for that market+side that has resolved win/loss.

        This is the learning corpus — it counts each market-direction once
        (not once per flicker), so a noisy market can't dominate the stats.
        """
        return self.query(
            "SELECT DISTINCT ON (ticker, side) id, asset, ticker, side, "
            "signal_type, state, edge, fair_prob, win_prob, entry_ask, fee, "
            "seconds_remaining, outcome, realized_return, sent, ts "
            "FROM signals "
            "WHERE side IN ('YES','NO') AND outcome IN ('win','loss') "
            "AND ticker IS NOT NULL "
            "AND state IN ('ENTRY CONFIRMED','ENTRY CANDIDATE','WATCH') "
            "ORDER BY ticker, side, ts ASC LIMIT %s",
            (limit,),
        )

    def upsert_learning_param(self, seg):
        """Insert/replace a learning_params row from a dict."""
        if not self.enabled:
            return False
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO learning_params (segment_key, dimension, n, "
                    "wins, predicted, posterior, realized, edge_adjustment, "
                    "suppressed, reasons, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW()) "
                    "ON CONFLICT (segment_key) DO UPDATE SET "
                    "dimension=EXCLUDED.dimension, n=EXCLUDED.n, "
                    "wins=EXCLUDED.wins, predicted=EXCLUDED.predicted, "
                    "posterior=EXCLUDED.posterior, realized=EXCLUDED.realized, "
                    "edge_adjustment=EXCLUDED.edge_adjustment, "
                    "suppressed=EXCLUDED.suppressed, reasons=EXCLUDED.reasons, "
                    "updated_at=NOW()",
                    (
                        seg["segment_key"], seg.get("dimension"), seg.get("n", 0),
                        seg.get("wins", 0), seg.get("predicted"), seg.get("posterior"),
                        seg.get("realized"), seg.get("edge_adjustment", 0),
                        bool(seg.get("suppressed", False)), Json(seg.get("reasons")),
                    ),
                )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"upsert_learning_param failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        finally:
            if conn:
                self._release(conn)

    def load_learning_params(self):
        return self.query(
            "SELECT segment_key, dimension, n, wins, predicted, posterior, "
            "realized, edge_adjustment, suppressed, reasons, updated_at "
            "FROM learning_params"
        )

    def claim_event(self, event_key):
        """Atomically claim a one-shot event across instances (dev + prod share
        one DB and one Telegram chat). Returns True only for the instance that
        wins the claim, so an alert/report is delivered exactly once.
        """
        if not self.enabled:
            return True  # no DB -> single instance, always allowed to send
        conn = None
        try:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sent_events (event_key) VALUES (%s) "
                    "ON CONFLICT (event_key) DO NOTHING RETURNING event_key",
                    (event_key,),
                )
                won = cur.fetchone() is not None
            conn.commit()
            return won
        except Exception as e:
            logger.error(f"claim_event failed: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        finally:
            if conn:
                self._release(conn)
