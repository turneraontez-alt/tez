"""Cloud mode: BetsAPI translation/client, the git state branch, and the
full cloud cycle — fake transport, in-memory SQLite, real git tmp repos."""
import gzip
import json
import logging
import subprocess
from datetime import timedelta, timezone

import pytest

from tests import tt_edge_fixtures as fx
from tt_edge import state_sync
from tt_edge.edge.vig import OddsError
from tt_edge.jobs.cloud_cycle import (grade_only_result_dates,
                                      main as cloud_main, run_cloud_cycle)
from tt_edge.scrapers import betsapi
from tt_edge.scrapers.events import ParsedEvent

NOW_TS = int(fx.NOW.timestamp())
START_TS = int(fx.START.timestamp())


def bets_event(event_id, home_id, away_id, ts, *, status="0", ss=None,
               home_name="H", away_name="A", league="TT Elite Series"):
    event = {"id": event_id, "sport_id": "92", "time": str(ts),
             "time_status": status,
             "league": {"id": "29128", "name": league},
             "home": {"id": home_id, "name": home_name},
             "away": {"id": away_id, "name": away_name}}
    if ss is not None:
        event["ss"] = ss
    return event


class TestTranslation:
    @pytest.mark.parametrize("ss,expected", [
        ("3-1", 1), ("1-3", 2), ("0-3", 2), ("2-2", None), (None, None),
        ("abc", None), ("3", None)])
    def test_winner_from_score(self, ss, expected):
        assert betsapi.winner_code_from_score(ss) == expected

    def test_translate_upcoming_event(self):
        event = betsapi.translate_event(bets_event(
            9001, 101, 102, START_TS, home_name="Marud",
            away_name="Gumulinski"))
        assert event["id"] == 9001
        assert event["status"]["type"] == "notstarted"
        assert event["homeTeam"] == {"id": 101, "name": "Marud"}
        assert event["startTimestamp"] == START_TS
        assert "winnerCode" not in event

    def test_translate_finished_event_carries_winner(self):
        event = betsapi.translate_event(bets_event(
            9001, 101, 102, NOW_TS, status="3", ss="3-1"))
        assert event["status"]["type"] == "finished"
        assert event["winnerCode"] == 1

    @pytest.mark.parametrize("status,expected", [
        ("1", "inprogress"), ("4", "postponed"), ("5", "canceled"),
        ("99", "")])
    def test_status_map(self, status, expected):
        event = betsapi.translate_event(bets_event(1, 2, 3, NOW_TS,
                                                   status=status))
        assert event["status"]["type"] == expected

    def test_malformed_events_dropped(self):
        broken = [{"id": 1}, {"home": {"id": 1}}, "junk",
                  bets_event(2, 3, 4, "not-a-time")]
        assert betsapi.translate_events(broken) == []


