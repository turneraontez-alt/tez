"""Model layer: feature extraction, the logistic blend and its
missing-feature contract, and the Platt calibration port."""
import math
from datetime import timedelta

import pytest

from tests.tt_edge_fixtures import NOW
from tt_edge.freshness import NaiveTimestampError
from tt_edge.model.calibration import (IDENTITY, SLOPE_MIN, CalibrationError,
                                       fit_platt)
from tt_edge.model.features import (MatchResult, common_opponent_feature,
                                    form_differential, form_feature,
                                    h2h_feature)
from tt_edge.model.win_prob import blend_probability


def _match(days_ago: float, winner: str, home: str = "A", away: str = "B"):
    return MatchResult(start_time=NOW - timedelta(days=days_ago),
                       winner_id=winner, home_id=home, away_id=away)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class TestH2HFeature:
    def test_laplace_smoothing_on_recent_meetings(self):
        meetings = [_match(0.01, "A"), _match(0.02, "A"), _match(0.03, "A"),
                    _match(0.04, "B")]
        feat = h2h_feature(meetings, "A", NOW, half_life_days=365.0)
        # Weights ~1 -> (3+1)/(4+2) = 2/3.
        assert feat.rate == pytest.approx(2 / 3, abs=1e-4)
        assert (feat.career_wins, feat.career_losses) == (3, 1)
        assert feat.n_meetings == 4

    def test_age_decay_discounts_old_wins(self):
        old_win = h2h_feature([_match(365, "A")], "A", NOW,
                              half_life_days=365.0)
        new_win = h2h_feature([_match(0.001, "A")], "A", NOW,
                              half_life_days=365.0)
        # Weight 0.5 -> (0.5+1)/(0.5+2) = 0.6 vs ~(1+1)/(1+2) = 2/3.
        assert old_win.rate == pytest.approx(0.6, abs=1e-4)
        assert new_win.rate == pytest.approx(2 / 3, abs=1e-4)
        assert old_win.rate < new_win.rate

    def test_last20_window(self):
        # 20 recent wins, then 5 older losses: career 20-5, L20 20-0.
        meetings = [_match(i + 1, "A") for i in range(20)]
        meetings += [_match(100 + i, "B") for i in range(5)]
        feat = h2h_feature(meetings, "A", NOW, half_life_days=365.0)
        assert (feat.career_wins, feat.career_losses) == (20, 5)
        assert (feat.last20_wins, feat.last20_losses) == (20, 0)

    def test_meetings_of_other_players_ignored(self):
        feat = h2h_feature([_match(1, "C", home="C", away="D")], "A", NOW,
                           half_life_days=365.0)
        assert feat.n_meetings == 0

    def test_naive_timestamp_rejected(self):
        naive = MatchResult(start_time=NOW.replace(tzinfo=None),
                            winner_id="A", home_id="A", away_id="B")
        with pytest.raises(NaiveTimestampError):
            h2h_feature([naive], "A", NOW, half_life_days=365.0)


class TestFormFeature:
    def test_empty_history_is_neutral(self):
        feat = form_feature([], "A", NOW, last_n=15, hot_n=5)
        assert feat.rate == 0.5
        assert feat.n_results == 0

    def test_hot_window_weighted_double(self):
        # 5 recent wins (2x) + 10 older losses: 10/20 = 0.5, where an
        # unweighted mean would be 1/3.
        results = [_match(i + 1, "A") for i in range(5)]
        results += [_match(10 + i, "B") for i in range(10)]
        feat = form_feature(results, "A", NOW, last_n=15, hot_n=5)
        assert feat.rate == pytest.approx(0.5)
        assert feat.n_results == 15

    def test_only_last_n_count(self):
        # 15 recent losses then 10 ancient wins: the wins fall outside.
        results = [_match(i + 1, "B") for i in range(15)]
        results += [_match(50 + i, "A") for i in range(10)]
        feat = form_feature(results, "A", NOW, last_n=15, hot_n=5)
        assert feat.rate == 0.0

    def test_no_lookahead(self):
        future = MatchResult(start_time=NOW + timedelta(minutes=1),
                             winner_id="A", home_id="A", away_id="B")
        feat = form_feature([future], "A", NOW, last_n=15, hot_n=5)
        assert feat.n_results == 0

    def test_differential(self):
        hot = form_feature([_match(1, "A")], "A", NOW, last_n=15, hot_n=5)
        cold = form_feature([_match(1, "C", home="B", away="C")], "B", NOW,
                            last_n=15, hot_n=5)
        assert form_differential(hot, cold) == pytest.approx(1.0)


