"""Drift-hypothesis shadow recorder (13M near-strike YES, record-only).

Forward-tests the pre-registered near-strike-YES hypothesis (see HANDOFF): at
the 13M mark, at-the-money alt YES contracts with low flip-risk are underpriced.
It NEVER trades, notifies, or touches any live path. Pure observation: it writes
its own SQLite ledger and exposes a scoreboard with frozen kill/promote bars.

v2 (2026-07-07): the recorder captures a SUPERSET envelope (ask 60-80, every
qualifying candidate per interval, with pick_rank) so that THREE nested books
grade simultaneously from one stream, each against its own frozen bar:

  Book P (primary; pre-registered 2026-07-06, bars UNCHANGED):
    ask 65-73, top-1 per interval by disagreement (pick_rank==1 within 65-73).
    KILL n>=40 if EV<=0 or WR<breakeven; PROMOTE n>=150 if EV>=+2c AND
    Wilson-95 LB > breakeven AND no day >40% of P&L AND >=3 assets.
  Book V (volume expansion; pre-registered 2026-07-07 after a 38-hypothesis
    parallel search + adversarial audit — ask-floor widening to 60 confirmed,
    multi-pick confirmed; the 74-80 ceiling was a train-mirage and is EXCLUDED):
    ask 60-73, ALL qualifying picks. KILL n>=60 if EV<=0 or WR<breakeven;
    PROMOTE n>=150 if EV>=+2c AND Wilson-95 LB > breakeven.
  Book X (diagnostic only, no bar, never tradeable from this data alone):
    ask 74-80 rows — recorded to settle the train-mirage question forward.

Core rule (all books): asset not in {BTC, ETH}, side == YES,
distance_sigma <= 3e-5 (near-strike), flip_probability <= 30 — every one of
these conditions is load-bearing by ablation; dist/fp relaxations were tested
and their marginal cohorts LOSE (see HANDOFF 2026-07-07).

Sizing (2026-07-07, the one lever of 28 tested that raised profit without
touching accuracy): each row records size_weight — 1.5x when disagreement is
above the frozen train-fit high tercile (-0.0917), 0.5x below the low tercile
(-0.1157), else 1.0x. On the tape: +19% total P&L at identical trades; OOS
check (train-fit thresholds on the test half) +9.47 vs +9.26 c/unit. The
scoreboard reports each book's weighted P&L alongside flat.

v3 (2026-07-08): SEPARATE new tracks from the 8-family deep-improvement search,
each verified by adversarial replay (ablation, walk-forward, holiday/day-
concentration controls — see HANDOFF). The three v2 books and their bars are
UNTOUCHED; every v3 track records and grades independently:

  ADD-ON track (drift_addons): when an existing 13M volume-book pick re-passes
    the FULL rule at a later checkpoint (12M..7M: still alt YES, ask 60-73,
    dist<=3e-5, fp<=30), record ONE add-on unit at that checkpoint's ask
    (first re-qualification only). Tape: n=123, +12.14c/add, WR 81.3%, both
    halves and all quarters positive; null-controlled vs unconditional adds
    (+6.94). Bars: KILL n>=40 if EV<=0 or WR<breakeven; PROMOTE n>=120 if
    EV>=+4c AND Wilson-95 LB > breakeven.
  LATE-QUAL track (drift_lq_watch -> drift_latequal): a 13M capture that was
    clean (alt YES, dist<=3e-5, fp<=30) but priced BELOW the 60c floor goes on
    watch; if the market reprices INTO 60-73 by 12M/11M (gates re-checked at
    entry), record an entry at that ask. The original 10M extension is excluded:
    it lost both on the historical tape and again in the fresh forward sample.
    This track remains record-only with a full bar: KILL n>=40 if EV<=0 or
    WR<breakeven; PROMOTE n>=150 if EV>=+2c AND Wilson-95 LB > breakeven.
  SIZING TILTS (columns on drift_picks): spread_weight (sp 3-4c -> 1.5x,
    sp>=5 -> 0.5x, else 1.0x; the 3-4c cohort held ~+14c/pick in every
    sub-period tested) and session_weight (UTC 16-24 -> 1.33x, 8-16 -> 0.75x,
    0-8 -> 0.84x; ordering survives weekday/holiday controls), plus
    stack_weight = clip(spread*session, 0.5, 1.5). Reported alongside flat
    and size_weight; NEVER gate any bar.
  Execution context: spread_cents + depth_contracts are stored per pick so the
  conditional-chase doctrine (pay +1c only when displayed depth < 50) can be
  graded forward.

v4 (2026-07-10): adds a completely separate, prospective NO-mirror research
track. It uses the actual predicted-side NO ask and records only the positive
cohorts from the expanded audit: XRP/HYPE/SOL plus either a 65-69c entry or a
tight (<=2c) spread with BTC also predicting NO. BNB, DOGE, and untagged rows
are intentionally absent. This track never changes the YES books or their bars.

v5 (2026-07-11): the old NO mirror becomes a legacy shadow. Its source table
now captures the candidate envelope for the separately scored NO expansion:
XRP 60-69c, HYPE 60-64c, and DOGE 65-69c. Point-in-time spot-flow/spread
confirmation is applied downstream; this recorder still never sends alerts.

scoreboard()["full_enable_blueprint"] lists every component with its live
status — the assembly list for an eventual promotion: if the shadow is ever
enabled as a live book, it takes ALL components whose bars passed (owner
directive 2026-07-08). Promotion itself stays manual and significance-tested.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Any, Mapping, Sequence

FEATURES_VERSION = "drift-shadow-v5-no-expansion"

# volume-book band ceiling: the tradeable band is 60-73; 74-80 is diagnostic only
_BOOK_HI = 73.0
# add-on / late-qual entry checkpoints, in firing order
_ADDON_INTERVALS = ("12M", "11M", "10M", "9M", "8M", "7M")
_LATEQUAL_INTERVALS = ("12M", "11M")
_NO_EXPANSION_BANDS = {
    "XRP": (60.0, 69.0),
    "HYPE": (60.0, 64.0),
    "DOGE": (65.0, 69.0),
}
_NO_MIRROR_TIGHT_SPREAD = 2.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drift_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    side TEXT NOT NULL,
    ask_cents REAL NOT NULL,
    distance_sigma REAL,
    flip_probability REAL,
    calibrated_yes_probability REAL,
    side_prob REAL,
    disagreement REAL,
    slate_n INTEGER,
    pick_rank INTEGER,
    size_weight REAL,
    spread_cents REAL,
    depth_contracts REAL,
    spread_weight REAL,
    session_weight REAL,
    stack_weight REAL,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key, ticker)
);
CREATE TABLE IF NOT EXISTS drift_addons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    add_interval TEXT NOT NULL,
    ask_cents REAL NOT NULL,
    base_ask_cents REAL,
    distance_sigma REAL,
    flip_probability REAL,
    disagreement REAL,
    spread_cents REAL,
    depth_contracts REAL,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key, ticker)
);
CREATE TABLE IF NOT EXISTS drift_lq_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    ask13_cents REAL NOT NULL,
    UNIQUE(model_version, window_key, ticker)
);
CREATE TABLE IF NOT EXISTS drift_latequal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    entry_interval TEXT NOT NULL,
    ask_cents REAL NOT NULL,
    ask13_cents REAL,
    distance_sigma REAL,
    flip_probability REAL,
    disagreement REAL,
    spread_cents REAL,
    depth_contracts REAL,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key, ticker)
);
CREATE TABLE IF NOT EXISTS drift_no_mirror (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    side TEXT NOT NULL DEFAULT 'NO',
    ask_cents REAL NOT NULL,
    distance_sigma REAL,
    flip_probability REAL,
    calibrated_yes_probability REAL,
    side_prob REAL,
    disagreement REAL,
    spread_cents REAL,
    depth_contracts REAL,
    btc_side TEXT,
    reason_codes TEXT NOT NULL,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key, ticker)
);
"""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _num(v: Any) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def taker_fee_cents(ask: float) -> int:
    p = max(0.0, min(100.0, ask)) / 100.0
    if p <= 0.0 or p >= 1.0:
        return 0
    return int(math.ceil(7.0 * p * (1.0 - p)))


