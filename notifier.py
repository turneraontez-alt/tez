"""Telegram notifier.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from Secrets only. Degrades
gracefully to "disabled" when not configured. The bot token NEVER appears in
logs, API responses, or error messages: every outbound string is redacted.
"""
import os
import logging

import requests
from q15_upgrade.checkpoint_v95 import format_telegram_message as augment_telegram_message  # Q15_V952_FORMATTER
from q15_upgrade.professional_v7 import professionalize_telegram_message  # Q15_V7_REPORTING

logger = logging.getLogger(__name__)


# --- Alert verbosity control (Q15_ALERT_LEVEL) ----------------------------
# Read-only: this only decides whether a fully-formed message is delivered to
# Telegram. It never changes engine decisions, learning, or order surface.
#   all      -> deliver everything (kill switch / legacy behaviour)
#   balanced -> (default) deliver actionable alerts only; mute routine
#               "NO ENTRY YET / NO TRADE / WAIT FOR PRICE / WATCH" checkpoint
#               reports. Keeps ENTRY RECOMMENDED / READY, real entry/paper
#               signals, dip alerts, exits/invalidations, reversals.
_ALERT_ACTIONABLE_MARKERS = (
    "ENTRY RECOMMENDED",
    "ENTRY SIGNAL",
    "PAPER SIGNAL",
    "#1 READY",
    "DIP",
    "EXIT",
    "INVALIDAT",
    "REVERSAL",
    "HOLD",
    # The one post-entry follow-up check (hold / take-profit / avoid / exit) is
    # always actionable — never mute it.
    "FOLLOW-UP",
)
_ALERT_NONACTIONABLE_MARKERS = (
    "NO ENTRY YET",
    "NO TRADE",
    "WAIT FOR PRICE",
    # Watch headers only ("#1 WATCH ..." and "WATCH — <asset>"); narrow forms
    # avoid muting any future non-routine header that merely contains "WATCH".
    "#1 WATCH",
    "WATCH —",
    "WATCH -",
    # The "canonical analysis is not ready" placeholder emitted while the v9.5
    # globals are still empty (the startup window re-opens on every restart). It
    # carries no decision — pure noise to the user — so mute it under balanced.
    # Still delivered under Q15_ALERT_LEVEL=all and always visible in
    # /api/health + /api/q15-v9-5/diagnostics.
    "V9.5 STARTUP",
)
_ALERT_LEVEL_DELIVER_ALL = {"all", "off", "none", "full", "verbose", "everything"}

# Per-checkpoint overrides of Q15_ALERT_LEVEL. When one is set, that checkpoint's
# own level governs delivery of its messages — so e.g. the 10m and 7m checks can
# be delivered ("all") while the 15m check stays muted under the global
# "balanced" level (or vice versa). Unset -> inherit Q15_ALERT_LEVEL (so the
# default behaviour is unchanged). Identification keys off the checkpoint label
# already present in the rendered header (e.g. "10M V9.5 CHECK ...").
_CHECKPOINT_ALERT_LEVEL_ENV = {
    "15M": "Q15_ALERT_LEVEL_15M",
    "10M": "Q15_ALERT_LEVEL_10M",
    "7M": "Q15_ALERT_LEVEL_7M",
}


def _checkpoint_of(header):
    """Return the checkpoint (15M/10M/7M) a rendered header belongs to, else None.

    Only checkpoint messages — those whose header carries a checkpoint label —
    can be governed by a per-checkpoint override. Everything else (dip alerts,
    exits/invalidations, the hourly report, startup pings) keeps the global
    Q15_ALERT_LEVEL. The tokens are mutually non-overlapping substrings."""
    for checkpoint in ("10M", "15M", "7M"):
        if checkpoint in header:
            return checkpoint
    return None

# The canonical hourly performance report (reporting.HourlyReporter.build_report)
# is already fully formatted, complete, and honest. It must NOT be piped through
# the checkpoint-check reformatters (augment_telegram_message -> v95 -> v94 ->
# v93._format_hourly), because that chain rebuilds the report from live decision
# stats and discards every per-segment number (Real alerts, Paper, Last hour,
# loss factors, scalps), surfacing them to the user as zeros / "n/a". The em dash
# header is unique to the canonical report; the reformatters emit "Hourly
# Operational Status" instead, so this never matches an already-mangled message.
_PERF_REPORT_MARKER = "Hourly Report —"


def _is_performance_report(text):
    return _PERF_REPORT_MARKER in str(text or "")


