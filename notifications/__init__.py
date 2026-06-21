"""Telegram notification layer — all PURE delivery + message/report formatting.

Consolidated here so everything that builds or sends a Telegram message lives in
one place: delivery + suppression (:mod:`notifications.notifier`), the reliable retry
outbox (:mod:`notifications.outbox_v9`), the hourly report (:mod:`notifications.reporting`),
the V9.5 checkpoint/ranked/recap panels (:mod:`notifications.panels_v95`), the
manipulation alert (:mod:`notifications.manipulation_alert`), and alert thresholds
(:mod:`notifications.alert_config`).

This package intentionally stays import-light (no eager submodule imports) so the
notifier ⇄ q15_upgrade formatter chain has no circular-import hazard.

NOTE: the legacy ``format_telegram_message`` reformatter chain still lives in the
FROZEN ``q15_upgrade/checkpoint_v9{1..5}.py`` modules — its Telegram formatting is
welded into frozen decision logic and cannot be moved without editing frozen code.
"""
