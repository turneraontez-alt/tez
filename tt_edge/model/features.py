"""Feature extraction from H2H / form histories.

Pure functions over already-parsed records (see ``tt_edge.scrapers``); the
clock is always an argument — nothing here reads wall time. Feature values
are floats in [-1, 1] (or a rate in [0, 1]) because they feed a sigmoid;
Decimal starts at the edge/staking boundary where money begins.

Perspective convention: "subject" is the player the feature describes (the
HOME player in the pipeline); positive differentials favor the subject.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tt_edge.freshness import require_aware


@dataclass(frozen=True)
class MatchResult:
    """One completed match from a player's history (used for both H2H
    meetings and form). ``winner_id`` is a source player id."""

    start_time: datetime
    winner_id: str
    home_id: str
    away_id: str

    def involves(self, player_id: str) -> bool:
        return player_id in (self.home_id, self.away_id)

    def opponent_of(self, player_id: str) -> str:
        if player_id == self.home_id:
            return self.away_id
        if player_id == self.away_id:
            return self.home_id
        raise ValueError(f"player {player_id} not in match {self!r}")

    def won_by(self, player_id: str) -> bool:
        return self.winner_id == player_id


@dataclass(frozen=True)
class H2HFeature:
    """Laplace-smoothed, age-decayed H2H rate for the subject, plus the raw
    tallies the alert's basis line shows (career W-L and last-20 W-L)."""

    rate: float                # smoothed weighted win rate, (0, 1)
    n_meetings: int            # total meetings considered
    career_wins: int
    career_losses: int
    last20_wins: int
    last20_losses: int


def h2h_feature(meetings: list[MatchResult], subject_id: str, now: datetime,
                *, half_life_days: float) -> H2HFeature:
    """H2H rate with exponential age decay (recent meetings dominate career)
    and Laplace smoothing (wins+1)/(n+2) on the DECAYED counts, so a 3-0
    record does not read as certainty. Meetings timestamped at/after ``now``
    are excluded (no look-ahead — a future-dated source glitch must not
    inform the prediction)."""
    now = require_aware("now", now)
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive; got {half_life_days}")
    relevant = sorted(
        (m for m in meetings
         if m.involves(subject_id)
         and require_aware("meeting.start_time", m.start_time) < now),
        key=lambda m: m.start_time, reverse=True)
    weighted_wins = 0.0
    weighted_total = 0.0
    career_w = career_l = l20_w = l20_l = 0
    for index, meeting in enumerate(relevant):
        start = require_aware("meeting.start_time", meeting.start_time)
        age_days = max(0.0, (now - start).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / half_life_days)
        won = meeting.won_by(subject_id)
        weighted_total += weight
        if won:
            weighted_wins += weight
            career_w += 1
            if index < 20:
                l20_w += 1
        else:
            career_l += 1
            if index < 20:
                l20_l += 1
    rate = (weighted_wins + 1.0) / (weighted_total + 2.0)
    return H2HFeature(rate=rate, n_meetings=len(relevant),
                      career_wins=career_w, career_losses=career_l,
                      last20_wins=l20_w, last20_losses=l20_l)


@dataclass(frozen=True)
class FormFeature:
    """Recency-weighted win rate over the last ``last_n`` results."""

    rate: float
    n_results: int


def form_feature(results: list[MatchResult], player_id: str, now: datetime, *,
                 last_n: int, hot_n: int) -> FormFeature:
    """Win rate over the player's last ``last_n`` completed matches, with the
    most recent ``hot_n`` weighted 2x. Only matches strictly before ``now``
    count (no look-ahead: a result timestamped after the scan instant cannot
    inform it)."""
    now = require_aware("now", now)
    if last_n < 1 or hot_n < 0 or hot_n > last_n:
        raise ValueError(f"invalid window: last_n={last_n}, hot_n={hot_n}")
    past = sorted(
        (r for r in results
         if r.involves(player_id) and require_aware("start_time", r.start_time) < now),
        key=lambda r: r.start_time, reverse=True)[:last_n]
    if not past:
        return FormFeature(rate=0.5, n_results=0)
    weighted_wins = 0.0
    weighted_total = 0.0
    for index, result in enumerate(past):
        weight = 2.0 if index < hot_n else 1.0
        weighted_total += weight
        if result.won_by(player_id):
            weighted_wins += weight
    return FormFeature(rate=weighted_wins / weighted_total, n_results=len(past))


def form_differential(subject: FormFeature, opponent: FormFeature) -> float:
    """Subject-minus-opponent form rate, in [-1, 1]."""
    return subject.rate - opponent.rate


@dataclass(frozen=True)
class CommonOpponentFeature:
    """Shared-opponent comparison over a recent window.

    ``differential`` is the mean per-opponent win-rate gap (subject minus
    opponent), in [-1, 1]. ``net_units`` is the display stat for the alert
    basis line: sum over shared opponents of (subject net W-L) - (opponent
    net W-L)."""

    differential: float
    n_common: int
    net_units: int


def common_opponent_feature(subject_results: list[MatchResult],
                            opponent_results: list[MatchResult],
                            subject_id: str, opponent_id: str, now: datetime,
                            *, window_days: int) -> CommonOpponentFeature:
    """Compare the two players through opponents both faced in the last
    ``window_days`` days. Meetings between the two players themselves are
    excluded — that signal belongs to the H2H feature."""
    now = require_aware("now", now)
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1; got {window_days}")
    cutoff_seconds = window_days * 86400.0

    def _tally(results: list[MatchResult], player_id: str,
               exclude_id: str) -> dict[str, tuple[int, int]]:
        record: dict[str, tuple[int, int]] = {}
        for result in results:
            if not result.involves(player_id):
                continue
            start = require_aware("start_time", result.start_time)
            age = (now - start).total_seconds()
            if age < 0 or age > cutoff_seconds:
                continue
            other = result.opponent_of(player_id)
            if other == exclude_id:
                continue
            wins, losses = record.get(other, (0, 0))
            if result.won_by(player_id):
                record[other] = (wins + 1, losses)
            else:
                record[other] = (wins, losses + 1)
        return record

    subject_record = _tally(subject_results, subject_id, opponent_id)
    opponent_record = _tally(opponent_results, opponent_id, subject_id)
    shared = sorted(set(subject_record) & set(opponent_record))
    if not shared:
        return CommonOpponentFeature(differential=0.0, n_common=0, net_units=0)

    gaps: list[float] = []
    net_units = 0
    for other in shared:
        s_wins, s_losses = subject_record[other]
        o_wins, o_losses = opponent_record[other]
        s_rate = s_wins / (s_wins + s_losses)
        o_rate = o_wins / (o_wins + o_losses)
        gaps.append(s_rate - o_rate)
        net_units += (s_wins - s_losses) - (o_wins - o_losses)
    return CommonOpponentFeature(differential=sum(gaps) / len(gaps),
                                 n_common=len(shared), net_units=net_units)