class TestCommonOpponents:
    def test_shared_opponent_differential_and_net(self):
        # A beat X twice and Y once; B lost to X once and split with Y.
        a_results = [_match(5, "A", home="A", away="X"),
                     _match(6, "A", home="A", away="X"),
                     _match(7, "A", home="A", away="Y")]
        b_results = [_match(5, "X", home="B", away="X"),
                     _match(6, "B", home="B", away="Y"),
                     _match(7, "Y", home="B", away="Y")]
        feat = common_opponent_feature(a_results, b_results, "A", "B", NOW,
                                       window_days=60)
        assert feat.n_common == 2
        # Per-opponent rate gaps: X: 1.0 - 0.0 = 1.0; Y: 1.0 - 0.5 = 0.5.
        assert feat.differential == pytest.approx(0.75)
        # Net units: A(+2 +1) - B(-1 +0) = 4.
        assert feat.net_units == 4

    def test_window_excludes_old_matches(self):
        a_results = [_match(61, "A", home="A", away="X")]
        b_results = [_match(5, "B", home="B", away="X")]
        feat = common_opponent_feature(a_results, b_results, "A", "B", NOW,
                                       window_days=60)
        assert feat.n_common == 0

    def test_direct_meetings_excluded(self):
        # A beating B is H2H signal, not common-opponent signal.
        a_results = [_match(5, "A", home="A", away="B")]
        b_results = [_match(5, "A", home="A", away="B")]
        feat = common_opponent_feature(a_results, b_results, "A", "B", NOW,
                                       window_days=60)
        assert feat.n_common == 0


class TestBlend:
    W = dict(w_h2h=0.45, w_form=0.35, w_common=0.20, scale=2.5)

    def _h2h(self, rate=0.727, n=20):
        from tt_edge.model.features import H2HFeature
        return H2HFeature(rate=rate, n_meetings=n, career_wins=0,
                          career_losses=0, last20_wins=0, last20_losses=0)

    def _form(self, rate, n=15):
        from tt_edge.model.features import FormFeature
        return FormFeature(rate=rate, n_results=n)

    def _common(self, diff=0.75, n=2):
        from tt_edge.model.features import CommonOpponentFeature
        return CommonOpponentFeature(differential=diff, n_common=n,
                                     net_units=4)

    def test_full_blend(self):
        result = blend_probability(self._h2h(), self._form(0.85),
                                   self._form(0.25), self._common(), **self.W)
        score = 0.45 * (2 * 0.727 - 1) + 0.35 * 0.6 + 0.20 * 0.75
        assert result.score == pytest.approx(score)
        assert result.p_home == pytest.approx(_sigmoid(2.5 * score))

    def test_missing_h2h_renormalizes(self):
        result = blend_probability(None, self._form(0.85), self._form(0.25),
                                   self._common(), **self.W)
        score = (0.35 * 0.6 + 0.20 * 0.75) / 0.55
        assert result.h2h_component is None
        assert result.p_home == pytest.approx(_sigmoid(2.5 * score))

    def test_missing_common_renormalizes(self):
        result = blend_probability(self._h2h(), self._form(0.85),
                                   self._form(0.25), self._common(n=0),
                                   **self.W)
        score = (0.45 * (2 * 0.727 - 1) + 0.35 * 0.6) / 0.80
        assert result.common_component is None
        assert result.p_home == pytest.approx(_sigmoid(2.5 * score))

    def test_everything_missing_is_coin_flip(self):
        result = blend_probability(None, self._form(0.5, n=0),
                                   self._form(0.5, n=0), None, **self.W)
        assert result.p_home == 0.5

    def test_clamped_away_from_certainty(self):
        args = dict(self.W)
        args["scale"] = 50.0
        result = blend_probability(self._h2h(rate=0.99), self._form(1.0),
                                   self._form(0.0), self._common(diff=1.0),
                                   **args)
        assert result.p_home == 0.98


class TestCalibration:
    def _pairs(self, spec):
        """spec: list of (raw_p, n, wins) -> deterministic outcome pairs."""
        pairs = []
        for raw_p, n, wins in spec:
            pairs.extend([(raw_p, 1.0)] * wins)
            pairs.extend([(raw_p, 0.0)] * (n - wins))
        return pairs

    def test_refuses_thin_data(self):
        with pytest.raises(CalibrationError):
            fit_platt(self._pairs([(0.6, 10, 6)]), min_rows=300)

    def test_well_calibrated_data_fits_near_identity(self):
        pairs = self._pairs([(0.2, 40, 8), (0.35, 40, 14), (0.5, 40, 20),
                             (0.65, 40, 26), (0.8, 40, 32)])
        fit = fit_platt(pairs, min_rows=100)
        assert fit.converged
        assert fit.transform.intercept == pytest.approx(0.0, abs=0.15)
        assert fit.transform.slope == pytest.approx(1.0, abs=0.15)

    def test_underconfident_model_gets_sharpened(self):
        # Model says 0.6, reality is 0.8: the fit must beat identity Brier
        # and push 0.6 upward.
        pairs = self._pairs([(0.6, 100, 80), (0.4, 100, 20)])
        fit = fit_platt(pairs, min_rows=100)
        assert fit.brier_fitted < fit.brier_identity
        assert fit.transform.apply(0.6) > 0.65

    def test_slope_clamped_on_adversarial_data(self):
        # Outcomes anti-correlated with the model: raw slope would go
        # negative; the clamp pins it at SLOPE_MIN.
        pairs = self._pairs([(0.8, 100, 20), (0.2, 100, 80)])
        fit = fit_platt(pairs, min_rows=100)
        assert fit.transform.slope == pytest.approx(SLOPE_MIN)

    def test_identity_transform_round_trips(self):
        assert IDENTITY.apply(0.73) == pytest.approx(0.73, abs=1e-9)
        assert IDENTITY.apply(0.02) == pytest.approx(0.02, abs=1e-9)
