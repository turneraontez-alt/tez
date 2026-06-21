"""Read-only "setup miner": searches settled predictions for a combination of
decision-time conditions that has reliably predicted the final outcome — with the
anti-overfitting guards baked in, so a fluke is never reported as a rule.

It NEVER changes a prediction, weight, or order. It only reads resolved rows and
reports what, if anything, holds up. The guards (all enforced, not optional):

  * TIME-ORDERED SPLIT — setups are discovered on an earlier train slice and
    confirmed on a later, untouched test slice. "Does it still work on data not
    used to discover it?" is answered directly, never assumed.
  * MINIMUM SUPPORT — a setup must occur at least ``min_support_train`` times in
    train (and ``min_support_test`` in test) or it is not even considered. Kills
    rules "so specific they only match a few cases".
  * MULTIPLE-TESTING CORRECTION — every setup with enough support counts as one
    hypothesis; significance uses a Bonferroni-adjusted alpha (``alpha / H``), so
    "100% on the luckiest of thousands tried" does not pass.
  * WILSON LOWER BOUND, not the raw rate — "100% on 8 cases" is reported as its
    conservative ≥X% lower bound, so small-sample perfection is never sold as a
    guarantee.
  * NO LEAKAGE — only decision-time inputs (the recorded feature_json + regime)
    are used as conditions; the outcome is the target, never an input.

This is pure logic + formatting (no DB, no network) so it is fully unit-testable.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Mapping, Sequence


# --------------------------------------------------------------------------- #
# statistics (standalone so the module has no dependencies)
# --------------------------------------------------------------------------- #
def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for ``successes/n`` (0 if n==0)."""
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))) / denom
    return max(0.0, center - margin)


def binom_sf(k: int, n: int, p: float = 0.5) -> float:
    """One-sided exact tail P(X >= k) for X ~ Binomial(n, p). Used as the per-setup
    significance test against a coin flip before any multiple-testing correction."""
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * (p ** i) * ((1.0 - p) ** (n - i)) for i in range(k, n + 1))


# --------------------------------------------------------------------------- #
# conditions & setups
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Condition:
    """A single decision-time predicate over one feature (or the regime)."""
    feature: str
    op: str          # "ge" | "lt" | "eq"
    value: Any

    def label(self) -> str:
        if self.op == "eq":
            return f"{self.feature}={self.value}"
        sign = "≥" if self.op == "ge" else "<"
        return f"{self.feature}{sign}{self.value:g}" if isinstance(self.value, (int, float)) else f"{self.feature}{sign}{self.value}"

    def test(self, features: Mapping[str, Any], regime: Any) -> bool | None:
        """True/False if the predicate is decidable for this row, else None
        (missing data — the row neither matches nor counts against the setup)."""
        if self.feature == "regime":
            if regime is None:
                return None
            return str(regime) == str(self.value)
        if self.feature not in features:
            return None
        v = features.get(self.feature)
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if self.op == "ge":
            return v >= float(self.value)
        if self.op == "lt":
            return v < float(self.value)
        return None


@dataclass(frozen=True)
class Setup:
    """A conjunction of conditions (all must hold)."""
    conditions: tuple[Condition, ...]

    def label(self) -> str:
        return " AND ".join(c.label() for c in self.conditions)

    def matches(self, features: Mapping[str, Any], regime: Any) -> bool | None:
        """True if all conditions hold; None if any condition is undecidable for
        this row (missing data) — such rows are excluded, never guessed."""
        decided_all = True
        for c in self.conditions:
            r = c.test(features, regime)
            if r is None:
                return None
            if not r:
                decided_all = False
        return decided_all


# --------------------------------------------------------------------------- #
# config / results
# --------------------------------------------------------------------------- #
@dataclass
class MineConfig:
    checkpoint: str = "10M"
    min_support_train: int = 30
    min_support_test: int = 10
    max_conditions: int = 2
    train_fraction: float = 0.70
    wilson_z: float = 1.96
    min_wilson_lb: float = 0.60     # out-of-sample "usefully better than a coin flip"
    alpha: float = 0.05             # family-wise error rate (Bonferroni-corrected)
    include_regime: bool = True

    @classmethod
    def from_env(cls, checkpoint: str = "10M") -> "MineConfig":
        def _i(name, d):
            try:
                return int(float(os.environ.get(name, d)))
            except (TypeError, ValueError):
                return d

        def _f(name, d):
            try:
                return float(os.environ.get(name, d))
            except (TypeError, ValueError):
                return d
        return cls(
            checkpoint=checkpoint,
            min_support_train=_i("Q15_SETUP_MINER_MIN_SUPPORT_TRAIN", 30),
            min_support_test=_i("Q15_SETUP_MINER_MIN_SUPPORT_TEST", 10),
            max_conditions=_i("Q15_SETUP_MINER_MAX_CONDITIONS", 2),
            train_fraction=_f("Q15_SETUP_MINER_TRAIN_FRACTION", 0.70),
            min_wilson_lb=_f("Q15_SETUP_MINER_MIN_WILSON_LB", 0.60),
            alpha=_f("Q15_SETUP_MINER_ALPHA", 0.05),
        )


