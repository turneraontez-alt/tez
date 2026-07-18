"""Scrapers: pure sofascore payload parsers (:mod:`board`, :mod:`h2h`,
:mod:`form`), odds types (:mod:`odds`) and the Playwright XHR-intercept
fetcher (:mod:`sofascore`, lazy import — the parsers never need a browser).

Every parsed snapshot carries ``fetched_at`` (timezone-aware UTC) so the
freshness guard can reject stale payloads downstream. Parsers are defensive:
a malformed individual event is skipped and counted, a malformed top-level
payload raises :class:`~tt_edge.scrapers.events.ParseError`.
"""
