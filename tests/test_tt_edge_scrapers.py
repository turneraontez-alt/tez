"""Scraper layer: payload parsing, XHR URL classification, envelope
provenance, the freshness guard, and odds input validation."""
import json
from datetime import timedelta, timezone

import pytest

from tests.tt_edge_fixtures import NOW, envelope, event_dict, finished
from tt_edge import freshness
from tt_edge.edge.vig import OddsError
from tt_edge.freshness import NaiveTimestampError
from tt_edge.scrapers import sofascore
from tt_edge.scrapers.board import parse_board_payload
from tt_edge.scrapers.events import ParseError
from tt_edge.scrapers.form import parse_form_payload
from tt_edge.scrapers.h2h import parse_h2h_payload
from tt_edge.scrapers.odds import OddsSnapshot, parse_american_str


class TestBoardParsing:
    def test_filters_to_tournament_and_upcoming(self):
        payload = {"events": [
            event_dict(1, 11, "Marud", 12, "Gumulinski", NOW,
                       tournament="TT Elite Series"),
            event_dict(2, 13, "Someone", 14, "Else", NOW,
                       tournament="Setka Cup"),
            event_dict(3, 15, "Done", 16, "Already", NOW,
                       tournament="TT Elite Series", status="finished",
                       winner_code=1),
        ]}
        board = parse_board_payload(payload, fetched_at=NOW,
                                    tournament_keyword="TT Elite")
        assert [m.source_event_id for m in board.matches] == ["1"]
        assert board.skipped_events == 0

    def test_keyword_match_is_case_insensitive(self):
        payload = {"events": [event_dict(1, 11, "A", 12, "B", NOW,
                                         tournament="tt elite series MEN")]}
        board = parse_board_payload(payload, fetched_at=NOW,
                                    tournament_keyword="TT Elite")
        assert len(board.matches) == 1

    def test_malformed_event_skipped_and_counted(self):
        payload = {"events": [
            {"id": 1, "homeTeam": {"id": 11, "name": "A"}},  # no away/start
            event_dict(2, 11, "A", 12, "B", NOW),
        ]}
        board = parse_board_payload(payload, fetched_at=NOW,
                                    tournament_keyword="TT Elite")
        assert [m.source_event_id for m in board.matches] == ["2"]
        assert board.skipped_events == 1

    def test_matches_sorted_by_start_time(self):
        payload = {"events": [
            event_dict(2, 21, "C", 22, "D", NOW + timedelta(hours=2)),
            event_dict(1, 11, "A", 12, "B", NOW + timedelta(hours=1)),
        ]}
        board = parse_board_payload(payload, fetched_at=NOW,
                                    tournament_keyword="TT Elite")
        assert [m.source_event_id for m in board.matches] == ["1", "2"]

    def test_start_times_are_aware_utc(self):
        payload = {"events": [event_dict(1, 11, "A", 12, "B", NOW)]}
        board = parse_board_payload(payload, fetched_at=NOW,
                                    tournament_keyword="TT Elite")
        assert board.matches[0].start_time.tzinfo == timezone.utc

    @pytest.mark.parametrize("bad", [None, [], {"foo": 1}, {"events": "x"}])
    def test_bad_payload_raises(self, bad):
        with pytest.raises(ParseError):
            parse_board_payload(bad, fetched_at=NOW,
                                tournament_keyword="TT Elite")


class TestH2HParsing:
    def test_winner_maps_through_home_away_swaps(self):
        payload = {"events": [
            finished(1, 11, 12, NOW - timedelta(days=1), 1),   # 11 won
            finished(2, 12, 11, NOW - timedelta(days=2), 1),   # 12 won
            finished(3, 12, 11, NOW - timedelta(days=3), 2),   # 11 won
        ]}
        snap = parse_h2h_payload(payload, home_id="11", away_id="12",
                                 fetched_at=NOW)
        winners = sorted(m.winner_id for m in snap.meetings)
        assert winners == ["11", "11", "12"]

    def test_third_party_and_unfinished_events_dropped(self):
        payload = {"events": [
            finished(1, 11, 99, NOW - timedelta(days=1), 1),        # not pair
            event_dict(2, 11, "A", 12, "B", NOW - timedelta(days=1),
                       status="inprogress"),                        # undecided
            finished(3, 11, 12, NOW - timedelta(days=2), 2),
        ]}
        snap = parse_h2h_payload(payload, home_id="11", away_id="12",
                                 fetched_at=NOW)
        assert len(snap.meetings) == 1
        assert snap.meetings[0].winner_id == "12"

    def test_malformed_event_counted(self):
        payload = {"events": [{"id": None}]}
        snap = parse_h2h_payload(payload, home_id="11", away_id="12",
                                 fetched_at=NOW)
        assert snap.skipped_events == 1