@dataclass
class SetupFinding:
    setup: Setup
    side: str                       # outcome direction assigned from TRAIN majority
    train_n: int
    train_correct: int
    test_n: int
    test_correct: int
    p_value_train: float
    alpha_adjusted: float
    notes: list[str] = field(default_factory=list)

    @property
    def train_accuracy(self) -> float | None:
        return self.train_correct / self.train_n if self.train_n else None

    @property
    def test_accuracy(self) -> float | None:
        return self.test_correct / self.test_n if self.test_n else None

    def train_wilson(self, z: float = 1.96) -> float:
        return wilson_lower_bound(self.train_correct, self.train_n, z)

    def test_wilson(self, z: float = 1.96) -> float:
        return wilson_lower_bound(self.test_correct, self.test_n, z)

    def significant_in_train(self) -> bool:
        return self.p_value_train <= self.alpha_adjusted

    def validated_out_of_sample(self, cfg: MineConfig) -> bool:
        """Held up on data it was NOT discovered on: enough test support AND its
        test Wilson lower bound clears the usefulness gate."""
        return (self.significant_in_train()
                and self.test_n >= cfg.min_support_test
                and self.test_wilson(cfg.wilson_z) >= cfg.min_wilson_lb)


@dataclass
class MineReport:
    checkpoint: str
    total_rows: int
    train_rows: int
    test_rows: int
    hypotheses_tested: int
    alpha_adjusted: float
    findings: list[SetupFinding]
    note: str | None = None

    def validated(self, cfg: MineConfig) -> list[SetupFinding]:
        return [f for f in self.findings if f.validated_out_of_sample(cfg)]

    def perfect_in_train(self) -> list[SetupFinding]:
        return [f for f in self.findings if f.train_accuracy == 1.0]


# --------------------------------------------------------------------------- #
# candidate generation
# --------------------------------------------------------------------------- #
def _candidate_conditions(train: Sequence[Mapping[str, Any]], cfg: MineConfig) -> list[Condition]:
    """Build the predicate vocabulary from TRAIN only (never the test slice).

    Numeric features get a sign split (``≥0`` / ``<0``) — the most robust, least
    overfit binning. The regime (a recorded categorical) gets an equality
    predicate per value with enough support. Constant or near-absent features are
    dropped so they cannot manufacture a vacuous rule."""
    n = len(train)
    feat_seen: dict[str, int] = {}
    feat_pos: dict[str, int] = {}
    regimes: dict[str, int] = {}
    for row in train:
        feats = row.get("features") or {}
        for k, v in feats.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            feat_seen[k] = feat_seen.get(k, 0) + 1
            if fv >= 0:
                feat_pos[k] = feat_pos.get(k, 0) + 1
        if cfg.include_regime and row.get("regime") is not None:
            r = str(row["regime"])
            regimes[r] = regimes.get(r, 0) + 1

    conditions: list[Condition] = []
    min_seen = max(cfg.min_support_train, 1)
    for k, seen in feat_seen.items():
        if seen < min_seen:
            continue
        pos = feat_pos.get(k, 0)
        # Skip constant-sign features (both sides need enough support to matter).
        if pos < cfg.min_support_train or (seen - pos) < cfg.min_support_train:
            # still allow if at least one side is well-populated; both predicates
            # are added but support is re-checked per setup, so this only avoids
            # obviously degenerate (all-one-sign) features.
            if pos == 0 or pos == seen:
                continue
        conditions.append(Condition(k, "ge", 0.0))
        conditions.append(Condition(k, "lt", 0.0))
    for r, c in regimes.items():
        if c >= cfg.min_support_train:
            conditions.append(Condition("regime", "eq", r))
    return conditions


