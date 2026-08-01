"""Sofascore fetching via Playwright XHR interception.

aiscore/sofascore pages are JS-rendered — a plain HTTP fetch returns a stale
CDN snapshot (confirmed: aiscore served a June 24 board on July 18), which is
exactly the failure mode this pipeline exists to kill. So we drive a real
browser and INTERCEPT the frontend's own JSON XHR responses
(``api.sofascore.com/api/v1/...``) instead of parsing DOM: structured,
stable, and every payload gets stamped with ``fetched_at`` at capture time.

Playwright is imported lazily inside :func:`fetch_payloads` — the parsers,
the envelope I/O, and the whole test suite never need a browser. Captured
payloads are saved as ENVELOPE files (kind, url, entity id, fetched_at,
payload) that ``jobs/scan.py --data-dir`` consumes; the envelope is what
carries provenance for the freshness guard.

CLI (the Phase 0 stopgap collection loop)::

    python3 -m tt_edge.scrapers.sofascore --url <sofascore page> \
        --out-dir data/tt_scrape [--wait 12] [--headed]

Respect the source: one page load per invocation, no polling loops in here.
Cache aggressively (envelopes on disk), re-fetch only when the freshness
guard says the data is too old to use.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tt_edge.freshness import require_aware

logger = logging.getLogger(__name__)

BOARD = "board"
H2H = "h2h"
FORM = "form"
ODDS = "odds"
KINDS = (BOARD, H2H, FORM, ODDS)

_API_BASE = "https://api.sofascore.com/api/v1"

_BOARD_RE = re.compile(
    r"api\.sofascore\.com/api/v1/sport/table-tennis/scheduled-events/"
    r"(?P<entity>\d{4}-\d{2}-\d{2})")
_H2H_RE = re.compile(r"api\.sofascore\.com/api/v1/event/(?P<entity>\d+)/h2h/events")
_FORM_RE = re.compile(
    r"api\.sofascore\.com/api/v1/team/(?P<entity>\d+)/events/last/\d+")
_ODDS_RE = re.compile(
    r"api\.sofascore\.com/api/v1/event/(?P<entity>\d+)/odds/\d+/all")


def board_api_url(date_str: str) -> str:
    return f"{_API_BASE}/sport/table-tennis/scheduled-events/{date_str}"


def h2h_api_url(event_id: str) -> str:
    return f"{_API_BASE}/event/{event_id}/h2h/events"


def form_api_url(player_id: str) -> str:
    return f"{_API_BASE}/team/{player_id}/events/last/0"


def odds_api_url(event_id: str, provider_id: int) -> str:
    return f"{_API_BASE}/event/{event_id}/odds/{provider_id}/all"


@dataclass(frozen=True)
class Classified:
    kind: str
    entity_id: str    # board: the date; h2h: the event id; form: the player id


def classify_url(url: str) -> Classified | None:
    """Which pipeline input (if any) a sofascore XHR URL carries."""
    for kind, pattern in ((BOARD, _BOARD_RE), (H2H, _H2H_RE), (FORM, _FORM_RE),
                          (ODDS, _ODDS_RE)):
        match = pattern.search(url)
        if match:
            return Classified(kind=kind, entity_id=match.group("entity"))
    return None


@dataclass(frozen=True)
class Envelope:
    """A captured payload plus the provenance the freshness guard needs."""

    kind: str
    entity_id: str
    url: str
    fetched_at: datetime      # aware UTC
    payload: Any

    def file_name(self) -> str:
        safe_entity = re.sub(r"[^A-Za-z0-9_-]", "_", self.entity_id)
        return f"{self.kind}_{safe_entity}.json"


def save_envelope(envelope: Envelope, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / envelope.file_name()
    body = {
        "kind": envelope.kind,
        "entity_id": envelope.entity_id,
        "url": envelope.url,
        "fetched_at": require_aware("fetched_at", envelope.fetched_at).isoformat(),
        "payload": envelope.payload,
    }
    path.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    return path


class EnvelopeError(ValueError):
    """An envelope file is missing required provenance fields."""


def load_envelope(path: Path) -> Envelope:
    """Load a saved envelope. Raw payloads without provenance are rejected —
    if you hand-save a payload, wrap it with ``--wrap`` (see CLI) so it gets
    an explicit fetched_at instead of a guessed one."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(body, dict) or "payload" not in body:
        raise EnvelopeError(f"{path}: not an envelope (no 'payload' field)")
    kind = body.get("kind")
    fetched_raw = body.get("fetched_at")
    if kind not in KINDS or not isinstance(fetched_raw, str):
        raise EnvelopeError(f"{path}: envelope lacks kind/fetched_at")
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError as exc:
        raise EnvelopeError(f"{path}: bad fetched_at {fetched_raw!r}") from exc
    fetched_at = require_aware("fetched_at", fetched_at)
    return Envelope(kind=kind, entity_id=str(body.get("entity_id", "")),
                    url=str(body.get("url", "")), fetched_at=fetched_at,
                    payload=body["payload"])


