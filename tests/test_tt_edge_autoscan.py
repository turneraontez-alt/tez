"""Phase 1 autoscan: sofascore odds parsing, board->results mapping, the
Telegram fallback, and the full grade -> odds -> scan cycle — all with
injected envelopes, no browser, no network."""
from datetime import timedelta

import pytest

from tests import tt_edge_fixtures as fx
from tt_edge.alerts.telegram import TTEdgeTelegram
from tt_edge.edge.vig import OddsError
from tt_edge.jobs.autoscan import (cycle_dates, load_envelopes,
                                   results_from_board, run_cycle)
from tt_edge.jobs.grade import ResultInput
from tt_edge.scrapers import sofa_odds, sofascore
from tt_edge.scrapers.sofa_odds import (decimal_odds_to_american,
                                        fractional_to_decimal_odds,
                                        parse_odds_payload)


class TestOddsConversion:
    @pytest.mark.parametrize("text,expected", [
        ("1", 100),        # evens
        ("1/1", 100),
        ("4/5", -125),
        ("21/20", 105),
        ("13/10", 130),
        ("1/2", -200),
        ("2", 200),        # bare integer = fractional profit 2/1
        ("2.50", 150),     # decimal odds
        ("1.80", -125),
        ("1.98", -102),
    ])
    def test_to_american(self, text, expected):
        assert decimal_odds_to_american(
            fractional_to_decimal_odds(text)) == expected

    def test_gap_snap_just_under_evens(self):
        # decimal 1.995 -> raw -100.5 -> -101; decimal 1.999 -> -100.05 ->
        # rounds -100 (legal boundary).
        assert decimal_odds_to_american(
            fractional_to_decimal_odds("1.999")) == -100

    @pytest.mark.parametrize("bad", ["", "0", "-1/2", "1/0", "abc", "0.98"])
    def test_rejects(self, bad):
        with pytest.raises(OddsError):
            fractional_to_decimal_odds(bad)


class TestOddsPayload:
    def _payload(self, home="4/5", away="21/20", name="Full time",
                 live=False):
        return {"markets": [{
            "marketName": name, "isLive": live,
            "choices": [{"name": "1", "fractionalValue": home},
                        {"name": "2", "fractionalValue": away}]}]}

    def test_parses_winner_market(self):
        odds = parse_odds_payload(self._payload())
        assert (odds.home_price, odds.away_price) == (-125, 105)
        assert not odds.is_live

    def test_decimal_value_field_fallback(self):
        payload = {"markets": [{
            "marketName": "Match winner", "isLive": False,
            "choices": [{"name": "1", "value": "1.80"},
                        {"name": "2", "value": "2.05"}]}]}
        odds = parse_odds_payload(payload)
        assert (odds.home_price, odds.away_price) == (-125, 105)

    def test_no_usable_market_is_none(self):
        assert parse_odds_payload({"markets": []}) is None
        assert parse_odds_payload({}) is None
        assert parse_odds_payload(
            {"markets": [{"marketName": "Total sets", "choices": []}]}) is None

    def test_malformed_payload_raises(self):
        with pytest.raises(OddsError):
            parse_odds_payload([1, 2])
        with pytest.raises(OddsError):
            parse_odds_payload({"markets": "nope"})


class TestResultsFromBoard:
    def test_maps_finished_and_void_statuses(self):
        payload = {"events": [
            fx.finished(1, 11, 12, fx.NOW - timedelta(hours=1), 1),
            fx.finished(2, 13, 14, fx.NOW - timedelta(hours=1), 2),
            fx.event_dict(3, 15, "A", 16, "B", fx.NOW, status="canceled"),
            fx.event_dict(4, 17, "C", 18, "D", fx.NOW),   # upcoming: ignored
            {"id": None},                                  # malformed: skipped
        ]}
        results = results_from_board(payload)
        assert results == [
            ResultInput("1", "home"), ResultInput("2", "away"),
            ResultInput("3", "void")]