class TestFormParsing:
    def test_player_perspective(self):
        payload = {"events": [
            finished(1, 11, 55, NOW - timedelta(days=1), 1),  # 11 home, won
            finished(2, 56, 11, NOW - timedelta(days=2), 1),  # 11 away, lost
        ]}
        snap = parse_form_payload(payload, player_id="11", fetched_at=NOW)
        assert len(snap.results) == 2
        assert snap.results[0].won_by("11")
        assert not snap.results[1].won_by("11")

    def test_foreign_events_dropped(self):
        payload = {"events": [finished(1, 55, 56, NOW, 1)]}
        snap = parse_form_payload(payload, player_id="11", fetched_at=NOW)
        assert snap.results == ()


class TestUrlClassification:
    def test_board_url(self):
        c = sofascore.classify_url(
            "https://api.sofascore.com/api/v1/sport/table-tennis/"
            "scheduled-events/2026-07-18")
        assert (c.kind, c.entity_id) == (sofascore.BOARD, "2026-07-18")

    def test_h2h_url(self):
        c = sofascore.classify_url(
            "https://api.sofascore.com/api/v1/event/12513062/h2h/events")
        assert (c.kind, c.entity_id) == (sofascore.H2H, "12513062")

    def test_form_url(self):
        c = sofascore.classify_url(
            "https://api.sofascore.com/api/v1/team/391710/events/last/0")
        assert (c.kind, c.entity_id) == (sofascore.FORM, "391710")

    @pytest.mark.parametrize("url", [
        "https://api.sofascore.com/api/v1/event/123/statistics",
        "https://www.sofascore.com/table-tennis",
        "https://evil.example/api/v1/event/1/h2h/events?api.sofascore.com",
    ])
    def test_unrelated_urls_ignored(self, url):
        assert sofascore.classify_url(url) is None


class TestEnvelopes:
    def test_round_trip(self, tmp_path):
        env = envelope(sofascore.H2H, "9001", {"events": []}, NOW)
        path = sofascore.save_envelope(env, tmp_path)
        loaded = sofascore.load_envelope(path)
        assert loaded.kind == sofascore.H2H
        assert loaded.entity_id == "9001"
        assert loaded.fetched_at == NOW
        assert loaded.payload == {"events": []}

    def test_raw_payload_rejected(self, tmp_path):
        # A hand-saved payload without provenance must not be analyzable.
        path = tmp_path / "board_x.json"
        path.write_text(json.dumps({"events": []}))
        with pytest.raises(sofascore.EnvelopeError):
            sofascore.load_envelope(path)

    def test_garbage_rejected(self, tmp_path):
        path = tmp_path / "junk.json"
        path.write_text("{not json")
        with pytest.raises(sofascore.EnvelopeError):
            sofascore.load_envelope(path)

    def test_collect_classified_later_payload_wins(self):
        url = "https://api.sofascore.com/api/v1/event/1/h2h/events"
        envs = sofascore.collect_classified(
            [(url, {"events": ["old"]}), (url, {"events": ["new"]})], NOW)
        assert len(envs) == 1
        assert envs[0].payload == {"events": ["new"]}


class TestFreshnessGuard:
    def test_fresh_inside_limit(self):
        check = freshness.check("board", NOW - timedelta(seconds=1799), NOW,
                                1800)
        assert check.ok and check.reason is None

    def test_age_at_limit_is_stale(self):
        check = freshness.check("board", NOW - timedelta(seconds=1800), NOW,
                                1800)
        assert not check.ok
        assert check.reason == "stale_board"

    def test_future_fetch_clamps_to_zero(self):
        assert freshness.age_seconds(NOW + timedelta(seconds=30), NOW) == 0.0

    def test_naive_timestamps_rejected(self):
        with pytest.raises(NaiveTimestampError):
            freshness.check("board", NOW.replace(tzinfo=None), NOW, 1800)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            freshness.check("weather", NOW, NOW, 60)

    @pytest.mark.parametrize("seconds,label", [
        (45, "45s"), (240, "4m"), (7200, "2h"), (200000, "2d")])
    def test_format_age(self, seconds, label):
        assert freshness.format_age(seconds) == label


class TestOddsInput:
    def test_snapshot_validates_prices_and_tz(self):
        snap = OddsSnapshot("9001", "manual", -120, 100, NOW)
        assert snap.home_price == -120
        with pytest.raises(OddsError):
            OddsSnapshot("9001", "manual", -50, 100, NOW)
        with pytest.raises(NaiveTimestampError):
            OddsSnapshot("9001", "manual", -120, 100, NOW.replace(tzinfo=None))
        with pytest.raises(ValueError):
            OddsSnapshot("", "manual", -120, 100, NOW)

    @pytest.mark.parametrize("text,expected", [
        ("-120", -120), ("+100", 100), (" 105 ", 105), ("+1 500", 1500)])
    def test_parse_american_str(self, text, expected):
        assert parse_american_str(text) == expected

    @pytest.mark.parametrize("bad", ["", "abc", "1.5", "-99", "+0"])
    def test_parse_american_str_rejects(self, bad):
        with pytest.raises(OddsError):
            parse_american_str(bad)
