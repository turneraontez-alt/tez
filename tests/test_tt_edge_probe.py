"""The diagnosis surface: .env bootstrap, Telegram source tagging, and the
autoscan --probe health check."""
from datetime import timedelta

from tests import tt_edge_fixtures as fx
from tt_edge.alerts.telegram import TTEdgeTelegram
from tt_edge.envfile import load_env_file
from tt_edge.jobs.autoscan import run_probe
from tt_edge.scrapers import sofascore


class TestEnvFile:
    def test_loads_values_without_overriding_environ(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "TT_EDGE_TELEGRAM_BOT_TOKEN=abc:123\n"
            "export TT_EDGE_TELEGRAM_CHAT_ID='chat-9'\n"
            'TT_EDGE_MIN_EDGE="0.06"\n'
            "BROKEN LINE\n")
        environ = {"TT_EDGE_MIN_EDGE": "0.05"}      # real env wins
        loaded = load_env_file(env_file, environ)
        assert loaded == 2
        assert environ["TT_EDGE_TELEGRAM_BOT_TOKEN"] == "abc:123"
        assert environ["TT_EDGE_TELEGRAM_CHAT_ID"] == "chat-9"
        assert environ["TT_EDGE_MIN_EDGE"] == "0.05"

    def test_missing_file_is_quietly_zero(self, tmp_path):
        assert load_env_file(tmp_path / "nope.env", {}) == 0


class TestTelegramSource:
    def test_sources_are_tagged(self):
        assert TTEdgeTelegram.from_config(fx.make_config()).source == "dedicated"
        fallback = fx.make_config(telegram_token=None, telegram_chat_id="",
                                  q15_telegram_token="t",
                                  q15_telegram_chat_id="c")
        assert TTEdgeTelegram.from_config(fallback).source == "q15_fallback"
        muted = fx.make_config(telegram_token=None, telegram_chat_id="",
                               telegram_fallback=False)
        assert TTEdgeTelegram.from_config(muted).source == "unconfigured"


class TestProbe:
    def _data_dir(self, tmp_path, board_age=timedelta(minutes=4)):
        env = fx.scenario_envelopes(board_age=board_age)
        sofascore.save_envelope(env["board"], tmp_path)
        sofascore.save_envelope(env["h2h"][fx.MATCH_ID], tmp_path)
        for e in env["form"].values():
            sofascore.save_envelope(e, tmp_path)
        odds_payload = {"markets": [{
            "marketName": "Full time", "isLive": False,
            "choices": [{"name": "1", "fractionalValue": "4/5"},
                        {"name": "2", "fractionalValue": "21/20"}]}]}
        sofascore.save_envelope(
            fx.envelope(sofascore.ODDS, fx.MATCH_ID, odds_payload,
                        fx.NOW - timedelta(seconds=40)), tmp_path)
        return tmp_path

    def test_healthy_chain_reports_ready(self, tmp_path, capsys):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        code = run_probe(repo=repo, config=fx.make_config(),
                         data_dir=self._data_dir(tmp_path), sender=sender,
                         scan_dates=["2026-07-18"], now=fx.NOW,
                         test_message=True)
        out = capsys.readouterr().out
        assert code == 0
        assert "bankroll: $65.00" in out
        assert "telegram: configured (dedicated TT_EDGE bot)" in out
        assert "telegram test message: DELIVERED" in out
        assert "board 2026-07-18: fresh, 1 upcoming TT match(es), odds for 1" in out
        assert "verdict: READY" in out
        assert len(post.calls) == 1

    def test_broken_chain_names_every_failure(self, tmp_path, capsys):
        repo = fx.make_repo()                      # no bankroll
        muted = TTEdgeTelegram.from_config(fx.make_config(
            telegram_token=None, telegram_chat_id="",
            telegram_fallback=False))
        code = run_probe(repo=repo, config=fx.make_config(),
                         data_dir=tmp_path, sender=muted,
                         scan_dates=["2026-07-18"], now=fx.NOW)
        out = capsys.readouterr().out
        assert code == 1
        assert "bankroll: NOT SET" in out
        assert "telegram: MUTED" in out
        assert "board 2026-07-18: MISSING" in out
        assert "verdict: NOT READY" in out

    def test_stale_board_flagged(self, tmp_path, capsys):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, _ = fx.make_sender()
        code = run_probe(
            repo=repo, config=fx.make_config(),
            data_dir=self._data_dir(tmp_path, board_age=timedelta(hours=2)),
            sender=sender, scan_dates=["2026-07-18"], now=fx.NOW)
        out = capsys.readouterr().out
        assert code == 1
        assert "STALE" in out
        assert "NO FRESH BOARD" in out
