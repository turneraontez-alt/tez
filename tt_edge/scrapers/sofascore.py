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

_BOARD_RE = re.compile(
    r"api\.sofascore\.com/api/v1/sport/table-tennis/scheduled-events/"
    r"(?P<entity>\d{4}-\d{2}-\d{2})")
_H2H_RE = re.compile(r"api\.sofascore\.com/api/v1/event/(?P<entity>\d+)/h2h/events")
_FORM_RE = re.compile(
    r"api\.sofascore\.com/api/v1/team/(?P<entity>\d+)/events/last/\d+")


@dataclass(frozen=True)
class Classified:
    kind: str
    entity_id: str    # board: the date; h2h: the event id; form: the player id


def classify_url(url: str) -> Classified | None:
    """Which pipeline input (if any) a sofascore XHR URL carries."""
    for kind, pattern in ((BOARD, _BOARD_RE), (H2H, _H2H_RE), (FORM, _FORM_RE)):
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
    if kind not in (BOARD, H2H, FORM) or not isinstance(fetched_raw, str):
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
    parser.add_argument("--kind", choices=(BOARD, H2H, FORM),
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