class TestUrlAndEnvelope:
    def test_odds_url_classified(self):
        c = sofascore.classify_url(
            "https://api.sofascore.com/api/v1/event/9001/odds/1/all")
        assert (c.kind, c.entity_id) == (sofascore.ODDS, "9001")

    def test_api_url_builders_round_trip(self):
        assert sofascore.classify_url(sofascore.board_api_url("2026-07-18"))
        assert sofascore.classify_url(sofascore.h2h_api_url("9001"))
        assert sofascore.classify_url(sofascore.form_api_url("101"))
        assert sofascore.classify_url(sofascore.odds_api_url("9001", 1))

    def test_fresh_entity_ids_respects_age(self, tmp_path):
        old = fx.envelope(sofascore.H2H, "1", {"events": []},
                          fx.NOW - timedelta(hours=7))
        new = fx.envelope(sofascore.H2H, "2", {"events": []},
                          fx.NOW - timedelta(hours=1))
        sofascore.save_envelope(old, tmp_path)
        sofascore.save_envelope(new, tmp_path)
        fresh = sofascore.fresh_entity_ids(tmp_path, sofascore.H2H,
                                           6 * 3600, fx.NOW)
        assert fresh == {"2"}

    def test_load_envelopes_skips_garbage(self, tmp_path):
        sofascore.save_envelope(
            fx.envelope(sofascore.BOARD, "2026-07-18", {"events": []},
                        fx.NOW), tmp_path)
        (tmp_path / "junk.json").write_text("{broken")
        assert len(load_envelopes(tmp_path)) == 1


class TestTelegramFallback:
    def test_dedicated_bot_preferred(self):
        cfg = fx.make_config()
        assert TTEdgeTelegram.from_config(cfg)._client.token == "test-token"

    def test_falls_back_to_q15_channel(self):
        cfg = fx.make_config(telegram_token=None, telegram_chat_id="",
                             q15_telegram_token="q15-tok",
                             q15_telegram_chat_id="q15-chat")
        client = TTEdgeTelegram.from_config(cfg)._client
        assert client.token == "q15-tok"
        assert client.chat_id == "q15-chat"
        assert client.enabled

    def test_fallback_can_be_disabled(self):
        cfg = fx.make_config(telegram_token=None, telegram_chat_id="",
                             telegram_fallback=False,
                             q15_telegram_token="q15-tok",
                             q15_telegram_chat_id="q15-chat")
        assert not TTEdgeTelegram.from_config(cfg).configured


class TestCycleDates:
    def test_covers_yesterday_for_grading(self):
        fetch, scan = cycle_dates(fx.NOW, 2)
        assert fetch == ["2026-07-17", "2026-07-18", "2026-07-19"]
        assert scan == ["2026-07-18", "2026-07-19"]


def _odds_envelope(home="4/5", away="21/20", age_s=45):
    payload = {"markets": [{
        "marketName": "Full time", "isLive": False,
        "choices": [{"name": "1", "fractionalValue": home},
                    {"name": "2", "fractionalValue": away}]}]}
    return fx.envelope(sofascore.ODDS, fx.MATCH_ID, payload,
                       fx.NOW - timedelta(seconds=age_s))


def _cycle_envelopes(**scenario_ages):
    env = fx.scenario_envelopes(**scenario_ages)
    return ([env["board"]] + list(env["h2h"].values()) +
            list(env["form"].values()) + [_odds_envelope()])


def _autoscan_config():
    # Autoscan prices come from the sofascore book.
    return fx.make_config(book=sofa_odds.BOOK)