class TestOdds:
    @pytest.mark.parametrize("text,expected", [
        ("1.80", -125), ("2", 100), ("2.05", 105), ("3.50", 250),
        ("1.5", -200)])
    def test_decimal_str_to_american(self, text, expected):
        assert betsapi.decimal_str_to_american(text) == expected

    @pytest.mark.parametrize("bad", ["-", "", "0.9", "1", "abc", None])
    def test_rejects_non_decimal_odds(self, bad):
        # NB: "1" (decimal odds 1.0) is a void price, not evens — decimal
        # odds must exceed 1, unlike the sofascore fractional heuristic.
        with pytest.raises(OddsError):
            betsapi.decimal_str_to_american(bad)

    def test_series_parses_chronologically_and_skips_suspended(self):
        payload = {"success": 1, "results": {"odds": {"92_1": [
            {"add_time": str(NOW_TS - 45), "home_od": "1.80",
             "away_od": "2.05"},
            {"add_time": str(NOW_TS - 3600), "home_od": "1.66",
             "away_od": "2.20"},
            {"add_time": str(NOW_TS - 100), "home_od": "-", "away_od": "-"},
        ]}}}
        series = betsapi.parse_odds_series(payload, "9001")
        assert [s.home_price for s in series] == [-152, -125]
        assert all(s.book == "bet365" for s in series)
        assert series[0].captured_at < series[1].captured_at

    def test_no_odds_market_is_empty(self):
        assert betsapi.parse_odds_series({"results": {}}, "9001") == []
        with pytest.raises(betsapi.BetsAPIError):
            betsapi.parse_odds_series([1], "9001")

    def test_current_odds_returns_latest_price(self):
        payload = {"success": 1, "results": {"odds": {"92_1": [
            {"add_time": str(NOW_TS - 7200), "home_od": "1.50",
             "away_od": "2.50"},
            {"add_time": str(NOW_TS - 3600), "home_od": "1.80",
             "away_od": "2.05"},
        ]}}}
        assert betsapi.current_odds(payload) == (-125, 105)   # newest wins
        assert betsapi.current_odds({"results": {}}) is None

    def test_stable_old_price_is_not_stale(self):
        # A price Bet365 set 2h ago but never changed must still be usable:
        # fetch_cloud_bundle stamps it at `now`, so freshness passes.
        payload = {"success": 1, "results": {"odds": {"92_1": [
            {"add_time": str(NOW_TS - 7200), "home_od": "1.80",
             "away_od": "2.05"}]}}}

        class OneOddsTransport:
            def __call__(self, url, timeout):
                import json as _json
                import urllib.parse as _up
                q = _up.parse_qs(_up.urlparse(url).query)
                if "/v3/events/upcoming" in url:
                    return 200, _json.dumps({"success": 1, "results": [
                        bets_event(9001, 101, 102, START_TS,
                                   home_name="Marud", away_name="G")]}).encode()
                if "/v1/event/history" in url:
                    r = _scenario_routes()["/v1/event/history"]["results"]
                    return 200, _json.dumps({"success": 1, "results": r}).encode()
                if "/v2/event/odds" in url:
                    return 200, _json.dumps(payload).encode()
                return 200, b'{"success":1,"results":[]}'

        client = betsapi.BetsAPIClient("tok", transport=OneOddsTransport())
        bundle = betsapi.fetch_cloud_bundle(
            client, ["2026-07-18"], league_id="29128", max_events=5,
            now=fx.NOW)
        assert len(bundle.odds_snapshots) == 1
        # captured_at is the fetch time, NOT the 2h-old add_time.
        assert bundle.odds_snapshots[0].captured_at == fx.NOW


