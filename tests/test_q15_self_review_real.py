"""End-to-end verification of threshold auto-adaptation against REAL settled markets.

The synthetic unit tests in ``test_q15_self_review.py`` prove the adaptation
state machine in isolation. This module instead replays the *actual* settled
final-10m predictions recorded in Postgres (``q15_prediction_checkpoints``)
through a real :class:`SelfReviewEngine` and asserts the four production
invariants on production-shaped data:

  1. nudges fire ONLY after an adaptable failure mode repeats (>= min_samples
     with a high enough loss-rate) -- never on a one-off loss;
  2. every adapted value stays clamped inside its ``[min_value, max_value]``
     band, even under sustained failure;
  3. once the failure pattern stops, the value decays back toward baseline;
  4. no adaptation happens while a learning circuit breaker was active (either
     at decision time, frozen into the features, or right now).

The engine is always constructed with a DISABLED store, so this verification is
strictly read-only against the database -- it loads recorded checkpoints but
never writes to any ``q15_*`` table.

When no database (or no settled actionable data) is available the relevant
tests skip with an explanatory reason rather than fail, so the suite still runs
in an isolated environment.

Run as a standalone monitoring pass with::

    python -m tests.test_q15_self_review_real        # from artifacts/kalshi-monitor
"""
import os
import sys
import unittest
from collections import Counter
from itertools import cycle, islice

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.self_review import SelfReviewEngine, ADAPT_RULES  # noqa: E402
from q15_upgrade.window_focus import FocusSettings  # noqa: E402

# Failure modes that map one-to-one to an adaptable decision threshold.
ADAPTABLE_MODES = set(ADAPT_RULES)


class _DisabledStore:
    """A store the engine treats as absent -> pure in-memory, no DB writes."""

    enabled = False


