-- TT-Edge initial schema. Portable across PostgreSQL and SQLite via two
-- tokens the migration runner substitutes per dialect:
--   {PK} -> BIGSERIAL PRIMARY KEY (postgres) | INTEGER PRIMARY KEY AUTOINCREMENT (sqlite)
--   {TS} -> TIMESTAMPTZ (postgres)           | TEXT (sqlite, ISO-8601 UTC)
-- Booleans are INTEGER 0/1 everywhere; money is integer cents; probabilities
-- and edges are exact decimal TEXT. Statements are ;-separated (no ; inside
-- any statement).

CREATE TABLE IF NOT EXISTS tt_players (
    id {PK},
    source_player_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at {TS} NOT NULL
);

CREATE TABLE IF NOT EXISTS tt_matches (
    id {PK},
    source_event_id TEXT NOT NULL UNIQUE,
    tournament TEXT NOT NULL,
    home_source_id TEXT NOT NULL,
    away_source_id TEXT NOT NULL,
    home_name TEXT NOT NULL,
    away_name TEXT NOT NULL,
    start_time {TS} NOT NULL,
    status TEXT NOT NULL,
    created_at {TS} NOT NULL,
    updated_at {TS} NOT NULL
);

-- Raw scraped payload archives (audit + replay; payload is JSON text).
CREATE TABLE IF NOT EXISTS tt_h2h_snapshots (
    id {PK},
    match_source_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at {TS} NOT NULL
);

CREATE TABLE IF NOT EXISTS tt_form_snapshots (
    id {PK},
    player_source_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at {TS} NOT NULL
);

-- Append-only price history (enables the fix-risk movement guard now and
-- closing-line-value analysis later). Idempotent per capture instant.
CREATE TABLE IF NOT EXISTS tt_odds_snapshots (
    id {PK},
    match_source_id TEXT NOT NULL,
    book TEXT NOT NULL,
    home_price INTEGER NOT NULL,
    away_price INTEGER NOT NULL,
    captured_at {TS} NOT NULL,
    UNIQUE (match_source_id, book, captured_at)
);

CREATE INDEX IF NOT EXISTS tt_odds_snapshots_match_idx
    ON tt_odds_snapshots (match_source_id, captured_at);

-- EVERY analyzed match gets a prediction row (recommended or not) — this is
-- the unbiased calibration corpus. One row per match/market/scan-day.
CREATE TABLE IF NOT EXISTS tt_predictions (
    id {PK},
    match_source_id TEXT NOT NULL,
    market TEXT NOT NULL,
    predicted_on TEXT NOT NULL,
    model_p_home TEXT NOT NULL,
    fair_p_home TEXT,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    created_at {TS} NOT NULL,
    outcome_home INTEGER,
    graded_at {TS},
    UNIQUE (match_source_id, market, predicted_on)
);

-- Alerts actually recommended. The UNIQUE key is the duplicate-alert
-- idempotency claim: a rescan the same day cannot re-alert the same market.
CREATE TABLE IF NOT EXISTS tt_recommendations (
    id {PK},
    match_source_id TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    pick_name TEXT NOT NULL,
    model_p TEXT NOT NULL,
    fair_p TEXT NOT NULL,
    edge TEXT NOT NULL,
    price_american INTEGER NOT NULL,
    stake_cents INTEGER NOT NULL,
    bankroll_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    recommended_at {TS} NOT NULL,
    recommended_on TEXT NOT NULL,
    alert_delivered_at {TS},
    basis TEXT NOT NULL,
    data_ages TEXT NOT NULL,
    settled_at {TS},
    profit_cents INTEGER,
    UNIQUE (match_source_id, market, recommended_on)
);

CREATE TABLE IF NOT EXISTS tt_results (
    id {PK},
    match_source_id TEXT NOT NULL UNIQUE,
    winner_side TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    source TEXT NOT NULL,
    recorded_at {TS} NOT NULL
);

-- Single-row bankroll (cents). Updated only by grade.py / the bankroll CLI —
-- never a constant in code.
CREATE TABLE IF NOT EXISTS tt_bankroll (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cents INTEGER NOT NULL,
    updated_at {TS} NOT NULL
);

-- Versioned calibration transforms (same pattern as q15_calibration_versions):
-- fitted rows are stored INACTIVE with their evidence; promotion is a manual,
-- separate step.
CREATE TABLE IF NOT EXISTS tt_calibration_versions (
    id {PK},
    version INTEGER NOT NULL UNIQUE,
    method TEXT NOT NULL,
    intercept TEXT NOT NULL,
    slope TEXT NOT NULL,
    n_rows INTEGER NOT NULL,
    brier_identity TEXT NOT NULL,
    brier_fitted TEXT NOT NULL,
    converged INTEGER NOT NULL,
    active INTEGER NOT NULL,
    fitted_at {TS} NOT NULL
);