def _alert_level(checkpoint=None):
    """Effective verbosity level. A per-checkpoint override (e.g. for 10M/7M)
    takes precedence over the global Q15_ALERT_LEVEL when set."""
    if checkpoint:
        override = os.environ.get(_CHECKPOINT_ALERT_LEVEL_ENV.get(checkpoint, ""))
        if override and override.strip():
            return override.strip().lower()
    return (os.environ.get("Q15_ALERT_LEVEL", "balanced") or "balanced").strip().lower()


def should_suppress_alert(text, level=None):
    """Decide whether a final, rendered Telegram message should be muted.

    Classification is on the *header* (first non-empty line) of the already
    formatted message — that is exactly what the user sees. Actionable markers
    always win, so a message is only muted when its header is unambiguously a
    routine, non-actionable checkpoint/watch report.

    When ``level`` is not given explicitly it is resolved from the environment,
    honouring a per-checkpoint override (Q15_ALERT_LEVEL_10M / _7M / _15M) keyed
    off the checkpoint label in the header, then falling back to Q15_ALERT_LEVEL.
    """
    header = ""
    for line in str(text or "").splitlines():
        if line.strip():
            header = line.upper()
            break
    if not header:
        return False
    if level is None:
        level = _alert_level(_checkpoint_of(header))
    if level in _ALERT_LEVEL_DELIVER_ALL:
        return False
    if any(marker in header for marker in _ALERT_ACTIONABLE_MARKERS):
        return False
    return any(marker in header for marker in _ALERT_NONACTIONABLE_MARKERS)


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        self.sent_count = 0
        self.last_error = None
        self.last_sent_at = None
        # Telegram message_id of the most recent successful delivery (None if the
        # last send was muted/failed). Lets the official record store proof that a
        # specific prediction was actually delivered, not merely "handled".
        self.last_message_id = None

    def status(self):
        if self.enabled:
            return "configured"
        if self.token and not self.chat_id:
            return "missing_chat_id"
        if self.chat_id and not self.token:
            return "missing_token"
        return "disabled"

    def _redact(self, s):
        """Strip the bot token from any string before it is logged/exposed."""
        if not s:
            return s
        s = str(s)
        if self.token:
            s = s.replace(self.token, "***")
        return s

    def send(self, text):
        """Backward-compatible boolean send: True when the message was handled
        (delivered OR intentionally muted), False on failure/disabled."""
        return self.send_with_result(text)["ok"]

    def send_with_result(self, text):
        """Deliver and report exactly what happened, so callers that keep an
        official record can distinguish a real delivery from a mute or a failure:

            {"ok": bool,          # handled (delivered or muted) -> no retry needed
             "delivered": bool,   # actually reached Telegram (200 + message_id)
             "muted": bool,       # suppressed by alert level (NOT delivered)
             "message_id": int|None}

        ``last_message_id`` is set to the delivered id, or None on mute/failure."""
        self.last_message_id = None
        if not self.enabled:
            return {"ok": False, "delivered": False, "muted": False, "message_id": None}
        if _is_performance_report(text):
            # Deliver the canonical performance report as built. The reformatter
            # chain would otherwise strip its stats down to zeros / "n/a".
            text = str(text or "")
        else:
            text = augment_telegram_message(text)
            text = professionalize_telegram_message(text)
        if should_suppress_alert(text):
            # Muted by Q15_ALERT_LEVEL (or a per-checkpoint override). Report
            # handled so the outbox marks the message durably done (no retry);
            # nothing is sent to Telegram, so it is NOT an official delivery.
            header = next((ln.upper() for ln in str(text or "").splitlines() if ln.strip()), "")
            logger.info("Telegram alert muted (level=%s)", _alert_level(_checkpoint_of(header)))
            return {"ok": True, "delivered": False, "muted": True, "message_id": None}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                self.sent_count += 1
                import time as _t
                self.last_sent_at = _t.time()
                message_id = None
                try:
                    message_id = int((((resp.json() or {}).get("result")) or {}).get("message_id"))
                except (ValueError, TypeError, AttributeError):
                    message_id = None
                self.last_message_id = message_id
                return {"ok": True, "delivered": True, "muted": False, "message_id": message_id}
            # Telegram's error body does not echo the token, but be defensive.
            self.last_error = self._redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
            logger.warning("Telegram send failed: %s", self.last_error)
            return {"ok": False, "delivered": False, "muted": False, "message_id": None}
        except Exception as e:
            # requests exceptions can embed the full URL (with token) -> redact.
            self.last_error = self._redact(str(e))
            logger.warning("Telegram send error: %s", self.last_error)
            return {"ok": False, "delivered": False, "muted": False, "message_id": None}