class FakeTransport:
    """URL-dispatching transport: path substring -> JSON payload."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return 200, json.dumps(payload).encode()
        return 200, json.dumps({"success": 1, "results": []}).encode()


class TestClient:
    def test_success_flag_enforced(self):
        transport = FakeTransport({"upcoming": {"success": 0,
                                                "error": "PERMISSION_DENIED"}})
        client = betsapi.BetsAPIClient("tok", transport=transport)
        with pytest.raises(betsapi.BetsAPIError, match="PERMISSION_DENIED"):
            client.upcoming("20260718", "29128")

    def test_http_error_raises(self):
        client = betsapi.BetsAPIClient(
            "tok", transport=lambda url, timeout: (500, b"boom"))
        with pytest.raises(betsapi.BetsAPIError, match="HTTP 500"):
            client.upcoming("20260718", "29128")

    def test_token_and_params_in_url(self):
        transport = FakeTransport({})
        client = betsapi.BetsAPIClient("secret-tok", transport=transport)
        client.upcoming("20260718", "29128")
        assert "token=secret-tok" in transport.urls[0]
        assert "sport_id=92" in transport.urls[0]
        assert "league_id=29128" in transport.urls[0]

    def test_empty_token_rejected(self):
        with pytest.raises(betsapi.BetsAPIError):
            betsapi.BetsAPIClient("")


def _scenario_routes():
    """BetsAPI payloads reproducing the standard Marud scenario."""
    h2h = [bets_event(8000 + i, 101, 102, NOW_TS - 86400 * (3 + i),
                      status="3", ss="3-1" if i % 4 != 3 else "1-3")
           for i in range(20)]
    home_form = [bets_event(7000 + i, 101,
                            201 if i == 0 else (202 if i == 1 else 300 + i),
                            NOW_TS - 86400 * (1 + i), status="3",
                            ss="3-0" if i < 12 else "0-3")
                 for i in range(15)]
    away_form = [bets_event(6000 + i, 102,
                            201 if i == 0 else (202 if i == 1 else 400 + i),
                            NOW_TS - 86400 * (1 + i), status="3",
                            ss="0-3" if i < 10 else "3-0")
                 for i in range(15)]
    return {
        "/v3/events/upcoming": {"success": 1, "results": [
            bets_event(9001, 101, 102, START_TS, home_name="Marud",
                       away_name="Gumulinski")]},
        "/v3/events/ended": {"success": 1, "results": []},
        "/v1/event/history": {"success": 1, "results": {
            "h2h": h2h, "home": home_form, "away": away_form}},
        "/v2/event/odds": {"success": 1, "results": {"odds": {"92_1": [
            {"add_time": str(NOW_TS - 45), "home_od": "1.80",
             "away_od": "2.05"}]}}},
    }


class TestCloudCycle:
    def _client(self, routes=None):
        return betsapi.BetsAPIClient(
            "tok", transport=FakeTransport(routes or _scenario_routes()))

    def test_full_cycle_recommends_with_bet365_prices(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        config = fx.make_config(book=betsapi.BOOK)

        report, odds_inserted = run_cloud_cycle(
            repo=repo, config=config, client=self._client(), now=fx.NOW,
            sender=sender, league_ids=["29128"], dry_run=False)

        assert odds_inserted == 1
        assert report.recommended == 1 and report.delivered == 1
        text = post.calls[0]["json"]["text"]
        assert "Pick: Marud ML @ -125" in text
        assert "Price source: bet365 — take -125 or better" in text
        assert [s.book for s in repo.odds_history("9001")] == ["bet365"]

    def test_cycles_are_idempotent_and_grade_later(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        config = fx.make_config(book=betsapi.BOOK)
        run_cloud_cycle(repo=repo, config=config, client=self._client(),
                        now=fx.NOW, sender=sender, league_ids=["29128"],
                        dry_run=False)
        second, odds_second = run_cloud_cycle(
            repo=repo, config=config, client=self._client(), now=fx.NOW,
            sender=sender, league_ids=["29128"], dry_run=False)
        assert len(post.calls) == 1
        assert odds_second == 0                      # same add_time deduped

        routes = _scenario_routes()
        routes["/v3/events/upcoming"] = {"success": 1, "results": []}
        routes["/v3/events/ended"] = {"success": 1, "results": [
            bets_event(9001, 101, 102, START_TS, status="3", ss="3-1",
                       home_name="Marud", away_name="Gumulinski")]}
        later = fx.NOW + timedelta(minutes=90)
        report, _ = run_cloud_cycle(
            repo=repo, config=config, client=self._client(routes), now=later,
            sender=sender, league_ids=["29128"], dry_run=False)
        assert report.grade.recommendations_settled == 1
        assert report.grade.profit_cents == 260      # stake 325 at -125
        assert repo.get_bankroll_cents() == 6760

    def test_history_failure_degrades_to_insufficient_data(self):
        routes = _scenario_routes()
        routes["/v1/event/history"] = {"success": 0, "error": "RATE_LIMIT"}
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        report, _ = run_cloud_cycle(
            repo=repo, config=fx.make_config(book=betsapi.BOOK),
            client=self._client(routes), now=fx.NOW, sender=None,
            league_ids=["29128"], dry_run=True)
        assert report.outcomes[0].status == "NO_BET"
        assert "insufficient_data" in report.outcomes[0].reasons

    def test_main_exits_quietly_without_key(self, monkeypatch, capsys):
        monkeypatch.delenv("TT_EDGE_BETSAPI_KEY", raising=False)
        assert cloud_main(["--no-state", "--dry-run"]) == 3
        assert "no TT_EDGE_BETSAPI_KEY" in capsys.readouterr().out

    def test_multiple_leagues_merge_into_one_cycle(self):
        # Two leagues, one upcoming match each; both must be scanned and the
        # per-league board/history/odds must route by their own ids.
        cup_h2h = [bets_event(5000 + i, 301, 302, NOW_TS - 86400 * (2 + i),
                              status="3", ss="3-1" if i % 5 else "1-3")
                   for i in range(20)]
        cup_form_h = [bets_event(5100 + i, 301, 900 + i,
                                 NOW_TS - 86400 * (1 + i), status="3",
                                 ss="3-0" if i < 11 else "0-3")
                      for i in range(15)]
        cup_form_a = [bets_event(5200 + i, 302, 900 + i,
                                 NOW_TS - 86400 * (1 + i), status="3",
                                 ss="0-3" if i < 9 else "3-0")
                      for i in range(15)]

        class LeagueTransport:
            def __init__(self):
                self.leagues_seen = set()

            def __call__(self, url, timeout):
                import json as _json
                import urllib.parse as _up
                query = _up.parse_qs(_up.urlparse(url).query)
                if "/v3/events/upcoming" in url:
                    league = query["league_id"][0]
                    self.leagues_seen.add(league)
                    if league == "29128":
                        ev = [bets_event(9001, 101, 102, START_TS,
                                         home_name="Marud",
                                         away_name="Gumu")]
                    else:            # 29097 TT Cup
                        ev = [bets_event(7700, 301, 302, START_TS,
                                         home_name="Cupic", away_name="Cupax")]
                    return 200, _json.dumps({"success": 1, "results": ev}).encode()
                if "/v3/events/ended" in url:
                    return 200, b'{"success":1,"results":[]}'
                if "/v1/event/history" in url:
                    eid = query["event_id"][0]
                    if eid == "9001":
                        r = {"h2h": _scenario_routes()["/v1/event/history"]
                             ["results"]["h2h"],
                             "home": _scenario_routes()["/v1/event/history"]
                             ["results"]["home"],
                             "away": _scenario_routes()["/v1/event/history"]
                             ["results"]["away"]}
                    else:
                        r = {"h2h": cup_h2h, "home": cup_form_h,
                             "away": cup_form_a}
                    return 200, _json.dumps({"success": 1, "results": r}).encode()
                if "/v2/event/odds" in url:
                    eid = query["event_id"][0]
                    price = ("1.80", "2.05") if eid == "9001" else ("1.72", "2.10")
                    body = {"success": 1, "results": {"odds": {"92_1": [
                        {"add_time": str(NOW_TS - 40), "home_od": price[0],
                         "away_od": price[1]}]}}}
                    return 200, _json.dumps(body).encode()
                return 200, b'{"success":1,"results":[]}'

        transport = LeagueTransport()
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        client = betsapi.BetsAPIClient("tok", transport=transport)
        # tournament_keyword must be empty (cloud mode) or the non-"TT Elite"
        # league's board would be filtered away.
        config = fx.make_config(book=betsapi.BOOK, tournament_keyword="")
        report, inserted = run_cloud_cycle(
            repo=repo, config=config, client=client, now=fx.NOW, sender=None,
            league_ids=["29128", "29097"], dry_run=True)

        assert transport.leagues_seen == {"29128", "29097"}
        scanned = {o.match_source_id for o in report.outcomes}
        assert scanned == {"9001", "7700"}
        assert inserted == 2                        # one odds row per league
        # Both matches are strong home favorites -> both recommend.
        assert report.recommended == 2


class TestGradeOnlyLeagues:
    """Split coverage: a league with OPEN claims that is no longer in the
    scan list must still get a results-only fetch — its claims settle, but
    it can never produce a new pick (stuck claims would otherwise pin the
    max-open-picks cap and suppress every future pick)."""

    def _open_claim(self, repo, event_id, tournament, start, *, now):
        repo.upsert_match(ParsedEvent(
            source_event_id=event_id, home_id=f"h{event_id}", home_name="H",
            away_id=f"a{event_id}", away_name="A", start_time=start,
            status_type="notstarted", winner_code=None,
            tournament_name=tournament), now)
        assert repo.insert_recommendation(
            match_source_id=event_id, market="ML", side="home", pick_name="H",
            model_p="0.6", fair_p="0.5", edge="0.1", price_american=-120,
            stake_cents=100, bankroll_cents=6500, recommended_at=now,
            recommended_on=now.date().isoformat(), basis_json="{}",
            data_ages_json="{}") is not None

    def test_open_claim_in_unscanned_league_settles_without_new_picks(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        sender, post = fx.make_sender()
        config = fx.make_config(book=betsapi.BOOK, tournament_keyword="")
        # Seed: a normal TT Elite cycle claims the Marud pick.
        run_cloud_cycle(
            repo=repo, config=config,
            client=betsapi.BetsAPIClient(
                "tok", transport=FakeTransport(_scenario_routes())),
            now=fx.NOW, sender=sender, league_ids=["29128"], dry_run=False)
        assert repo.open_recommendation_count() == 1
        assert len(post.calls) == 1

        # Cups-only cycle: TT Elite is no longer scanned, but its open claim
        # triggers a results-only fetch for the claim's own day.
        transport = FakeTransport({
            "/v3/events/upcoming": {"success": 1, "results": []},
            "league_id=29128": {"success": 1, "results": [
                bets_event(9001, 101, 102, START_TS, status="3", ss="3-1",
                           home_name="Marud", away_name="Gumulinski")]},
            "/v3/events/ended": {"success": 1, "results": []},
        })
        later = fx.NOW + timedelta(minutes=90)
        report, _ = run_cloud_cycle(
            repo=repo, config=config,
            client=betsapi.BetsAPIClient("tok", transport=transport),
            now=later, sender=sender, league_ids=["29097"], dry_run=False)

        assert report.grade.recommendations_settled == 1
        assert repo.get_bankroll_cents() == 6760       # stake 325 at -125
        assert repo.open_recommendation_count() == 0
        assert len(post.calls) == 1                    # seed alert only
        assert not report.outcomes                     # nothing scannable
        # TT Elite saw ONLY ended fetches, on the claim's start date …
        elite = [u for u in transport.urls if "league_id=29128" in u]
        day = fx.START.astimezone(timezone.utc).strftime("%Y%m%d")
        assert elite and all("/v3/events/ended" in u and f"day={day}" in u
                             for u in elite)
        # … and no per-event calls happened anywhere (nothing was scanned).
        assert not any("/v1/event/history" in u or "/v2/event/odds" in u
                       for u in transport.urls)

    def test_dates_derive_from_claims_and_skip_scanned_or_unmapped(
            self, caplog):
        repo = fx.make_repo()
        old = fx.NOW - timedelta(days=3)
        self._open_claim(repo, "111", "TT Elite Series", old, now=fx.NOW)
        self._open_claim(repo, "222", "Czech Liga Pro", fx.NOW, now=fx.NOW)
        self._open_claim(repo, "333", "Mystery League", fx.NOW, now=fx.NOW)

        with caplog.at_level(logging.WARNING):
            fetches = grade_only_result_dates(repo, ["22742"])
        # Czech is scanned (skipped); the days-old TT Elite claim still gets
        # its own result day; the unmapped league is logged, not fatal.
        assert fetches == {"29128": [old.astimezone(timezone.utc)
                                     .date().isoformat()]}
        assert "Mystery League" in caplog.text
        assert grade_only_result_dates(repo, ["29128", "22742"]) == {}

    def test_results_bundle_is_ended_only_and_degrades(self):
        transport = FakeTransport({
            "day=20260717": {"success": 1, "results": [
                bets_event(1, 10, 11, NOW_TS - 3600, status="3", ss="3-2"),
                bets_event(2, 12, 13, NOW_TS + 3600, status="0")]},
            "day=20260716": {"success": 0, "error": "PERMISSION"},
        })
        client = betsapi.BetsAPIClient("tok", transport=transport)
        bundle = betsapi.fetch_results_bundle(
            client, ["2026-07-17", "2026-07-16"], league_id="29128",
            now=fx.NOW)
        assert [e.entity_id for e in bundle.envelopes] == ["2026-07-17",
                                                           "2026-07-16"]
        finished_day, failed_day = bundle.envelopes
        assert [e["id"] for e in finished_day.payload["events"]] == [1]
        assert failed_day.payload["events"] == []      # failure degrades
        assert bundle.odds_snapshots == [] and bundle.events_fetched == 0
        assert all("/v3/events/ended" in u for u in transport.urls)


class TestParseLeagueIds:
    def test_default_is_the_three_book_leagues(self):
        assert betsapi.parse_league_ids(None) == ["29128", "29097", "22742"]
        assert betsapi.parse_league_ids("  ") == ["29128", "29097", "22742"]

    def test_comma_separated_dedup_order_preserving(self):
        assert betsapi.parse_league_ids("29097, 22742 ,29097") == \
            ["29097", "22742"]

    def test_single_league(self):
        assert betsapi.parse_league_ids("22307") == ["22307"]


class TestStateSync:
    def _repos(self, tmp_path):
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)],
                       check=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin)],
                       cwd=work, check=True)
        return work

    def test_push_and_restore_round_trip(self, tmp_path):
        work = self._repos(tmp_path)
        db = tmp_path / "data" / "tt_edge.sqlite3"
        db.parent.mkdir()
        db.write_bytes(b"sqlite-bytes-123")
        assert state_sync.push_state(db, work, "tt-state", label="t1")

        restored = tmp_path / "restored" / "tt_edge.sqlite3"
        assert state_sync.restore_state(restored, work, "tt-state")
        assert restored.read_bytes() == b"sqlite-bytes-123"

    def test_push_overwrites_single_commit(self, tmp_path):
        work = self._repos(tmp_path)
        db = tmp_path / "db.sqlite3"
        db.write_bytes(b"v1")
        state_sync.push_state(db, work, "tt-state")
        db.write_bytes(b"v2")
        state_sync.push_state(db, work, "tt-state")
        out = tmp_path / "out.sqlite3"
        assert state_sync.restore_state(out, work, "tt-state")
        assert out.read_bytes() == b"v2"
        log = subprocess.run(["git", "rev-list", "--count", "origin/tt-state"],
                             cwd=work, capture_output=True, text=True)
        assert log.stdout.strip() == "1"             # orphan: no history

    def test_restore_missing_branch_is_false(self, tmp_path):
        work = self._repos(tmp_path)
        assert state_sync.restore_state(tmp_path / "x.sqlite3", work,
                                        "absent") is False

    def test_restore_never_clobbers_local_db(self, tmp_path):
        work = self._repos(tmp_path)
        db = tmp_path / "db.sqlite3"
        db.write_bytes(b"remote")
        state_sync.push_state(db, work, "tt-state")
        db.write_bytes(b"local-newer")
        assert state_sync.restore_state(db, work, "tt-state") is False
        assert db.read_bytes() == b"local-newer"

    def test_push_failure_returns_false(self, tmp_path):
        work = tmp_path / "lonely"
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        db = tmp_path / "db.sqlite3"
        db.write_bytes(b"x")
        assert state_sync.push_state(db, work, "tt-state") is False
