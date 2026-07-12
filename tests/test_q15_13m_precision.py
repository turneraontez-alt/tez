from __future__ import annotations

import unittest

from q15_upgrade.interval_research.precision13 import EXPECTED_ASSETS, evaluate_slate


def _slate(probabilities):
    return [
        {
            "ticker": f"KX{asset}",
            "asset": asset,
            "calibrated_yes_probability": probabilities[asset],
        }
        for asset in sorted(EXPECTED_ASSETS)
    ]


def _prior(probabilities):
    return {
        f"KX{asset}": {"calibrated_yes_probability": probabilities[asset]}
        for asset in EXPECTED_ASSETS
    }


class Precision13PolicyTest(unittest.TestCase):
    def test_synchronized_yes_regime_emits_yes(self):
        current = {asset: 0.62 for asset in EXPECTED_ASSETS}
        prior = {asset: 0.65 for asset in EXPECTED_ASSETS}
        decisions = evaluate_slate(_slate(current), _prior(prior))
        self.assertEqual({row["decision"] for row in decisions}, {"YES"})

    def test_yes_regime_allows_one_laggard_but_not_two(self):
        current = {asset: 0.65 for asset in EXPECTED_ASSETS}
        current["HYPE"] = 0.49
        prior = {asset: 0.65 for asset in EXPECTED_ASSETS}
        decisions = evaluate_slate(_slate(current), _prior(prior))
        self.assertEqual(sum(row["decision"] == "YES" for row in decisions), 6)

        current["DOGE"] = 0.49
        decisions = evaluate_slate(_slate(current), _prior(prior))
        self.assertNotIn("YES", {row["decision"] for row in decisions})

    def test_confirmed_no_expansion_emits_only_qualified_rows(self):
        current = {asset: 0.44 for asset in EXPECTED_ASSETS}
        current["BTC"] = 0.45
        current["XRP"] = 0.32
        prior = {asset: 0.48 for asset in EXPECTED_ASSETS}
        prior["XRP"] = 0.42
        decisions = evaluate_slate(_slate(current), _prior(prior))
        by_asset = {row["asset"]: row for row in decisions}
        self.assertEqual(by_asset["XRP"]["decision"], "NO")
        self.assertEqual(by_asset["XRP"]["reason"], "CONFIRMED_NO_EXPANSION")
        self.assertTrue(all(
            row["decision"] == "ABSTAIN"
            for asset, row in by_asset.items()
            if asset != "XRP"
        ))

    def test_missing_15m_probability_fails_closed(self):
        current = {asset: 0.62 for asset in EXPECTED_ASSETS}
        prior = _prior({asset: 0.62 for asset in EXPECTED_ASSETS})
        prior.pop("KXSOL")
        decisions = evaluate_slate(_slate(current), prior)
        sol = next(row for row in decisions if row["asset"] == "SOL")
        self.assertEqual(sol["decision"], "ABSTAIN")
        self.assertEqual(sol["reason"], "MISSING_15M_PROBABILITY")

    def test_incomplete_slate_fails_closed(self):
        current = {asset: 0.62 for asset in EXPECTED_ASSETS}
        rows = _slate(current)[:-1]
        decisions = evaluate_slate(rows, _prior(current))
        self.assertEqual({row["decision"] for row in decisions}, {"ABSTAIN"})
        self.assertEqual({row["reason"] for row in decisions}, {"INCOMPLETE_13M_SLATE"})


if __name__ == "__main__":
    unittest.main()
