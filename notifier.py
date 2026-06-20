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
        if not self.enabled:
            return False
        if _is_performance_report(text):
            # Deliver the canonical performance report as built. The reformatter
            # chain would otherwise strip its stats down to zeros / "n/a".
            text = str(text or "")
        else:
            text = augment_telegram_message(text)
            text = professionalize_telegram_message(text)
        if should_suppress_alert(text):
            # Muted by Q15_ALERT_LEVEL (or a per-checkpoint override). Report
            # success so the outbox marks the message durably handled (no retry);
            # nothing is sent to Telegram.
            header = next((ln.upper() for ln in str(text or "").splitlines() if ln.strip()), "")
            logger.info("Telegram alert muted (level=%s)", _alert_level(_checkpoint_of(header)))
            return True
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
                return True
            # Telegram's error body does not echo the token, but be defensive.
            self.last_error = self._redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
            logger.warning("Telegram send failed: %s", self.last_error)
            return False
        except Exception as e:
            # requests exceptions can embed the full URL (with token) -> redact.
            self.last_error = self._redact(str(e))
            logger.warning("Telegram send error: %s", self.last_error)
            return False
