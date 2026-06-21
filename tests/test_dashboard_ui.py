"""Dashboard (templates/index.html) rendering & UI-contract tests.

The dashboard is a single static template that renders client-side from
/api/snapshot, so these tests assert the *served* markup contains the structural,
state-handling, accessibility and safe-rendering machinery the cleanup
introduced — and that the route still serves a 200 HTML page with the no-cache
headers the app relies on. They never touch the network or wall clock.
"""
from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class DashboardTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = appmod.app.test_client()
        resp = cls.client.get("/")
        cls.status = resp.status_code
        cls.html = resp.get_data(as_text=True)
        cls.headers = resp.headers

    def test_route_serves_html_200(self):
        self.assertEqual(self.status, 200)
        self.assertIn("text/html", self.headers.get("Content-Type", ""))
        self.assertIn("<!DOCTYPE html>", self.html)

    def test_no_cache_headers_present(self):
        # The live monitor must never be cached by a proxy/browser.
        self.assertIn("no-store", self.headers.get("Cache-Control", ""))

    def test_design_tokens_defined(self):
        # A consistent spacing/radius/type/colour system, not ad-hoc values.
        for token in ("--s1:", "--r-md:", "--fz-base:", "--positive:",
                      "--negative:", "--stale:", "--critical:"):
            self.assertIn(token, self.html, f"missing design token {token}")

    def test_required_asset_and_market_scaffolding(self):
        # The four card modes and the key per-asset fields must be renderable.
        for needle in ("buildLiveScaffold", "buildSimpleScaffold",
                       "renderPrediction", "Order book / spread",
                       "Recent trades", "Reversal confidence components"):
            self.assertIn(needle, self.html)

    def test_state_handling_loading_empty_stale_disconnected(self):
        self.assertIn("Loading markets…", self.html)         # loading
        self.assertIn("Waiting for data…", self.html)        # empty
        self.assertIn("staleLabel", self.html)               # stale per-card
        self.assertIn("Disconnected", self.html)             # disconnected feed
        self.assertIn("Could not load data", self.html)      # hard error

    def test_connection_banner_present(self):
        self.assertIn('id="conn-banner"', self.html)
        self.assertIn("setConnState", self.html)
        # Banner is an assertive live region for screen readers.
        self.assertIn('role="alert"', self.html)

    def test_freshness_and_timezone_clarity(self):
        self.assertIn('data-r="fresh"', self.html)           # per-card stale pill
        self.assertIn("timeZoneName", self.html)             # tz-labelled clock

    def test_accessibility_basics(self):
        self.assertIn(":focus-visible", self.html)           # visible focus
        self.assertIn('type="button"', self.html)            # real button semantics
        self.assertIn("aria-label", self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn("prefers-reduced-motion", self.html)   # motion safety

    def test_responsive_layout(self):
        self.assertIn("@media (max-width: 380px)", self.html)
        self.assertIn("minmax(", self.html)                  # fluid card grid
        self.assertIn("viewport", self.html)

    def test_dynamic_content_is_escaped(self):
        # An esc() helper exists and is applied to feed-derived text (flip-risk
        # lines, error messages) before innerHTML — no raw interpolation of
        # untrusted strings into markup.
        self.assertIn("function esc(", self.html)
        self.assertIn("esc(e.message)", self.html)
        self.assertIn("esc(lines[0])", self.html)

    def test_no_unrendered_template_expressions(self):
        # The template takes no server-side context; ensure no stray Jinja tags
        # leaked into the served page (which would indicate an injection surface).
        self.assertNotIn("{{", self.html)
        self.assertNotIn("{%", self.html)


if __name__ == "__main__":
    unittest.main()