def net_pnl_cents(ask: float, correct: bool) -> float:
    gross = (100.0 - ask) if correct else -ask
    return gross - float(taker_fee_cents(ask))


def wilson_lower(correct: int, n: int, z: float = 1.96) -> float | None:
    if n <= 0:
        return None
    p = correct / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half


class DriftShadow:
    """Record-only forward test of the near-strike-YES drift hypothesis."""

    def __init__(self, db_path: str | None = None):
        self.enabled = _bool("Q15_DRIFT_SHADOW", True)
        self.db_path = db_path or os.environ.get(
            "Q15_DRIFT_SHADOW_DB", "data/q15_drift_shadow_v1.sqlite3")
        self._conn: sqlite3.Connection | None = None
        if not self.enabled:
            return
        try:
            import pathlib
            pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate_v1_locked()
            self._conn.executescript(_SCHEMA)
            self._migrate_columns_locked()
            self._conn.commit()
        except sqlite3.Error:
            self._conn = None
            self.enabled = False

    def _migrate_v1_locked(self) -> None:
        """v1 -> v2: the v1 table had UNIQUE(model_version, window_key) (top-1 only)
        and no pick_rank. Rename it aside so v2's superset schema applies; v1 rows
        stay queryable in drift_picks_v1 (at most ~1 day of data existed)."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='drift_picks'"
        ).fetchone()
        if row and "UNIQUE(model_version, window_key)" in str(row["sql"]) \
                and "ticker" not in str(row["sql"]).split("UNIQUE", 1)[1]:
            self._conn.execute("ALTER TABLE drift_picks RENAME TO drift_picks_v1")

    def _migrate_columns_locked(self) -> None:
        """Earlier v2/v2.x tables lack later columns; add them in place
        (pre-existing rows keep NULL — weights read as 1.0 at scoreboard time)."""
        cols = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(drift_picks)")}
        for name in ("size_weight", "spread_cents", "depth_contracts",
                     "spread_weight", "session_weight", "stack_weight"):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE drift_picks ADD COLUMN {name} REAL")

    # -- rule parameters (frozen defaults; env override for research only) -----
    # v2 records the 60-80 SUPERSET envelope; books P (65-73 top-1) and
    # V (60-73 all) are sliced from the recorded rows at scoreboard time.
    @property
    def ask_lo(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_LO", 60.0)

    @property
    def ask_hi(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_HI", 80.0)

    @property
    def dist_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_DIST_MAX", 3e-5)

    @property
    def flip_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_FLIP_MAX", 30.0)

    @property
    def side(self) -> str:
        return (os.environ.get("Q15_DRIFT_SHADOW_SIDE", "YES") or "YES").upper()

    # sizing terciles: FROZEN from the train half (2026-07-07); env for research only
    @property
    def w_lo_threshold(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_W_LO_T", -0.1157)

    @property
    def w_hi_threshold(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_W_HI_T", -0.0917)

    def size_weight(self, disagreement: float | None) -> float:
        if disagreement is None:
            return 1.0
        if disagreement >= self.w_hi_threshold:
            return 1.5
        if disagreement < self.w_lo_threshold:
            return 0.5
        return 1.0

    # -- v3 sizing tilts (FROZEN 2026-07-08 from the verified tape analysis;
    #    observational only — no bar ever grades on them) ---------------------
    @staticmethod
    def spread_weight(spread_cents: float | None) -> float:
        """3-4c spreads carried ~+14c/pick in every sub-period; >=5c negative
        in both halves; 1-2c (MM-efficient) is the baseline."""
        if spread_cents is None or spread_cents <= 2.0:
            return 1.0
        if spread_cents <= 4.0:
            return 1.5
        return 0.5

    @staticmethod
    def session_weight(hour_utc: int | None) -> float:
        """US evening (16-24 UTC) is the book's rich regime, EU hours (8-16)
        its poor one; ordering survives weekday/holiday controls."""
        if hour_utc is None:
            return 1.0
        if 16 <= hour_utc <= 23:
            return 1.33
        if 8 <= hour_utc <= 15:
            return 0.75
        return 0.84

    @classmethod
    def stack_weight(cls, spread_cents: float | None, hour_utc: int | None) -> float:
        return max(0.5, min(1.5, cls.spread_weight(spread_cents) * cls.session_weight(hour_utc)))

    def _qualifies(self, cap: Mapping[str, Any]) -> bool:
        asset = str(cap.get("asset") or "").upper()
        if asset in {"BTC", "ETH"} or not asset:
            return False
        side = str(cap.get("predicted_side") or "").upper()
        if side != self.side:
            return False
        ask = _num(cap.get("yes_ask_cents"))
        if ask is None or not (self.ask_lo <= ask <= self.ask_hi):
            return False
        dist = _num(cap.get("distance_from_strike"))
        if dist is None or dist > self.dist_max:
            return False
        flip = _num(cap.get("flip_probability"))
        if flip is None or flip > self.flip_max:
            return False
        return True

    @staticmethod
    def _disagreement(cap: Mapping[str, Any]) -> float:
        cal = _num(cap.get("calibrated_yes_probability"))
        ask = _num(cap.get("yes_ask_cents"))
        if cal is None or ask is None:
            return -9.0
        side = str(cap.get("predicted_side") or "").upper()
        side_prob = cal if side == "YES" else 1.0 - cal
        return side_prob - ask / 100.0

    @staticmethod
    def _hour_utc(cap: Mapping[str, Any], fallback_now: float) -> int | None:
        ts = _num(cap.get("captured_at"))
        if ts is None:
            ts = fallback_now
        try:
            return time.gmtime(ts).tm_hour
        except (OverflowError, OSError, ValueError):
            return None

    def _lq_watchable(self, cap: Mapping[str, Any]) -> bool:
        """Clean 13M signal whose ONLY defect is price below the 60c floor —
        the late-qualifier watch condition (v3)."""
        asset = str(cap.get("asset") or "").upper()
        if asset in {"BTC", "ETH"} or not asset:
            return False
        if str(cap.get("predicted_side") or "").upper() != self.side:
            return False
        ask = _num(cap.get("yes_ask_cents"))
        if ask is None or ask >= self.ask_lo:
            return False
        dist = _num(cap.get("distance_from_strike"))
        if dist is None or dist > self.dist_max:
            return False
        flip = _num(cap.get("flip_probability"))
        if flip is None or flip > self.flip_max:
            return False
        return True

    def _no_expansion_tags(
        self,
        cap: Mapping[str, Any],
        *,
        btc_side: str | None,
    ) -> tuple[str, ...]:
        """Return the asset/price envelope tags for the NO expansion.

        Executed-flow confirmation belongs to the strategy adapter because the
        interval ledger does not persist spot trades. This source recorder only
        captures candidates that could pass after point-in-time enrichment.
        """
        asset = str(cap.get("asset") or "").upper()
        band = _NO_EXPANSION_BANDS.get(asset)
        if band is None:
            return ()
        if str(cap.get("predicted_side") or "").upper() != "NO":
            return ()
        ask = _num(cap.get("entry_ask_cents"))
        if ask is None or not (band[0] <= ask <= band[1]):
            return ()
        dist = _num(cap.get("distance_from_strike"))
        if dist is None or dist > self.dist_max:
            return ()
        flip = _num(cap.get("flip_probability"))
        if flip is None or flip > self.flip_max:
            return ()

        spread = _num(cap.get("spread_cents"))
        tight_spread = spread is not None and spread <= _NO_MIRROR_TIGHT_SPREAD
        btc_agrees_no = btc_side == "NO"
        tags = [
            "DRIFT_NO_EXPANSION_CANDIDATE",
            f"{asset}_NO_{int(band[0])}_{int(band[1])}",
        ]
        if tight_spread:
            tags.append("TIGHT_SPREAD")
        if btc_agrees_no:
            tags.append("BTC_AGREES_NO")
        return tuple(tags)

    def observe_window(self, *, model_version: str, window_key: int,
                       close_time: float | None, slate: Sequence[Mapping[str, Any]],
                       now: float) -> bool:
        """Evaluate one 15m interval's 13M slate. Records EVERY qualifying
        candidate in the recording envelope, ranked by disagreement (pick_rank
        1 = best), plus the late-qualifier watch list (clean-but-cheap, v3) and
        the separately filtered NO-mirror research book (v4).
        Idempotent per (window, ticker). Returns True if any new pick row was
        recorded. Never raises."""
        if not self.enabled or self._conn is None:
            return False
        try:
            wrote = False
            quals = [c for c in slate if self._qualifies(c)]
            quals.sort(key=self._disagreement, reverse=True)
            for rank, cand in enumerate(quals, start=1):
                cal = _num(cand.get("calibrated_yes_probability"))
                ask = _num(cand.get("yes_ask_cents"))
                side = str(cand.get("predicted_side") or "").upper()
                dis = self._disagreement(cand)
                sp = _num(cand.get("spread_cents"))
                hour = self._hour_utc(cand, now)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO drift_picks (created_at, model_version, asset,"
                    " ticker, window_key, close_time, side, ask_cents, distance_sigma,"
                    " flip_probability, calibrated_yes_probability, side_prob, disagreement,"
                    " slate_n, pick_rank, size_weight, spread_cents, depth_contracts,"
                    " spread_weight, session_weight, stack_weight, features_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, model_version, str(cand.get("asset")), str(cand.get("ticker")),
                     int(window_key), close_time, side, ask,
                     _num(cand.get("distance_from_strike")), _num(cand.get("flip_probability")),
                     cal, (cal if side == "YES" else 1.0 - cal) if cal is not None else None,
                     dis, len(quals), rank, self.size_weight(dis),
                     sp, _num(cand.get("depth_contracts")),
                     self.spread_weight(sp), self.session_weight(hour),
                     self.stack_weight(sp, hour), FEATURES_VERSION))
                wrote = wrote or cur.rowcount > 0
            # v3: late-qualifier watch list (never a pick; entries recorded only
            # if the market reprices into the band at 12M/11M/10M)
            for cand in slate:
                if not self._lq_watchable(cand):
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO drift_lq_watch (created_at, model_version,"
                    " asset, ticker, window_key, close_time, ask13_cents)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now, model_version, str(cand.get("asset")), str(cand.get("ticker")),
                     int(window_key), close_time, _num(cand.get("yes_ask_cents"))))

            # v5: record the NO expansion candidate envelope. The selected-side
            # entry ask is the real NO ask; downstream adds point-in-time flow.
            btc_side = next((
                str(cand.get("predicted_side") or "").upper()
                for cand in slate
                if str(cand.get("asset") or "").upper() == "BTC"
                and str(cand.get("predicted_side") or "").upper() in {"YES", "NO"}
            ), None)
            for cand in slate:
                tags = self._no_expansion_tags(cand, btc_side=btc_side)
                if not tags:
                    continue
                ask = _num(cand.get("entry_ask_cents"))
                cal = _num(cand.get("calibrated_yes_probability"))
                side_prob = (1.0 - cal) if cal is not None else None
                disagreement = side_prob - ask / 100.0 if side_prob is not None else None
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO drift_no_mirror (created_at, model_version,"
                    " asset, ticker, window_key, close_time, side, ask_cents,"
                    " distance_sigma, flip_probability, calibrated_yes_probability,"
                    " side_prob, disagreement, spread_cents, depth_contracts, btc_side,"
                    " reason_codes, features_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, model_version, str(cand.get("asset")), str(cand.get("ticker")),
                     int(window_key), close_time, "NO", ask,
                     _num(cand.get("distance_from_strike")),
                     _num(cand.get("flip_probability")), cal, side_prob, disagreement,
                     _num(cand.get("spread_cents")), _num(cand.get("depth_contracts")),
                     btc_side, ",".join(tags), FEATURES_VERSION))
                wrote = wrote or cur.rowcount > 0
            self._conn.commit()
            return wrote
        except sqlite3.Error:
            return False

    def observe_checkpoint(self, *, model_version: str, window_key: int, interval: str,
                           close_time: float | None, slate: Sequence[Mapping[str, Any]],
                           now: float) -> int:
        """v3 later-checkpoint hook (12M..7M). Two separate record-only tracks:

        ADD-ON: a candidate that re-passes the FULL rule in the 60-73 band and
        already has a base volume-book pick (13M, ask<=73) gets ONE add-on row
        at this checkpoint's ask (first re-qualification wins via the unique
        key — checkpoints fire chronologically).
        LATE-QUAL (12M/11M only): a watched clean-but-cheap ticker whose
        price repriced INTO 60-73 (gates re-checked here) gets an entry row.
        Idempotent; returns number of new rows; never raises."""
        if not self.enabled or self._conn is None or interval not in _ADDON_INTERVALS:
            return 0
        wrote = 0
        try:
            for cand in slate:
                if not self._qualifies(cand):
                    continue
                ask = _num(cand.get("yes_ask_cents"))
                if ask is None or ask > _BOOK_HI:
                    continue  # add/late-qual band is the tradeable 60-73 only
                ticker = str(cand.get("ticker"))
                base = self._conn.execute(
                    "SELECT ask_cents FROM drift_picks WHERE model_version=? AND"
                    " window_key=? AND ticker=? AND ask_cents<=?",
                    (model_version, int(window_key), ticker, _BOOK_HI)).fetchone()
                dis = self._disagreement(cand)
                common = (now, model_version, str(cand.get("asset")), ticker,
                          int(window_key), close_time, interval, ask,
                          _num(cand.get("distance_from_strike")),
                          _num(cand.get("flip_probability")), dis,
                          _num(cand.get("spread_cents")), _num(cand.get("depth_contracts")),
                          FEATURES_VERSION)
                if base is not None:
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO drift_addons (created_at, model_version,"
                        " asset, ticker, window_key, close_time, add_interval, ask_cents,"
                        " distance_sigma, flip_probability, disagreement, spread_cents,"
                        " depth_contracts, features_version, base_ask_cents)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        common + (float(base["ask_cents"]),))
                    wrote += cur.rowcount
                elif interval in _LATEQUAL_INTERVALS:
                    watch = self._conn.execute(
                        "SELECT ask13_cents FROM drift_lq_watch WHERE model_version=? AND"
                        " window_key=? AND ticker=?",
                        (model_version, int(window_key), ticker)).fetchone()
                    if watch is not None:
                        cur = self._conn.execute(
                            "INSERT OR IGNORE INTO drift_latequal (created_at, model_version,"
                            " asset, ticker, window_key, close_time, entry_interval, ask_cents,"
                            " distance_sigma, flip_probability, disagreement, spread_cents,"
                            " depth_contracts, features_version, ask13_cents)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            common + (float(watch["ask13_cents"]),))
                        wrote += cur.rowcount
            self._conn.commit()
        except sqlite3.Error:
            pass
        return wrote

    def resolve(self, events: Sequence[Mapping[str, Any]] | None, now: float) -> int:
        """Grade recorded picks from the champion's settlement events. Read-only."""
        if not self.enabled or self._conn is None or not events:
            return 0
        graded = 0
        try:
            for ev in events:
                if not isinstance(ev, Mapping):
                    continue
                ticker = ev.get("ticker") or ev.get("contract")
                result = str(ev.get("result") or ev.get("official_result") or "").upper()
                if not ticker or result not in {"YES", "NO"}:
                    continue
                for table in ("drift_picks", "drift_no_mirror"):
                    for row in self._conn.execute(
                            f"SELECT id, side, ask_cents FROM {table} WHERE ticker=?"
                            " AND official_result IS NULL", (str(ticker),)).fetchall():
                        correct = str(row["side"]).upper() == result
                        self._conn.execute(
                            f"UPDATE {table} SET official_result=?, resolved_at=?, correct=?,"
                            " pnl_cents=? WHERE id=?",
                            (result, now, 1 if correct else 0,
                             net_pnl_cents(float(row["ask_cents"]), correct), row["id"]))
                        graded += 1
                # v3 tracks are YES-side by construction: correct iff result YES
                for table in ("drift_addons", "drift_latequal"):
                    for row in self._conn.execute(
                            f"SELECT id, ask_cents FROM {table} WHERE ticker=?"
                            " AND official_result IS NULL", (str(ticker),)).fetchall():
                        correct = result == "YES"
                        self._conn.execute(
                            f"UPDATE {table} SET official_result=?, resolved_at=?, correct=?,"
                            " pnl_cents=? WHERE id=?",
                            (result, now, 1 if correct else 0,
                             net_pnl_cents(float(row["ask_cents"]), correct), row["id"]))
                        graded += 1
            self._conn.commit()
        except sqlite3.Error:
            pass
        return graded

    def _book_rows(self, lo: float, hi: float, top1: bool) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT window_key, asset, ask_cents, disagreement, correct, pnl_cents,"
            " size_weight, created_at FROM drift_picks WHERE official_result IS NOT NULL"
            " AND ask_cents >= ? AND ask_cents <= ?", (lo, hi)).fetchall()
        if not top1:
            return rows
        best: dict[int, sqlite3.Row] = {}
        for r in rows:
            wk = int(r["window_key"])
            cur = best.get(wk)
            if cur is None or (r["disagreement"] or -9) > (cur["disagreement"] or -9):
                best[wk] = r
        return list(best.values())

    def _grade_book(self, rows: list[sqlite3.Row], *, kill_n: int, promote_n: int,
                    concentration_guards: bool, promote_ev: float = 2.0) -> dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {"n_resolved": 0, "status": "empty"}
        correct = sum(1 for r in rows if r["correct"])
        total_pnl = sum(float(r["pnl_cents"]) for r in rows)
        wr = correct / n
        # disagreement-tercile sizing, reported ALONGSIDE flat — kill/promote
        # bars grade the FLAT numbers only (sizing is observational, not a gate)
        weights = [float(r["size_weight"])
                   if ("size_weight" in r.keys() and r["size_weight"] is not None) else 1.0
                   for r in rows]
        weighted_pnl = sum(w * float(r["pnl_cents"]) for w, r in zip(weights, rows))
        total_weight = sum(weights)
        breakeven = sum(r["ask_cents"] + taker_fee_cents(r["ask_cents"]) for r in rows) / n / 100.0
        wlb = wilson_lower(correct, n)
        by_day: dict[str, float] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
            by_day[day] = by_day.get(day, 0.0) + float(r["pnl_cents"])
        max_day_frac = (max((abs(v) for v in by_day.values()), default=0.0) /
                        abs(total_pnl)) if total_pnl else None
        assets = {str(r["asset"]) for r in rows}
        ev = total_pnl / n
        kill = n >= kill_n and (ev <= 0 or wr < breakeven)
        promote = (n >= promote_n and ev >= promote_ev and wlb is not None and wlb > breakeven)
        if concentration_guards:
            promote = promote and (max_day_frac is None or max_day_frac <= 0.40) and len(assets) >= 3
        status = "KILL" if kill else ("PROMOTE" if promote else "ACCRUING")
        return {"n_resolved": n, "status": status, "win_rate": round(wr, 3),
                "breakeven_rate": round(breakeven, 3), "wilson_lb": wlb and round(wlb, 3),
                "ev_cents": round(ev, 2), "total_pnl_cents": round(total_pnl, 0),
                "weighted_pnl_cents": round(weighted_pnl, 1),
                "weighted_ev_per_unit_cents": (round(weighted_pnl / total_weight, 2)
                                               if total_weight else None),
                "n_assets": len(assets),
                "max_day_pnl_frac": max_day_frac and round(max_day_frac, 2)}

    def _track_rows(self, table: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            f"SELECT window_key, asset, ask_cents, disagreement, correct, pnl_cents,"
            f" created_at FROM {table} WHERE official_result IS NOT NULL").fetchall()

    def _tilt_views(self) -> dict[str, Any]:
        """Weighted P&L of the resolved VOLUME-book rows under each recorded
        sizing tilt, alongside flat. Observational only — no bar reads these."""
        rows = self._conn.execute(
            "SELECT pnl_cents, size_weight, spread_weight, session_weight, stack_weight"
            " FROM drift_picks WHERE official_result IS NOT NULL AND ask_cents <= ?",
            (_BOOK_HI,)).fetchall()
        if not rows:
            return {"n_resolved": 0}
        out: dict[str, Any] = {"n_resolved": len(rows),
                               "flat_pnl_cents": round(sum(float(r["pnl_cents"]) for r in rows), 1)}
        for col in ("size_weight", "spread_weight", "session_weight", "stack_weight"):
            w = [float(r[col]) if r[col] is not None else 1.0 for r in rows]
            tot = sum(wi * float(r["pnl_cents"]) for wi, r in zip(w, rows))
            tw = sum(w)
            out[col] = {"weighted_pnl_cents": round(tot, 1),
                        "ev_per_unit_cents": round(tot / tw, 2) if tw else None}
        return out

    def scoreboard(self) -> dict[str, Any]:
        """v2 books plus the v3 tracks, each graded against its own frozen bar
        (see module docstring). The v3 tracks never affect the v2 bars."""
        if not self.enabled or self._conn is None:
            return {"available": False, "enabled": self.enabled}
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM drift_picks WHERE official_result IS NULL").fetchone()[0]
        book_primary = self._grade_book(
            self._book_rows(65.0, 73.0, top1=True),
            kill_n=40, promote_n=150, concentration_guards=True)
        book_volume = self._grade_book(
            self._book_rows(60.0, _BOOK_HI, top1=False),
            kill_n=60, promote_n=150, concentration_guards=False)
        book_addon = self._grade_book(
            self._track_rows("drift_addons"),
            kill_n=40, promote_n=120, concentration_guards=False, promote_ev=4.0)
        book_latequal = self._grade_book(
            self._track_rows("drift_latequal"),
            kill_n=40, promote_n=150, concentration_guards=False)
        book_no_mirror = self._grade_book(
            self._track_rows("drift_no_mirror"),
            kill_n=60, promote_n=150, concentration_guards=False)
        no_mirror_pending = self._conn.execute(
            "SELECT COUNT(*) FROM drift_no_mirror WHERE official_result IS NULL"
        ).fetchone()[0]
        tilts = self._tilt_views()
        return {
            "available": True, "enabled": True, "features_version": FEATURES_VERSION,
            "core_rule": {"envelope_ask": [self.ask_lo, self.ask_hi],
                          "dist_max": self.dist_max, "flip_max": self.flip_max,
                          "side": self.side},
            "sizing": {"w_lo_threshold": self.w_lo_threshold,
                       "w_hi_threshold": self.w_hi_threshold,
                       "weights": [0.5, 1.0, 1.5]},
            "n_pending": int(pending),
            "n_pending_no_mirror": int(no_mirror_pending),
            "book_primary_65_73_top1": book_primary,
            "book_volume_60_73_all": book_volume,
            "book_diag_74_80": self._grade_book(
                self._book_rows(74.0, 80.0, top1=False),
                kill_n=10**9, promote_n=10**9, concentration_guards=False),
            "book_addon_requal": book_addon,
            "book_latequal": book_latequal,
            "book_no_mirror_research": book_no_mirror,
            "tilts_volume_book": tilts,
            "bars": {
                "primary": "KILL n>=40 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be & day<=40% & assets>=3",
                "volume": "KILL n>=60 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be",
                "diag_74_80": "no bar; diagnostic only (train-mirage check)",
                "addon_requal": "KILL n>=40 if EV<=0|WR<be; PROMOTE n>=120 if EV>=4 & WLB>be",
                "latequal": "KILL n>=40 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be",
                "no_mirror_research": "KILL n>=60 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be; manual review required",
            },
            # the assembly list for an eventual full enable (owner directive
            # 2026-07-08): a promoted live book takes every component whose bar
            # passed; promotion itself stays manual + significance-tested.
            "full_enable_blueprint": {
                "base_book_60_73": book_volume.get("status"),
                "primary_65_73_top1": book_primary.get("status"),
                "addon_requal_12m_7m": book_addon.get("status"),
                "latequal_repriced_into_band": book_latequal.get("status"),
                "sizing_tilts_recorded": ["size_weight", "spread_weight",
                                          "session_weight", "stack_weight"],
                "execution_doctrine": "chase +1c only when depth<50; 25-50 contracts/pick comfort, ~100 ceiling",
                "no_mirror_research": "separate prospective book; never auto-promoted or mixed with YES",
            },
        }

    def picks_recorded_at(self, model_version: str, window_key: int,
                          recorded_at: float) -> list[dict[str, Any]]:
        """Rows INSERTED by the observe_window call that ran at ``recorded_at``
        (idempotent re-observations keep their original created_at, so exactly
        the new rows match). Read-only — used by the alert adapter; the
        recorder itself still never notifies."""
        if not self.enabled or self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT * FROM drift_picks WHERE model_version=? AND window_key=?"
                " AND created_at=?",
                (model_version, int(window_key), recorded_at)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def no_mirror_rows_recorded_at(
        self,
        model_version: str,
        window_key: int,
        recorded_at: float,
    ) -> list[dict[str, Any]]:
        """Filtered NO rows inserted by one 13M observation."""
        if not self.enabled or self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT * FROM drift_no_mirror WHERE model_version=? AND window_key=?"
                " AND created_at=? ORDER BY asset, ticker",
                (model_version, int(window_key), recorded_at),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def checkpoint_rows_recorded_at(
        self,
        model_version: str,
        window_key: int,
        interval: str,
        recorded_at: float,
    ) -> list[dict[str, Any]]:
        """Return add-on/late-qual rows inserted by one checkpoint observation."""
        if not self.enabled or self._conn is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            for table, interval_col, record_kind in (
                ("drift_addons", "add_interval", "DRIFT_ADDON_REQUAL"),
                ("drift_latequal", "entry_interval", "DRIFT_LATEQUAL"),
            ):
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE model_version=? AND window_key=?"
                    f" AND {interval_col}=? AND created_at=?",
                    (model_version, int(window_key), str(interval), recorded_at),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["record_kind"] = record_kind
                    out.append(item)
        except sqlite3.Error:
            return []
        return out

    def resolved_events(self) -> list[dict[str, Any]]:
        """Unique settled Drift events used to reconcile the V3 side ledger."""
        if not self.enabled or self._conn is None:
            return []
        events: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            for table in ("drift_picks", "drift_addons", "drift_latequal", "drift_no_mirror"):
                rows = self._conn.execute(
                    f"SELECT model_version, ticker, official_result, resolved_at FROM {table}"
                    " WHERE official_result IN ('YES','NO')"
                ).fetchall()
                for row in rows:
                    key = (str(row["model_version"] or ""), str(row["ticker"] or ""))
                    if not key[1]:
                        continue
                    events[key] = {
                        "model_version": key[0],
                        "ticker": key[1],
                        "official_result": str(row["official_result"]),
                        "resolved_at": _num(row["resolved_at"]),
                    }
        except sqlite3.Error:
            return []
        return list(events.values())

    def health(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if not self.enabled or self._conn is None:
            return {"enabled": self.enabled, "status": "disabled"}
        row = self._conn.execute(
            "SELECT COUNT(*) n, MAX(created_at) latest FROM drift_picks").fetchone()
        latest = _num(row["latest"])
        tracks = {table: int(self._conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("drift_addons", "drift_lq_watch", "drift_latequal",
                          "drift_no_mirror")}
        return {"enabled": True, "rows_written": int(row["n"] or 0),
                "latest_created_at": latest,
                "latest_age_seconds": (now - latest) if latest is not None else None,
                "v3_tracks": tracks,
                "status": "ok" if row["n"] else "empty"}


_singleton: DriftShadow | None = None


def get_recorder() -> DriftShadow | None:
    """Process-wide singleton; None when disabled so the call site is a cheap no-op."""
    global _singleton
    if _singleton is not None:
        return _singleton if _singleton.enabled else None
    _singleton = DriftShadow()
    return _singleton if _singleton.enabled else None


def reset_recorder() -> None:
    global _singleton
    _singleton = None