def _load_real_checkpoints():
    """Return (settled_10m_rows, prior_side_by_ticker) from Postgres, or (None, why).

    Read-only. Only SELECTs recorded checkpoints; never writes.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None, "DATABASE_URL not set"
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as exc:  # pragma: no cover - env without driver
        return None, f"psycopg2 unavailable: {exc}"
    try:
        conn = psycopg2.connect(url)
    except Exception as exc:  # pragma: no cover - no reachable db
        return None, f"cannot connect: {exc}"
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT ticker, predicted_side FROM q15_prediction_checkpoints WHERE checkpoint='15m'")
        prior = {r["ticker"]: r["predicted_side"] for r in cur.fetchall()}
        cur.execute(
            "SELECT ticker, asset, predicted_side, adjusted_yes_probability, raw_yes_probability, "
            "side_probability, consensus, required_consensus, required_side_probability, sample_count, feature_snapshot, outcome "
            "FROM q15_prediction_checkpoints "
            "WHERE checkpoint='10m' AND outcome IS NOT NULL ORDER BY captured_at"
        )
        rows = [dict(r) for r in cur.fetchall()]
        return (rows, prior), None
    except Exception as exc:  # pragma: no cover
        return None, f"query failed: {exc}"
    finally:
        conn.close()


def _prediction_from_row(row, prior, *, breaker=False):
    """Rebuild the engine's prediction contract from a recorded checkpoint row.

    ``required_consensus`` and ``required_side_probability`` are now persisted on
    each checkpoint at capture time, so the low_consensus / high_consensus
    diagnoses (and any future side-confidence failure mode) are fully
    reconstructable from recorded data -- the replay sees exactly the consensus
    and side-probability bars the live engine used at decision time.
    """
    features = dict(row.get("feature_snapshot") or {})
    if breaker:
        features["circuit_breaker_active"] = True
    yes_prob = row.get("adjusted_yes_probability")
    if yes_prob is None:
        yes_prob = row.get("raw_yes_probability")
    return {
        "side": row.get("predicted_side"),
        "yes_probability": yes_prob,
        "side_probability": row.get("side_probability"),
        "consensus": row.get("consensus"),
        "required_consensus": row.get("required_consensus"),
        "required_side_probability": row.get("required_side_probability"),
        "sample_count": row.get("sample_count"),
        "features": features,
    }, prior.get(row.get("ticker"))


_DATA, _SKIP_REASON = _load_real_checkpoints()


def _diagnose_rows(engine, rows, prior):
    """Diagnose each recorded row (no adaptation) -> list of (outcome, tags)."""
    out = []
    for row in rows:
        pred, prior_side = _prediction_from_row(row, prior)
        outcome, tags, _primary = engine._diagnose(
            side=str(row.get("predicted_side") or "").upper(),
            actual=str(row.get("outcome") or "").upper(),
            prediction=pred,
            features=pred["features"],
            prior_side=prior_side,
            decision_breaker=False,
        )
        out.append((outcome, tags, row))
    return out


@unittest.skipIf(_DATA is None, f"no real settled checkpoints: {_SKIP_REASON}")
class RealSettledMarketTests(unittest.TestCase):
    """Replay recorded settled markets and assert the adaptation invariants."""

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.prior = _DATA
        # Classify recorded rows by the live engine's own diagnosis.
        diag = _diagnose_rows(SelfReviewEngine(_DisabledStore(), FocusSettings()), cls.rows, cls.prior)
        cls.adaptable_losses = {mode: [] for mode in ADAPTABLE_MODES}
        cls.wins = []
        for outcome, tags, row in diag:
            if outcome == "wrong":
                for mode in ADAPTABLE_MODES:
                    if mode in tags:
                        cls.adaptable_losses[mode].append(row)
            elif outcome == "correct":
                cls.wins.append(row)

    def _engine(self, **kw):
        defaults = dict(
            self_review_adapt_min_samples=4,
            self_review_adapt_loss_rate=0.5,
            self_review_adapt_eval_window=4,
            self_review_adapt_decay_window=6,
        )
        defaults.update(kw)
        settings = FocusSettings(**defaults)
        return SelfReviewEngine(_DisabledStore(), settings), settings

    def _replay(self, engine, rows, *, prefix, breaker=False):
        """Feed recorded rows through the public review() path with unique keys."""
        for i, row in enumerate(rows):
            pred, prior_side = _prediction_from_row(row, self.prior, breaker=breaker)
            engine.review(
                ticker=f"{prefix}-{i}",
                asset=row.get("asset"),
                market_close=None,
                prediction=pred,
                agreement=None,
                decision_state=None,
                actual_result=str(row.get("outcome") or "").upper(),
                breaker_active=breaker,
                prior_side=prior_side,
            )

    def _assert_within_bands(self, engine):
        for attr, state in engine._adapt.items():
            self.assertGreaterEqual(
                state["value"], state["min_value"] - 1e-9,
                f"{attr} dropped below min band",
            )
            self.assertLessEqual(
                state["value"], state["max_value"] + 1e-9,
                f"{attr} exceeded max band",
            )

    # --- Invariant 1 + 2 on the live recorded set --------------------------
    def test_recorded_set_replays_in_bounds(self):
        """Replaying every recorded settled market keeps all values in-band and
        triggers a nudge only if an adaptable mode actually repeated enough."""
        engine, settings = self._engine(
            self_review_adapt_min_samples=8,
            self_review_adapt_loss_rate=0.55,
        )
        self._replay(engine, self.rows, prefix="real")
        self._assert_within_bands(engine)

        nudges = [e for e in engine._events if e["action"] == "nudge"]
        # A nudge is legitimate ONLY if some adaptable mode reached min_samples.
        max_adaptable = max((len(v) for v in self.adaptable_losses.values()), default=0)
        if max_adaptable < 8:
            self.assertEqual(
                nudges, [],
                "nudge fired on real data without an adaptable failure mode "
                f"repeating >= min_samples (max adaptable losses={max_adaptable})",
            )

    # --- Invariant 1: nudges require repetition; Invariant 2: clamps -------
    def test_real_loss_shape_nudge_requires_repetition_and_clamps(self):
        """Use real losing feature snapshots to manufacture a repeated failure
        and confirm: no nudge before min_samples, a bounded nudge at min_samples,
        and the value never escapes its band under sustained failure."""
        mode = next((m for m, rws in self.adaptable_losses.items() if rws), None)
        if mode is None:
            self.skipTest("no recorded loss maps to an adaptable threshold")
        attr = ADAPT_RULES[mode]["attr"]
        loss_rows = self.adaptable_losses[mode]

        engine, settings = self._engine()
        baseline = getattr(settings, attr)
        state = engine._adapt[attr]
        min_samples = 4

        # One short of the trigger: must NOT have nudged yet.
        self._replay(engine, list(islice(cycle(loss_rows), min_samples - 1)), prefix="pre")
        self.assertEqual(getattr(settings, attr), baseline,
                         "adaptation fired before the failure mode repeated enough")

        # The triggering loss: a single bounded nudge of exactly one step.
        self._replay(engine, list(islice(cycle(loss_rows), 1)), prefix="trig")
        self.assertGreater(getattr(settings, attr), baseline,
                           "no nudge after the failure mode repeated to min_samples")
        self.assertAlmostEqual(getattr(settings, attr), baseline + state["step"], places=6)
        self._assert_within_bands(engine)

        # Sustained failure: value clamps at max_value, never beyond.
        for block in range(40):
            self._replay(engine, list(islice(cycle(loss_rows), min_samples)), prefix=f"c{block}")
        self._assert_within_bands(engine)
        self.assertLessEqual(getattr(settings, attr), state["max_value"] + 1e-9)

    # --- Invariant 3: decay back toward baseline --------------------------
    def test_real_shape_decays_back_toward_baseline(self):
        """After a real-loss-driven nudge, feeding recorded WINNING snapshots
        (the pattern stopped) decays the value back toward baseline."""
        mode = next((m for m, rws in self.adaptable_losses.items() if rws), None)
        if mode is None:
            self.skipTest("no recorded loss maps to an adaptable threshold")
        if not self.wins:
            self.skipTest("no recorded winning checkpoints to drive decay")
        attr = ADAPT_RULES[mode]["attr"]
        loss_rows = self.adaptable_losses[mode]

        engine, settings = self._engine()
        baseline = getattr(settings, attr)
        # Drive a nudge with real losing snapshots.
        self._replay(engine, list(islice(cycle(loss_rows), 4)), prefix="nud")
        nudged = getattr(settings, attr)
        self.assertGreater(nudged, baseline)

        # Now the pattern stops: feed many recorded wins -> probation clears,
        # then decay relaxes the value back toward baseline.
        self._replay(engine, list(islice(cycle(self.wins), 30)), prefix="win")
        self._assert_within_bands(engine)
        self.assertLess(getattr(settings, attr), nudged,
                        "value did not decay after the failure pattern stopped")
        self.assertGreaterEqual(getattr(settings, attr), baseline - 1e-9)
        decays = [e for e in engine._events if e["action"] == "decay"]
        self.assertTrue(decays, "expected at least one decay event")

    # --- Invariant 4: circuit breaker gates adaptation --------------------
    def test_decision_time_breaker_blocks_real_loss_adaptation(self):
        """Real losing snapshots that carry a decision-time breaker must never
        drive a nudge, no matter how often the failure repeats."""
        mode = next((m for m, rws in self.adaptable_losses.items() if rws), None)
        if mode is None:
            self.skipTest("no recorded loss maps to an adaptable threshold")
        attr = ADAPT_RULES[mode]["attr"]
        loss_rows = self.adaptable_losses[mode]

        engine, settings = self._engine()
        baseline = getattr(settings, attr)
        # 5x the trigger count, all under a decision-time breaker.
        self._replay(engine, list(islice(cycle(loss_rows), 20)), prefix="brk", breaker=True)
        self.assertEqual(getattr(settings, attr), baseline,
                         "adaptation occurred while a circuit breaker was active")
        self.assertEqual([e for e in engine._events if e["action"] == "nudge"], [])


def _monitoring_report():  # pragma: no cover - manual diagnostics
    """Print a human-readable verification/monitoring summary."""
    if _DATA is None:
        print(f"[self-review verify] skipped: {_SKIP_REASON}")
        return
    rows, prior = _DATA
    engine = SelfReviewEngine(_DisabledStore(), FocusSettings())
    diag = _diagnose_rows(engine, rows, prior)
    outcomes = Counter(o for o, _t, _r in diag)
    loss_modes = Counter()
    win_modes = Counter()
    for outcome, tags, _row in diag:
        if outcome == "wrong":
            loss_modes.update(tags)
        elif outcome == "correct":
            win_modes.update(tags)
    print("=" * 64)
    print("Threshold auto-adapt verification against REAL settled markets")
    print("=" * 64)
    print(f"recorded settled 10m predictions : {len(rows)}")
    print(f"graded outcomes                  : {dict(outcomes)}")
    print(f"loss failure modes               : {dict(loss_modes)}")
    print(f"win success patterns             : {dict(win_modes)}")
    adaptable = {m: loss_modes.get(m, 0) for m in ADAPTABLE_MODES}
    print(f"adaptable-mode loss counts       : {adaptable}")
    print(f"adaptation thresholds            : "
          f"min_samples={FocusSettings().self_review_adapt_min_samples}, "
          f"loss_rate={FocusSettings().self_review_adapt_loss_rate}")
    repeated = [m for m, n in adaptable.items() if n >= FocusSettings().self_review_adapt_min_samples]
    if repeated:
        print(f"=> adaptable modes that repeated enough to nudge: {repeated}")
    else:
        print("=> no adaptable failure mode has repeated to the nudge threshold; "
              "correctly NO live adaptation on current data (invariant 1).")
    print("Note: required_consensus is now persisted on each checkpoint, so the "
          "low_consensus/high_consensus diagnoses are reconstructed directly "
          "from recorded data (replaying the live decision-time consensus bar).")
    print("=" * 64)


if __name__ == "__main__":
    _monitoring_report()