def collect_classified(responses: list[tuple[str, Any]],
                       fetched_at: datetime) -> list[Envelope]:
    """Pure core of the interceptor: (url, json) pairs -> envelopes for the
    URLs we recognize. Later duplicates of the same (kind, entity) win — the
    frontend occasionally re-requests with fresher data."""
    by_key: dict[tuple[str, str], Envelope] = {}
    for url, payload in responses:
        classified = classify_url(url)
        if classified is None:
            continue
        by_key[(classified.kind, classified.entity_id)] = Envelope(
            kind=classified.kind, entity_id=classified.entity_id, url=url,
            fetched_at=fetched_at, payload=payload)
    return list(by_key.values())


def fetch_payloads(page_url: str, *, wait_seconds: float = 12.0,
                   headless: bool = True,
                   goto_timeout_ms: int = 45_000,
                   clock: Callable[[], datetime] | None = None) -> list[Envelope]:
    """Load one sofascore page in Chromium and capture recognized XHRs.

    Requires ``playwright`` (``pip install playwright``; this repo's web
    sandbox and the operator host have Chromium pre-installed). Raises
    RuntimeError with an actionable message when the import fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed; `pip install playwright` (Chromium "
            "must be available) or run the scan from saved envelope files"
        ) from exc

    now = clock or (lambda: datetime.now(timezone.utc))
    raw_responses: list[tuple[str, Any]] = []
    # The handler only COLLECTS matching responses — reading the body inside
    # a sync event handler can deadlock Playwright; bodies are fetched after
    # the settle wait, before the browser closes.
    matched: list[Any] = []

    def on_response(response: Any) -> None:
        if classify_url(response.url) is not None:
            matched.append(response)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.on("response", on_response)
            page.goto(page_url, timeout=goto_timeout_ms,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(int(wait_seconds * 1000))
            for response in matched:
                try:
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001 - capture boundary:
                    # one unreadable XHR body must not abort the whole page
                    # capture; it surfaces later as an absent envelope.
                    logger.warning("sofascore: unreadable XHR body %s: %s",
                                   response.url, exc)
                    continue
                raw_responses.append((response.url, payload))
        finally:
            browser.close()

    envelopes = collect_classified(raw_responses, now())
    logger.info("sofascore: captured %d payload(s) from %s",
                len(envelopes), page_url)
    return envelopes


def fresh_entity_ids(out_dir: Path, kind: str, max_age_s: float,
                     now: datetime) -> set[str]:
    """Entity ids of cached envelopes of ``kind`` younger than ``max_age_s``
    — the bundle fetcher skips refetching those (H2H/form barely move
    intra-day; being polite to the source is part of the design)."""
    fresh: set[str] = set()
    for path in out_dir.glob(f"{kind}_*.json"):
        try:
            envelope = load_envelope(path)
        except EnvelopeError:
            continue
        age = (now - envelope.fetched_at).total_seconds()
        if envelope.kind == kind and 0 <= age < max_age_s:
            fresh.add(envelope.entity_id)
    return fresh


def fetch_bundle(dates: list[str], out_dir: Path, *,
                 tournament_keyword: str, provider_id: int,
                 max_matches: int, request_gap_s: float,
                 h2h_refresh_s: float, headless: bool = True,
                 clock: Callable[[], datetime] | None = None) -> list[Envelope]:
    """The Phase 1 collection loop: everything one scan cycle needs.

    Loads one sofascore page for a real browser fingerprint, then issues
    browser-context API GETs (``page.request`` carries the fingerprint —
    plain HTTP gets 403): per date the board, and per upcoming TT Elite
    match its H2H, both players' form, and the odds. Fresh cached H2H/form
    envelopes (younger than ``h2h_refresh_s``) are NOT refetched; boards and
    odds always are. Every payload is saved to ``out_dir`` as an envelope
    and also returned.

    Failures are per-request: one bad response is logged and skipped, the
    bundle continues (the freshness guard downstream decides what the gaps
    mean).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed; `pip install playwright` on the "
            "host that runs the autoscan") from exc

    from tt_edge.scrapers.board import parse_board_payload

    now = clock or (lambda: datetime.now(timezone.utc))
    out_dir.mkdir(parents=True, exist_ok=True)
    envelopes: list[Envelope] = []

    # A pinned @playwright version can disagree with the machine's browser
    # build; PLAYWRIGHT_CHROMIUM_EXECUTABLE points at an existing Chromium
    # binary instead of downloading one (e.g. /opt/pw-browsers/chromium in
    # the Claude web sandbox).
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless,
                                             executable_path=executable)
        try:
            page = browser.new_page()
            page.goto("https://www.sofascore.com/table-tennis",
                      timeout=45_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2_000)

            def api_get(url: str) -> Any | None:
                try:
                    response = page.request.get(
                        url, headers={"Accept": "application/json"})
                    if response.status != 200:
                        logger.warning("bundle: HTTP %s for %s",
                                       response.status, url)
                        return None
                    return response.json()
                except Exception as exc:  # noqa: BLE001 - per-request
                    # boundary: one failed GET must not abort the cycle.
                    logger.warning("bundle: request failed %s: %s", url, exc)
                    return None
                finally:
                    page.wait_for_timeout(int(request_gap_s * 1000))

            def capture(kind: str, entity_id: str, url: str) -> Any | None:
                payload = api_get(url)
                if payload is None:
                    return None
                envelope = Envelope(kind=kind, entity_id=entity_id, url=url,
                                    fetched_at=now(), payload=payload)
                save_envelope(envelope, out_dir)
                envelopes.append(envelope)
                return payload

            fresh_h2h = fresh_entity_ids(out_dir, H2H, h2h_refresh_s, now())
            fresh_form = fresh_entity_ids(out_dir, FORM, h2h_refresh_s, now())

            for date_str in dates:
                board_payload = capture(BOARD, date_str,
                                        board_api_url(date_str))
                if board_payload is None:
                    continue
                board = parse_board_payload(
                    board_payload, fetched_at=now(),
                    tournament_keyword=tournament_keyword)
                upcoming = list(board.matches)[:max_matches]
                logger.info("bundle %s: %d upcoming TT match(es)",
                            date_str, len(upcoming))
                for event in upcoming:
                    if event.source_event_id not in fresh_h2h:
                        capture(H2H, event.source_event_id,
                                h2h_api_url(event.source_event_id))
                    for player_id in (event.home_id, event.away_id):
                        if player_id not in fresh_form:
                            capture(FORM, player_id, form_api_url(player_id))
                            fresh_form.add(player_id)  # shared opponents dedup
                    capture(ODDS, event.source_event_id,
                            odds_api_url(event.source_event_id, provider_id))
        finally:
            browser.close()
    logger.info("bundle: captured %d payload(s) across %d date(s)",
                len(envelopes), len(dates))
    return envelopes


