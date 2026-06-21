#!/usr/bin/env python3
"""Audit: does ANY variable / rule / combination predict with a *credible* 100% win rate?

READ-ONLY forensic rule-miner over the production v95 prediction ledger. Run it
ON THE REPL (where ``data/`` holds settled history). It answers the question a
quant — not a salesperson — would ask: not "is there a row set that displays
100%?" but "is any 100% result statistically meaningful, leakage-free, and likely
to survive into future markets?"

Design decisions that keep it honest (all enforced in code + tests):

* **Leakage firewall.** Only fields available AT ``created_at`` may be rule
  variables. Settlement/outcome columns (``official_result``, ``correct``,
  ``realized_cents``, the ``*_brier``/``*_logloss`` columns, ``resolved_at``) and
  post-decision columns written DURING the live window (``changed_before_close``,
  ``learning_applied``) are denylisted and can never enter a rule. The target —
  whether the prediction was correct — is recomputed here from
  ``predicted_side`` vs ``official_result`` and cross-checked against the stored
  ``correct`` flag (integrity guard).

* **No duplicate counting.** The ledger stores ONE row per
  ``(model_version, ticker, checkpoint)``, so a single 15-minute market appears
  up to 3× (15M/10M/7M) and those rows are serially correlated. Every statistic —
  sample tier, Wilson CI, multiple-testing count — is computed on UNIQUE MARKETS
  (distinct ``ticker``), and a market counts as a rule "win" only if EVERY matched
  row of that market was correct (conservative: perfection must hold across the
  market's checkpoints, not just one lucky row).

* **Chronological, not random, splitting.** Markets are ordered by first
  ``created_at`` and cut 60% discovery / 20% validation / 20% final test on MARKET
  boundaries (a market's checkpoints never straddle a split). Thresholds are
  chosen from discovery quantiles only; the test set is untouched until the final
  table. A rule is only "credible perfect" if it stays perfect on the untouched,
  chronologically-later test set with enough unique markets.

* **Multiple-testing honesty.** Every hypothesis tried is counted, and the
  expected number of spurious perfect rules under the null (Σ baseline_acc ** n)
  is reported, so a 3/3 rule is never dressed up as a 150/150 rule.

Nothing here writes production state, touches a live exchange, or sends Telegram.

Usage (on the Repl):
    python3 scripts/perfect_condition_audit.py
    python3 scripts/perfect_condition_audit.py --ledger data/q15_v95_ledger_v1.sqlite3
    python3 scripts/perfect_condition_audit.py --max-vars 2 --min-test-markets 20
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade.challenger.mathx import num, wilson_interval

# --------------------------------------------------------------------------- #
# Leakage firewall: columns that are NOT known at prediction time.            #
# These can never be a rule variable. The audit raises if one slips in.       #
# --------------------------------------------------------------------------- #
OUTCOME_COLUMNS = frozenset({
    "official_result", "correct", "resolved_at", "realized_cents",
    "champion_brier", "challenger_brier", "baseline_brier",
    "champion_logloss", "challenger_logloss", "baseline_logloss",
})
# Written DURING the live window (after created_at) — look-ahead if used.
POST_DECISION_COLUMNS = frozenset({
    "changed_before_close", "learning_applied",
})
# Identifiers / raw timestamps we deliberately exclude as rule variables so the
# miner cannot memorise a single date or a single market.
ID_OR_TIME_COLUMNS = frozenset({
    "prediction_id", "ticker", "model_version", "feature_schema_version",
    "created_at", "close_time", "regime",  # regime kept separately as categorical
    "feature_json", "contribution_json", "quote_json",
    "original_predicted_side",  # vs predicted_side reveals drift = post-decision
})
DENYLIST = OUTCOME_COLUMNS | POST_DECISION_COLUMNS

# Decision-time scalar columns worth mining (numeric).
NUMERIC_COLUMNS = (
    "raw_yes_probability", "calibrated_yes_probability", "challenger_yes_probability",
    "baseline_yes_probability", "selected_probability", "conservative_probability",
    "data_quality", "evidence_quality", "trade_quality", "rank",
    "entry_ask_cents", "entry_cost_cents",
    "flip_risk_score", "flip_risk_confidence", "flip_evidence_count",
)
# Decision-time categorical columns.
CATEGORICAL_COLUMNS = (
    "asset", "checkpoint", "regime", "predicted_side", "trade_decision",
    "confidence_grade", "manipulation_reason",
)
# Champion signal values inside feature_json.
CHAMP_SIGNALS = ("momentum", "flow", "book", "wick", "context",
                 "threshold_interaction", "exchange_consensus", "derivatives", "absorption")
# Decision-time scalars inside quote_json.
QUOTE_NUMERIC = ("spot", "target", "seconds_remaining", "volatility_per_min",
                 "depth_contracts", "bid_cents", "ask_cents", "spread_cents", "book_imbalance")

MAX_CATEGORICAL_LEVELS = 20   # skip high-cardinality cols (anti-memorisation)
DECILES = tuple(i / 10 for i in range(1, 10))  # 0.1 .. 0.9


# --------------------------------------------------------------------------- #
# Row loading & decision-time variable extraction                            #
# --------------------------------------------------------------------------- #
@dataclass
class Obs:
    """One decision-time observation (a prediction) with its resolved outcome."""
    ticker: str
    checkpoint: str
    created_at: float
    correct: int                       # target: predicted_side == official_result
    numeric: dict[str, float]
    cat: dict[str, str]


def _parse_json(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _signal_value(raw: Any) -> Any:
    # champion signals are sometimes {"value": x, "quality": y}
    if isinstance(raw, Mapping):
        return raw.get("value")
    return raw


def load_rows(sqlite_path: str, *, model_version: str | None = None) -> list[dict]:
    """READ-ONLY: load resolved predictions as plain dicts (schema-tolerant)."""
    uri = f"file:{sqlite_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    where = ["official_result IS NOT NULL", "predicted_side IS NOT NULL"]
    args: list[Any] = []
    if model_version:
        where.append("model_version = ?")
        args.append(model_version)
    sql = "SELECT * FROM predictions WHERE " + " AND ".join(where) + " ORDER BY created_at ASC"
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


@dataclass
class IntegrityReport:
    rows: int = 0
    correct_mismatch: int = 0          # stored `correct` disagrees with recomputed
    missing_created_at: int = 0
    bad_result: int = 0
    notes: list[str] = field(default_factory=list)


def extract_observations(rows: Iterable[Mapping]) -> tuple[list[Obs], IntegrityReport]:
    """Turn ledger rows into leakage-safe observations + an integrity report."""
    obs: list[Obs] = []
    rep = IntegrityReport()
    for r in rows:
        rep.rows += 1
        result = str(r.get("official_result") or "").upper()
        side = str(r.get("predicted_side") or "").upper()
        if result not in {"YES", "NO"} or side not in {"YES", "NO"}:
            rep.bad_result += 1
            continue
        created = num(r.get("created_at"))
        if created is None:
            rep.missing_created_at += 1
            continue
        correct = 1 if side == result else 0
        stored = r.get("correct")
        if stored is not None:
            try:
                if int(stored) != correct:
                    rep.correct_mismatch += 1
            except (TypeError, ValueError):
                pass

        numeric: dict[str, float] = {}
        for col in NUMERIC_COLUMNS:
            v = num(r.get(col))
            if v is not None:
                numeric[col] = v
        # manipulation flag as 0/1 numeric (also usable as a gate)
        ms = r.get("manipulation_suspected")
        if ms is not None:
            numeric["manipulation_suspected"] = 1.0 if num(ms) else 0.0

        fj = _parse_json(r.get("feature_json"))
        fv = fj.get("feature_values") if isinstance(fj.get("feature_values"), Mapping) else fj
        for s in CHAMP_SIGNALS:
            v = num(_signal_value(fv.get(s)))
            if v is not None:
                numeric[s] = v

        qj = _parse_json(r.get("quote_json"))
        for k in QUOTE_NUMERIC:
            v = num(qj.get(k))
            if v is not None:
                numeric[k] = v

        # --- derived decision-time variables (computed only from the above) ---
        spot, target = numeric.get("spot"), numeric.get("target")
        if spot is not None and target not in (None, 0):
            numeric["pct_distance"] = (spot - target) / target * 100.0
            numeric["abs_pct_distance"] = abs(spot - target) / target * 100.0
        bid, ask = numeric.get("bid_cents"), numeric.get("ask_cents")
        if bid is not None and ask is not None:
            numeric["market_implied_prob"] = (bid + ask) / 200.0
        sr = numeric.get("seconds_remaining")
        if sr is not None:
            numeric["minutes_remaining"] = sr / 60.0
        # cross-feed agreement, if Coinbase/OKX prices are logged in quote_json
        feeds = {kk.lower(): num(vv) for kk, vv in qj.items() if isinstance(kk, str)}
        cb = next((feeds[k] for k in feeds if "coinbase" in k and feeds[k] is not None), None)
        okx = next((feeds[k] for k in feeds if "okx" in k and feeds[k] is not None), None)
        if cb is not None and okx is not None and cb != 0:
            numeric["feed_divergence_bps"] = (cb - okx) / cb * 1e4

        cat: dict[str, str] = {}
        for col in CATEGORICAL_COLUMNS:
            v = r.get(col)
            if v not in (None, ""):
                cat[col] = str(v)
        # derived direction from spot vs target (decision-time)
        if spot is not None and target is not None:
            cat["direction"] = "UP" if spot > target else "DOWN" if spot < target else "FLAT"

        obs.append(Obs(ticker=str(r.get("ticker") or f"row{rep.rows}"),
                       checkpoint=str(r.get("checkpoint") or "?"),
                       created_at=float(created), correct=correct,
                       numeric=numeric, cat=cat))

    # Leakage firewall: assert no outcome/post-decision column leaked into vars.
    leaked = set()
    for o in obs:
        leaked |= (set(o.numeric) | set(o.cat)) & DENYLIST
    if leaked:
        raise AssertionError(f"LEAKAGE: outcome/post-decision columns used as variables: {sorted(leaked)}")
    if rep.correct_mismatch:
        rep.notes.append(f"{rep.correct_mismatch} rows: stored `correct` disagreed with "
                         f"recomputed predicted_side==official_result (using recomputed).")
    return obs, rep


# --------------------------------------------------------------------------- #
# Chronological split on MARKET boundaries                                    #
# --------------------------------------------------------------------------- #
def chronological_split(obs: Sequence[Obs], frac=(0.6, 0.2, 0.2)) -> dict[str, list[Obs]]:
    """Split into discovery/validation/test by unique-market first-seen time.

    A market (ticker) and all of its checkpoint rows stay together in one split.
    """
    first_seen: dict[str, float] = {}
    for o in obs:
        first_seen[o.ticker] = min(first_seen.get(o.ticker, o.created_at), o.created_at)
    markets = sorted(first_seen, key=lambda t: (first_seen[t], t))
    n = len(markets)
    i1 = int(round(n * frac[0]))
    i2 = int(round(n * (frac[0] + frac[1])))
    assign = {}
    for idx, t in enumerate(markets):
        assign[t] = "discovery" if idx < i1 else "validation" if idx < i2 else "test"
    out: dict[str, list[Obs]] = {"discovery": [], "validation": [], "test": []}
    for o in obs:
        out[assign[o.ticker]].append(o)
    return out


# --------------------------------------------------------------------------- #
# Candidate atomic conditions and rules                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Atom:
    var: str
    op: str          # ">=", "<=", "=="
    value: Any

    def test(self, o: Obs) -> bool | None:
        if self.op == "==":
            v = o.cat.get(self.var)
            return None if v is None else (v == self.value)
        v = o.numeric.get(self.var)
        if v is None:
            return None
        return v >= self.value if self.op == ">=" else v <= self.value

    def human(self) -> str:
        if self.op == "==":
            return f"{self.var} == {self.value}"
        return f"{self.var} {self.op} {self.value:.6g}"


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def candidate_atoms(discovery: Sequence[Obs]) -> list[Atom]:
    """Atomic conditions, with thresholds taken from DISCOVERY quantiles only."""
    atoms: list[Atom] = []
    # numeric thresholds from discovery deciles
    num_names: set[str] = set()
    for o in discovery:
        num_names |= set(o.numeric)
    for name in sorted(num_names):
        vals = sorted(o.numeric[name] for o in discovery if name in o.numeric)
        if len(vals) < 2 or vals[0] == vals[-1]:
            continue
        cuts = sorted({round(_quantile(vals, q), 10) for q in DECILES})
        for c in cuts:
            atoms.append(Atom(name, ">=", c))
            atoms.append(Atom(name, "<=", c))
    # categorical equality (skip high-cardinality columns)
    cat_levels: dict[str, set[str]] = {}
    for o in discovery:
        for k, v in o.cat.items():
            cat_levels.setdefault(k, set()).add(v)
    for name in sorted(cat_levels):
        levels = cat_levels[name]
        if len(levels) > MAX_CATEGORICAL_LEVELS:
            continue
        for lvl in sorted(levels):
            atoms.append(Atom(name, "==", lvl))
    return atoms


# --------------------------------------------------------------------------- #
# Rule evaluation — market-level (no duplicate counting)                       #
# --------------------------------------------------------------------------- #
@dataclass
class SplitEval:
    rows: int = 0
    markets: int = 0
    market_wins: int = 0       # markets where EVERY matched row was correct
    market_losses: int = 0
    row_wins: int = 0
    row_total: int = 0

    @property
    def win_rate(self) -> float | None:
        return self.market_wins / self.markets if self.markets else None

    @property
    def row_win_rate(self) -> float | None:
        return self.row_wins / self.row_total if self.row_total else None

    @property
    def perfect(self) -> bool:
        return self.markets > 0 and self.market_losses == 0

    def wilson(self, z=1.96) -> tuple[float, float]:
        return wilson_interval(self.market_wins, self.markets, z)


def evaluate(rule: Sequence[Atom], obs: Sequence[Obs]) -> SplitEval:
    """Match a rule (AND of atoms) and score it at the MARKET level.

    A row matches only if every atom is *defined and true* for it (a missing
    variable is a non-match, never a silent pass)."""
    ev = SplitEval()
    per_market: dict[str, list[int]] = {}
    for o in obs:
        ok = True
        for a in rule:
            t = a.test(o)
            if t is not True:
                ok = False
                break
        if not ok:
            continue
        ev.rows += 1
        ev.row_total += 1
        ev.row_wins += o.correct
        per_market.setdefault(o.ticker, []).append(o.correct)
    ev.markets = len(per_market)
    for corrects in per_market.values():
        if all(c == 1 for c in corrects):
            ev.market_wins += 1
        else:
            ev.market_losses += 1
    return ev


def baseline_accuracy(obs: Sequence[Obs]) -> float:
    """Market-level base rate: fraction of markets correct at every matched row."""
    per_market: dict[str, list[int]] = {}
    for o in obs:
        per_market.setdefault(o.ticker, []).append(o.correct)
    if not per_market:
        return 0.0
    wins = sum(1 for cs in per_market.values() if all(c == 1 for c in cs))
    return wins / len(per_market)


# --------------------------------------------------------------------------- #
# Sample tiers & classification                                               #
# --------------------------------------------------------------------------- #
def sample_tier(unique_markets: int) -> str:
    if unique_markets <= 4:
        return "anecdotal"
    if unique_markets <= 19:
        return "insufficient evidence"
    if unique_markets <= 49:
        return "exploratory"
    if unique_markets <= 99:
        return "potentially meaningful"
    return "stronger evidence"


@dataclass
class RuleResult:
    atoms: tuple[Atom, ...]
    discovery: SplitEval
    validation: SplitEval
    test: SplitEval
    pooled: SplitEval                      # all splits, for headline counts
    base_acc_test: float

    @property
    def human(self) -> str:
        return " AND ".join(a.human() for a in self.atoms)

    @property
    def n_vars(self) -> int:
        return len(self.atoms)

    @property
    def assets(self) -> str:
        return ""  # filled by audit (needs obs); placeholder kept simple

    def classify(self, min_test_markets: int) -> str:
        """Reliability verdict combining perfection, OOS survival and sample size."""
        d, v, t = self.discovery, self.validation, self.test
        if not d.perfect:
            return "rejected: not perfect on discovery"
        if v.markets and not v.perfect:
            return "rejected: failed validation"
        if t.markets == 0:
            return "unverified: no test-set markets"
        if not t.perfect:
            return "rejected: failed final test set"
        if t.markets < min_test_markets:
            return f"insufficient: perfect OOS but only {t.markets} test markets"
        return "CREDIBLE perfect (survived untouched test set)"


# --------------------------------------------------------------------------- #
# The audit                                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class AuditConfig:
    max_vars: int = 3
    min_discovery_markets: int = 5     # support floor for an atom/rule on discovery
    min_test_markets: int = 20         # markets required in test to call it credible
    max_candidates: int = 200_000      # combinatorial safety cap
    near_perfect_floor: float = 0.95   # report near-misses at/above this win rate


def audit(sqlite_path: str, *, model_version: str | None = None,
          config: AuditConfig | None = None) -> dict[str, Any]:
    cfg = config or AuditConfig()
    rows = load_rows(sqlite_path, model_version=model_version)
    obs, integrity = extract_observations(rows)
    integrity_d = {"rows": integrity.rows, "correct_mismatch": integrity.correct_mismatch,
                   "missing_created_at": integrity.missing_created_at,
                   "bad_result": integrity.bad_result, "notes": integrity.notes}

    report: dict[str, Any] = {
        "sqlite": sqlite_path, "n_observations": len(obs),
        "n_unique_markets": len({o.ticker for o in obs}),
        "integrity": integrity_d,
        "hypotheses_tested": 0, "candidates_truncated": False,
        "perfect": [], "near_perfect": [], "rejected_examples": [],
        "expected_false_perfects": 0.0, "credible_perfect_count": 0,
    }
    if len(obs) < 10 or report["n_unique_markets"] < 6:
        report["note"] = ("not enough settled history to audit "
                          f"({report['n_unique_markets']} unique markets). "
                          "Let the monitor accumulate more resolved markets.")
        report["executive"] = (
            "No perfect condition found — insufficient settled history to evaluate "
            f"({report['n_unique_markets']} unique markets; need a chronological "
            "discovery/validation/test split). Accumulate more resolved markets.")
        return report

    splits = chronological_split(obs)
    disc, val, test = splits["discovery"], splits["validation"], splits["test"]
    report["split_markets"] = {k: len({o.ticker for o in v}) for k, v in splits.items()}
    report["split_base_accuracy"] = {k: round(baseline_accuracy(v), 4) for k, v in splits.items()}
    base_acc_test = baseline_accuracy(test)
    base_acc_pooled = baseline_accuracy(obs)

    # 1) atomic conditions with thresholds from DISCOVERY only.
    atoms = candidate_atoms(disc)
    # prune atoms that lack support on discovery (anti-overfit / anti-memorise).
    kept_atoms: list[Atom] = []
    for a in atoms:
        ev = evaluate((a,), disc)
        if ev.markets >= cfg.min_discovery_markets:
            kept_atoms.append(a)
    kept_atoms.sort(key=lambda a: (a.var, str(a.op), str(a.value)))

    # 2) build rules of size 1..max_vars by apriori-style support pruning.
    results: list[RuleResult] = []
    tested = 0
    truncated = False
    frontier: list[tuple[Atom, ...]] = [(a,) for a in kept_atoms]

    def _assets_for(atoms_t: tuple[Atom, ...]) -> list[str]:
        ev_obs = [o for o in obs if all(at.test(o) is True for at in atoms_t)]
        return sorted({o.cat.get("asset", "?") for o in ev_obs})

    for size in range(1, cfg.max_vars + 1):
        next_frontier: list[tuple[Atom, ...]] = []
        for combo in frontier:
            if tested >= cfg.max_candidates:
                truncated = True
                break
            tested += 1
            d_ev = evaluate(combo, disc)
            if d_ev.markets < cfg.min_discovery_markets:
                continue  # prune: insufficient support, and supersets only shrink
            v_ev = evaluate(combo, val)
            t_ev = evaluate(combo, test)
            p_ev = evaluate(combo, obs)
            rr = RuleResult(atoms=combo, discovery=d_ev, validation=v_ev,
                            test=t_ev, pooled=p_ev, base_acc_test=base_acc_test)
            results.append(rr)
            # extend only perfect-or-near discovery rules (keeps search focused
            # and honest: we explicitly look for HIGH win-rate conditions).
            if size < cfg.max_vars and d_ev.win_rate is not None \
                    and d_ev.win_rate >= cfg.near_perfect_floor:
                next_frontier.append(combo)
        if truncated:
            break
        # grow: append a higher-ordered atom to each retained combo.
        grown: list[tuple[Atom, ...]] = []
        for combo in next_frontier:
            last = combo[-1]
            for a in kept_atoms:
                if (a.var, str(a.op), str(a.value)) <= (last.var, str(last.op), str(last.value)):
                    continue
                if any(a.var == ex.var for ex in combo):
                    continue  # don't AND two conditions on the same variable
                grown.append(combo + (a,))
        frontier = grown
        if not frontier:
            break

    report["hypotheses_tested"] = tested
    report["candidates_truncated"] = truncated

    # 3) expected number of spurious perfect rules under the null.
    #    For each tested rule, P(perfect by luck) ~= base_acc ** (test markets).
    exp_fp = 0.0
    for rr in results:
        n = rr.test.markets
        if n > 0:
            exp_fp += base_acc_pooled ** n
    report["expected_false_perfects"] = round(exp_fp, 4)
    report["pooled_base_accuracy"] = round(base_acc_pooled, 4)

    # 4) collect perfect + near-perfect, deduped to the SIMPLEST rule.
    def _record(rr: RuleResult) -> dict[str, Any]:
        lo, hi = rr.test.wilson() if rr.test.markets else rr.pooled.wilson()
        return {
            "rule": rr.human, "n_vars": rr.n_vars,
            "assets": _assets_for(rr.atoms),
            "unique_markets_total": rr.pooled.markets,
            "rows_total": rr.pooled.rows,
            "discovery": {"markets": rr.discovery.markets, "win_rate": rr.discovery.win_rate},
            "validation": {"markets": rr.validation.markets, "win_rate": rr.validation.win_rate},
            "test": {"markets": rr.test.markets, "win_rate": rr.test.win_rate,
                     "wilson_low": round(lo, 4), "wilson_high": round(hi, 4)},
            "tier": sample_tier(rr.test.markets or rr.pooled.markets),
            "classification": rr.classify(cfg.min_test_markets),
            "test_base_accuracy": round(rr.base_acc_test, 4),
            "p_perfect_by_chance": round(base_acc_pooled ** (rr.test.markets or rr.pooled.markets), 6),
        }

    # 4) "Historical 100%" means perfect on the DISCOVERY set — that is what a
    #    naive miner would flag. Each is then annotated with its classification,
    #    which reveals whether it actually survived validation + the untouched
    #    test set. Dedup to the simplest rule per (variable, operator) signature.
    discovery_perfect = [rr for rr in results if rr.discovery.perfect]
    discovery_perfect.sort(key=lambda rr: (rr.n_vars, -(rr.test.markets), -rr.pooled.markets))
    seen_signatures: set[frozenset] = set()
    deduped: list[RuleResult] = []
    for rr in discovery_perfect:
        sig = frozenset((a.var, a.op) for a in rr.atoms)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        deduped.append(rr)
    report["perfect"] = [_record(rr) for rr in deduped]

    # Near-perfect: not perfect, but a stable high win rate across many markets is
    # often more useful than a 100% rule on six markets.
    near = [rr for rr in results
            if not rr.pooled.perfect and rr.pooled.win_rate is not None
            and rr.pooled.win_rate >= cfg.near_perfect_floor
            and rr.pooled.markets >= cfg.min_test_markets]
    near.sort(key=lambda rr: (-(rr.pooled.markets), -(rr.pooled.win_rate or 0)))
    report["near_perfect"] = [_record(rr) for rr in near[:25]]

    # Rejected: discovery-perfect rules that did NOT earn a credible verdict.
    rejected = [rr for rr in deduped
                if not rr.classify(cfg.min_test_markets).startswith("CREDIBLE")]
    report["rejected_examples"] = [
        {"rule": rr.human, "reason": rr.classify(cfg.min_test_markets),
         "test_markets": rr.test.markets, "total_markets": rr.pooled.markets}
        for rr in rejected[:25]
    ]

    # 5) executive classification, driven by the discovery-perfect candidates and
    #    their out-of-sample fate.
    credible = [rr for rr in deduped
                if rr.classify(cfg.min_test_markets).startswith("CREDIBLE")]
    report["credible_perfect_count"] = len(credible)
    reached_test = [rr for rr in deduped if rr.test.markets > 0]
    if integrity.correct_mismatch:
        report["executive"] = ("Historical 100% condition(s) present, but a data-integrity issue "
                               "was detected (stored `correct` disagreed with the recomputed "
                               "predicted_side==official_result outcome). Resolve before trusting any rule.")
    elif credible:
        report["executive"] = ("Strong out-of-sample 100% condition found, but future certainty is "
                               "NOT established (a finite perfect sample cannot prove a 100% true rate).")
    elif not deduped:
        report["executive"] = "No perfect condition found under strict, leakage-free, out-of-sample validation."
    elif any(not rr.test.perfect and rr.test.markets > 0 for rr in deduped) or \
            any(rr.validation.markets and not rr.validation.perfect for rr in deduped):
        report["executive"] = ("Historical 100% condition found, but it failed out-of-sample "
                               "validation (perfect in-sample, not on the untouched later data).")
    else:
        report["executive"] = ("Historical 100% condition found, but the sample is insufficient "
                               "(could not be confirmed on an adequately large untouched test set).")
    return report


# --------------------------------------------------------------------------- #
# Pretty printing                                                             #
# --------------------------------------------------------------------------- #
def _wr(x: float | None) -> str:
    return "n/a" if x is None else f"{x*100:.1f}%"


def print_report(rep: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"PERFECT-CONDITION AUDIT — {rep['sqlite']}")
    print(f"observations (predictions): {rep['n_observations']}   "
          f"unique markets: {rep['n_unique_markets']}")
    ig = rep["integrity"]
    print(f"integrity: rows={ig['rows']} bad_result={ig['bad_result']} "
          f"missing_created_at={ig['missing_created_at']} correct_mismatch={ig['correct_mismatch']}")
    for n in ig["notes"]:
        print(f"   ! {n}")
    if rep.get("note"):
        print(f"\n{rep['note']}")
        print("\nEXECUTIVE CONCLUSION:\n  " + rep.get("executive", rep["note"]))
        return
    print(f"split markets (chrono 60/20/20): {rep['split_markets']}")
    print(f"base accuracy by split: {rep['split_base_accuracy']}")
    print(f"hypotheses tested: {rep['hypotheses_tested']}"
          + ("  (TRUNCATED at cap)" if rep["candidates_truncated"] else ""))
    print(f"pooled base accuracy: {rep['pooled_base_accuracy']}   "
          f"expected spurious perfect rules under null: {rep['expected_false_perfects']}")

    print("\n" + "-" * 78)
    print("PERFECT HISTORICAL CONDITIONS (100% on discovery; classification = OOS fate)")
    if not rep["perfect"]:
        print("  none — no rule was perfect across its matched unique discovery markets.")
    for i, r in enumerate(rep["perfect"][:25], 1):
        d, v, t = r["discovery"], r["validation"], r["test"]
        print(f"\n  [{i}] {r['rule']}")
        print(f"      vars={r['n_vars']}  assets={r['assets']}  total markets={r['unique_markets_total']} "
              f"(rows={r['rows_total']})  tier={r['tier']}")
        print(f"      discovery {_wr(d['win_rate'])} ({d['markets']}m) | "
              f"validation {_wr(v['win_rate'])} ({v['markets']}m) | "
              f"TEST {_wr(t['win_rate'])} ({t['markets']}m, Wilson "
              f"[{t['wilson_low']:.2f},{t['wilson_high']:.2f}])")
        print(f"      P(perfect by chance)={r['p_perfect_by_chance']}  →  {r['classification']}")

    print("\n" + "-" * 78)
    print(f"NEAR-PERFECT CONDITIONS (>= {rep.get('pooled_base_accuracy','?')} base; "
          ">=95% win, enough markets)")
    if not rep["near_perfect"]:
        print("  none above threshold with sufficient markets.")
    for i, r in enumerate(rep["near_perfect"][:15], 1):
        print(f"  [{i}] {r['rule']}  →  pooled {_wr(r['test']['win_rate'])} "
              f"test / {r['unique_markets_total']} total markets  tier={r['tier']}")

    if rep["rejected_examples"]:
        print("\n" + "-" * 78)
        print("REJECTED PERFECT-LOOKING RULES (why they don't count)")
        for r in rep["rejected_examples"][:15]:
            print(f"  - {r['rule']}  →  {r['reason']} "
                  f"(test {r['test_markets']}m / total {r['total_markets']}m)")

    print("\n" + "=" * 78)
    print("EXECUTIVE CONCLUSION:\n  " + rep["executive"])
    print("\nReminder: a 100% historical win rate does NOT prove a 100% future\n"
          "probability. With n perfect markets the true win rate's lower bound is\n"
          "only ~1 - 3/n (rule of three); certainty is never established.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default="data/q15_v95_ledger_v1.sqlite3")
    ap.add_argument("--model-version", default=None)
    ap.add_argument("--max-vars", type=int, default=3,
                    help="max conditions ANDed per rule (default 3)")
    ap.add_argument("--min-discovery-markets", type=int, default=5)
    ap.add_argument("--min-test-markets", type=int, default=20,
                    help="unique test-set markets required to call a perfect rule credible")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    if not os.path.exists(args.ledger):
        print(f"ledger not found: {args.ledger}\nRun this on the Repl where data/ lives.",
              file=sys.stderr)
        return 2
    cfg = AuditConfig(max_vars=args.max_vars,
                      min_discovery_markets=args.min_discovery_markets,
                      min_test_markets=args.min_test_markets)
    rep = audit(args.ledger, model_version=args.model_version, config=cfg)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