def _eval_setup(setup: Setup, rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
    """(matched, yes_count, no_count) over rows where the setup is decidable."""
    matched = yes = no = 0
    for row in rows:
        m = setup.matches(row.get("features") or {}, row.get("regime"))
        if m is None or not m:
            continue
        matched += 1
        if str(row.get("result")).upper() == "YES":
            yes += 1
        else:
            no += 1
    return matched, yes, no


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def mine(rows: Sequence[Mapping[str, Any]], config: MineConfig | None = None) -> MineReport:
    """Run the guarded search. ``rows`` are resolved predictions, each a mapping
    with: ``created_at`` (float, for time ordering), ``features`` (dict of
    decision-time values), optional ``regime`` (str), and ``result`` ('YES'/'NO').

    Returns a :class:`MineReport`. Findings are SORTED best-first by out-of-sample
    Wilson lower bound, then train Wilson lower bound. Nothing is sent or stored.
    """
    cfg = config or MineConfig()
    clean = [r for r in rows if str(r.get("result")).upper() in {"YES", "NO"}
             and r.get("created_at") is not None]
    clean.sort(key=lambda r: float(r["created_at"]))
    total = len(clean)

    # Need enough to form both a train slice (>= min_support_train) and a test
    # slice (>= min_support_test) at the configured split — otherwise no honest
    # discovery + validation is possible and we say so rather than guess.
    cut = int(total * cfg.train_fraction)
    train, test = clean[:cut], clean[cut:]
    if len(train) < cfg.min_support_train or len(test) < cfg.min_support_test:
        return MineReport(
            checkpoint=cfg.checkpoint, total_rows=total, train_rows=len(train),
            test_rows=len(test), hypotheses_tested=0, alpha_adjusted=cfg.alpha,
            findings=[],
            note=(f"insufficient data: need ≥{cfg.min_support_train} train and "
                  f"≥{cfg.min_support_test} test rows at a {cfg.train_fraction:.0%} "
                  f"split (have {len(train)}/{len(test)} of {total})."),
        )

    conditions = _candidate_conditions(train, cfg)
    # Enumerate setups of 1..max_conditions, never combining two conditions on the
    # same feature (e.g. "x≥0 AND x<0" is impossible / vacuous).
    setups: list[Setup] = []
    for k in range(1, cfg.max_conditions + 1):
        for combo in combinations(conditions, k):
            feats = [c.feature for c in combo]
            if len(set(feats)) != len(feats):
                continue
            setups.append(Setup(tuple(combo)))

    # First pass: which setups have enough TRAIN support to even be a hypothesis.
    scored: list[tuple[Setup, str, int, int]] = []  # (setup, side, train_n, train_correct)
    for s in setups:
        n, yes, no = _eval_setup(s, train)
        if n < cfg.min_support_train:
            continue
        side = "YES" if yes >= no else "NO"
        correct = yes if side == "YES" else no
        scored.append((s, side, n, correct))

    hypotheses = len(scored)
    alpha_adj = cfg.alpha / hypotheses if hypotheses else cfg.alpha

    findings: list[SetupFinding] = []
    for s, side, tn, tc in scored:
        # Out-of-sample: apply the TRAIN-assigned side to the test slice.
        en, eyes, eno = _eval_setup(s, test)
        ec = eyes if side == "YES" else eno
        f = SetupFinding(
            setup=s, side=side, train_n=tn, train_correct=tc,
            test_n=en, test_correct=ec,
            p_value_train=binom_sf(tc, tn, 0.5), alpha_adjusted=alpha_adj,
        )
        if en < cfg.min_support_test:
            f.notes.append("test support below minimum — cannot validate out-of-sample")
        findings.append(f)

    findings.sort(key=lambda f: (f.test_wilson(cfg.wilson_z), f.train_wilson(cfg.wilson_z)),
                  reverse=True)
    return MineReport(
        checkpoint=cfg.checkpoint, total_rows=total, train_rows=len(train),
        test_rows=len(test), hypotheses_tested=hypotheses, alpha_adjusted=alpha_adj,
        findings=findings,
    )


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def build_report_lines(report: MineReport, cfg: MineConfig, top: int = 5) -> list[str]:
    """Plain-text, honest summary. Leads with whether ANYTHING validated
    out-of-sample, then shows the top candidates with their conservative stats."""
    head = f"{report.checkpoint} SETUP SCAN (read-only, leakage-safe)"
    if report.note:
        return [head, report.note]
    lines = [
        head,
        (f"{report.total_rows} settled · train {report.train_rows} / test {report.test_rows} "
         f"· {report.hypotheses_tested} setups tested · "
         f"Bonferroni α={report.alpha_adjusted:.2g}"),
    ]
    validated = report.validated(cfg)
    perfect = report.perfect_in_train()
    if not validated:
        lines.append("No setup holds up out-of-sample — none is a reliable predictor.")
        if perfect:
            f = max(perfect, key=lambda x: x.train_n)
            lines.append(
                f"Best in-sample only: [{f.setup.label()}] → {f.side} "
                f"{f.train_correct}/{f.train_n} (100% train) but test "
                f"{_pct(f.test_accuracy)} ({f.test_correct}/{f.test_n}); "
                "in-sample perfection did NOT carry over.")
        return lines

    lines.append(f"{len(validated)} setup(s) held up out-of-sample:")
    for f in validated[:top]:
        lines.append(
            f"• [{f.setup.label()}] → predict {f.side}")
        lines.append(
            f"    train {f.train_correct}/{f.train_n} ({_pct(f.train_accuracy)}, "
            f"≥{_pct(f.train_wilson(cfg.wilson_z))} conf) · "
            f"test {f.test_correct}/{f.test_n} ({_pct(f.test_accuracy)}, "
            f"≥{_pct(f.test_wilson(cfg.wilson_z))} conf)")
    lines.append("Note: 'held up' ≠ guaranteed — out-of-sample ≠ the future.")
    return lines