class TestRunCycle:
    def test_full_cycle_recommends_and_delivers(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()

        report = run_cycle(repo=repo, config=_autoscan_config(),
                           envelopes=_cycle_envelopes(),
                           scan_dates=["2026-07-18"], now=fx.NOW,
                           sender=sender, dry_run=False)

        assert report.odds_rows_inserted == 1
        assert report.recommended == 1 and report.delivered == 1
        text = post.calls[0]["json"]["text"]
        # -125/+105 de-vigs to fair home ~0.548; the blend (~0.82) clears
        # the threshold easily and the caveat line names the price source.
        assert "Pick: Marud ML @ -125" in text
        assert "Price source: sofascore — take -125 or better" in text
        history = repo.odds_history(fx.MATCH_ID, sofa_odds.BOOK)
        assert [s.book for s in history] == [sofa_odds.BOOK]

    def test_cycle_is_idempotent(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        config = _autoscan_config()

        run_cycle(repo=repo, config=config, envelopes=_cycle_envelopes(),
                  scan_dates=["2026-07-18"], now=fx.NOW, sender=sender,
                  dry_run=False)
        second = run_cycle(repo=repo, config=config,
                           envelopes=_cycle_envelopes(),
                           scan_dates=["2026-07-18"], now=fx.NOW,
                           sender=sender, dry_run=False)

        assert len(post.calls) == 1                      # no re-alert
        assert second.outcomes[0].reasons == ("duplicate_scan",)
        assert second.odds_rows_inserted == 0            # same captured_at
        assert len(repo._fetchall("SELECT id FROM tt_recommendations")) == 1

    def test_next_cycle_grades_the_finished_match(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        config = _autoscan_config()
        run_cycle(repo=repo, config=config, envelopes=_cycle_envelopes(),
                  scan_dates=["2026-07-18"], now=fx.NOW, sender=sender,
                  dry_run=False)

        # 90 minutes later the board shows the match finished, Marud (home,
        # picked at -125) won.
        later = fx.NOW + timedelta(minutes=90)
        finished_board = fx.envelope(
            sofascore.BOARD, "2026-07-18",
            {"events": [fx.finished(int(fx.MATCH_ID), int(fx.HOME_ID),
                                    int(fx.AWAY_ID), fx.START, 1,
                                    home_name=fx.HOME_NAME,
                                    away_name=fx.AWAY_NAME)]},
            later - timedelta(minutes=2))
        report = run_cycle(repo=repo, config=config,
                           envelopes=[finished_board],
                           scan_dates=["2026-07-18"], now=later,
                           sender=sender, dry_run=False)

        assert report.grade.results_recorded == 1
        assert report.grade.recommendations_settled == 1
        # Stake 325 at -125: profit floor(325 * 0.8) = 260.
        assert report.grade.profit_cents == 260
        assert repo.get_bankroll_cents() == 6760
        assert len(post.calls) == 1                      # no new alert

    def test_live_odds_are_never_priced(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        payload = {"markets": [{
            "marketName": "Full time", "isLive": True,
            "choices": [{"name": "1", "fractionalValue": "4/5"},
                        {"name": "2", "fractionalValue": "21/20"}]}]}
        envelopes = _cycle_envelopes()[:-1] + [fx.envelope(
            sofascore.ODDS, fx.MATCH_ID, payload,
            fx.NOW - timedelta(seconds=45))]
        report = run_cycle(repo=repo, config=_autoscan_config(),
                           envelopes=envelopes, scan_dates=["2026-07-18"],
                           now=fx.NOW, sender=None, dry_run=True)
        assert report.odds_rows_inserted == 0
        assert "no_odds" in report.outcomes[0].reasons

    def test_stale_cached_board_is_results_only(self):
        # Yesterday's board stays in the cache for grading but is not in
        # scan_dates — no scan outcomes from it.
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        old_board = fx.envelope(
            sofascore.BOARD, "2026-07-17",
            {"events": [fx.finished(7777, 55, 56,
                                    fx.NOW - timedelta(days=1), 1)]},
            fx.NOW - timedelta(hours=20))
        report = run_cycle(repo=repo, config=_autoscan_config(),
                           envelopes=[old_board],
                           scan_dates=["2026-07-18"], now=fx.NOW,
                           sender=None, dry_run=True)
        assert report.boards_scanned == 0
        assert report.outcomes == []
        assert report.grade.results_recorded == 1
