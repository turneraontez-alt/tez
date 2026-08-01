"""Persistence: portable DDL under ``migrations/`` and the repository in
:mod:`repo`. Runs on PostgreSQL (production, shares the Q15 database — all
tables are ``tt_``-prefixed) or SQLite (default local file / in-memory
tests). Timestamps are stored as UTC and returned timezone-aware; money is
integer cents; probabilities are exact decimal strings."""