def _wrap_file(raw_path: Path, kind: str, entity_id: str,
               fetched_at: datetime, out_dir: Path) -> Path:
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    envelope = Envelope(kind=kind, entity_id=entity_id,
                        url=f"manual:{raw_path.name}", fetched_at=fetched_at,
                        payload=payload)
    return save_envelope(envelope, out_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.scrapers.sofascore",
        description="Capture sofascore XHR payloads into envelope files.")
    parser.add_argument("--url", action="append", default=[],
                        help="sofascore page URL to load (repeatable)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=12.0,
                        help="seconds to let XHRs settle after page load")
    parser.add_argument("--headed", action="store_true",
                        help="run the browser headed (debugging)")
    parser.add_argument("--wrap", type=Path,
                        help="wrap a hand-saved raw payload file instead of fetching")
    parser.add_argument("--kind", choices=KINDS,
                        help="kind for --wrap")
    parser.add_argument("--entity-id", default="",
                        help="entity id for --wrap (event/player id or date)")
    parser.add_argument("--fetched-at",
                        help="ISO timestamp for --wrap (default: now UTC)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.wrap:
        if not args.kind:
            parser.error("--wrap requires --kind")
        fetched_at = (datetime.fromisoformat(args.fetched_at)
                      if args.fetched_at else datetime.now(timezone.utc))
        path = _wrap_file(args.wrap, args.kind, args.entity_id, fetched_at,
                          args.out_dir)
        print(f"wrapped -> {path}")
        return 0

    if not args.url:
        parser.error("provide --url (repeatable) or --wrap")
    total = 0
    for page_url in args.url:
        for envelope in fetch_payloads(page_url, wait_seconds=args.wait,
                                       headless=not args.headed):
            path = save_envelope(envelope, args.out_dir)
            print(f"{envelope.kind}[{envelope.entity_id}] -> {path}")
            total += 1
    print(f"captured {total} payload(s)")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
