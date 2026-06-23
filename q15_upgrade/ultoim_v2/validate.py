"""Ultoim V2 — read-only validation / promotion math (PURE, no I/O).

Answers the only question that gates promotion: *does the entry gate beat the base
rate, with statistical significance, including on the YES side and in a YES-prone
regime?* Operates on a list of settled row dicts (the ledger's ``resolved_rows``),
computes nothing that touches a live decision, and never writes.

Key honesty rules baked in:
  * The benchmark is the EMPIRICAL majority-class accuracy in the sample (``base
    rate``) AND a standing 0.75 prior — both p-values are reported so a regime that
    happens to settle 90% NO cannot masquerade as skill.
  * Significance is a one-sided EXACT binomial test (no normal approximation) plus
    the requirement that the Wilson lower bound clears the base rate.
  * Promotion needs ``n >= min_promote_n`` (default 50) — n=30 prints a number but
    cannot separate ~80-85% from ~75%.
  * Regime is reported TWO ways: the decision-time market-lean proxy
    (``regime_directional``, leakage-safe but unvalidated) and a post-hoc
    REALIZED-window regime (the settled YES-share across all contracts that share a
    ``window_key``) — the latter is only ever used to bucket already-graded rows,
    never fed back into a decision, so it introduces no look-ahead.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

from . import screen

PRIOR_NO_RATE = 0.75  # the standing "always bet NO" base rate for these binaries.

# Promotion-verdict states.
INSUFFICIENT = "INSUFFICIENT"
NOT_SEPARABLE = "NOT_SEPARABLE"
BEATS = "BEATS"

_VERDICT_LABEL = {
    INSUFFICIENT: "INSUFFICIENT DATA",
    NOT_SEPARABLE: "NOT SEPARABLE FROM BASE RATE",
    BEATS: "BEATS BASE RATE",
}


def wilson(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    """Wilson score interval (p, lo, hi). ``(None, None, None)`` when n<=0. The lower
    bound is clamped at 0.0 so a panel never prints a negative percentage."""
    if n <= 0:
        return None, None, None
    p = right / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = centre - half
    hi = centre + half
    return p, max(0.0, lo), min(1.0, hi)


def binom_test_greater(k: int, n: int, p0: float) -> float:
    """One-sided exact binomial tail P(X >= k | n, p0). Returns 1.0 for n<=0 or
    k<=0 (no evidence), 0.0-safe at the edges. Used to test 'hit-rate > base rate'."""
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p0 = min(1.0, max(0.0, float(p0)))
    if p0 <= 0.0:
        return 1.0  # any success is "significant" against a zero-rate null
    if p0 >= 1.0:
        return 1.0 if k <= n else 0.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p0 ** i) * ((1.0 - p0) ** (n - i))
    return min(1.0, max(0.0, total))


# --------------------------------------------------------------------------- #
# row helpers
# --------------------------------------------------------------------------- #
def _is_gated(row: Mapping[str, Any]) -> bool:
    """A row that passed the gate (conf+ask+edge), regardless of side.

    ``gate_b_pass AND gate_c_pass`` is the PRIMARY, authoritative source: those two
    columns are in the ORIGINAL ledger schema (they predate ``research_fired``) so they
    are present on every persisted row, and by construction ``research_fired==1`` is
    exactly ``gate_b_pass==1 AND gate_c_pass==1``. Deriving from them recovers YES
    research rows whose ``research_fired`` was absent and got backfilled to 0 by the
    schema migration — the delivered ``fired`` flag is structurally 0 for every YES row,
    so falling back to it would silently undercount the YES side. The older fallbacks
    remain for rows / test-fixtures that lack the gate columns."""
    gb, gc = row.get("gate_b_pass"), row.get("gate_c_pass")
    if gb is not None and gc is not None:
        return int(gb) == 1 and int(gc) == 1
    rf = row.get("research_fired")
    if rf is not None:
        return int(rf) == 1
    return int(row.get("fired") or 0) == 1


def gated_resolved(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The validation population: gated AND settled."""
    return [dict(r) for r in rows
            if _is_gated(r) and r.get("official_result") is not None]


def _hits(rows: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for r in rows if int(r.get("correct") or 0) == 1)


def _base_rate(rows: Sequence[Mapping[str, Any]]) -> tuple[float | None, str | None]:
    n = len(rows)
    if n == 0:
        return None, None
    n_no = sum(1 for r in rows if str(r.get("official_result") or "").upper() == "NO")
    n_yes = sum(1 for r in rows if str(r.get("official_result") or "").upper() == "YES")
    return max(n_no, n_yes) / n, ("NO" if n_no >= n_yes else "YES")


def lift_vs_base(rows: Sequence[Mapping[str, Any]], *, prior: float = PRIOR_NO_RATE) -> dict[str, Any]:
    """Accuracy + Wilson CI + base-rate lift + one-sided significance vs BOTH the
    empirical base rate and the standing prior."""
    n = len(rows)
    hits = _hits(rows)
    acc = (hits / n) if n else None
    p, lo, hi = wilson(hits, n)
    base, base_side = _base_rate(rows)
    edge = (acc - base) if (acc is not None and base is not None) else None
    p_value = binom_test_greater(hits, n, base) if base is not None else None
    p_value_prior = binom_test_greater(hits, n, prior)
    separable = bool(
        n > 0 and lo is not None and base is not None
        and lo > base and p_value is not None and p_value < 0.05
    )
    return {
        "n": n,
        "hits": hits,
        "misses": n - hits,
        "accuracy": acc,
        "ci_low": lo,
        "ci_high": hi,
        "base_rate": base,
        "base_rate_side": base_side,
        "edge_over_base": edge,
        "p_value": p_value,
        "p_value_vs_prior": p_value_prior,
        "separable": separable,
    }


def roi(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnl = [float(r["hypothetical_pnl_cents"]) for r in rows
           if r.get("hypothetical_pnl_cents") is not None]
    n = len(pnl)
    return {
        "pnl_n": n,
        "pnl_total_cents": round(sum(pnl), 2) if n else 0.0,
        "pnl_avg_cents": round(sum(pnl) / n, 3) if n else None,
    }


def by_side(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for sd in ("NO", "YES"):
        side_rows = [r for r in rows if str(r.get("predicted_side") or "").upper() == sd]
        d = lift_vs_base(side_rows)
        d.update(roi(side_rows))
        out[sd] = d
    return out


def by_interval(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for iv in ("15M", "10M", "7M"):
        iv_rows = [r for r in rows if str(r.get("interval") or "") == iv]
        out[iv] = lift_vs_base(iv_rows)
    return out


def regime_split_directional(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Split by the DECISION-TIME market-lean proxy stored on each row."""
    out: dict[str, dict[str, Any]] = {}
    for rd in ("YES_PRONE", "NO_PRONE", "BALANCED"):
        rd_rows = [r for r in rows if str(r.get("regime_directional") or "") == rd]
        out[rd] = lift_vs_base(rd_rows)
    return out


def realized_window_regime(all_resolved: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """Classify each settlement window by its REALIZED YES-share across all settled
    contracts in that window: YES-share >= 0.6 -> YES_PRONE, <= 0.4 -> NO_PRONE,
    else BALANCED. Post-hoc bucketing label only — never consumed by a decision, so
    no look-ahead. ``all_resolved`` should be the FULL settled population (not just
    gated rows) so the regime reflects the whole window, not the gate's selection."""
    by_window: dict[int, list[str]] = {}
    for r in all_resolved:
        wk = r.get("window_key")
        res = str(r.get("official_result") or "").upper()
        if wk is None or res not in ("YES", "NO"):
            continue
        by_window.setdefault(int(wk), []).append(res)
    out: dict[int, str] = {}
    for wk, results in by_window.items():
        share_yes = sum(1 for x in results if x == "YES") / len(results)
        if share_yes >= 0.6:
            out[wk] = "YES_PRONE"
        elif share_yes <= 0.4:
            out[wk] = "NO_PRONE"
        else:
            out[wk] = "BALANCED"
    return out


def regime_split_realized(gated: Sequence[Mapping[str, Any]],
                          all_resolved: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Split the GATED rows by the realized regime of their window (computed from the
    FULL settled population)."""
    window_regime = realized_window_regime(all_resolved)
    out: dict[str, dict[str, Any]] = {}
    for rd in ("YES_PRONE", "NO_PRONE", "BALANCED"):
        rd_rows = [r for r in gated
                   if r.get("window_key") is not None
                   and window_regime.get(int(r["window_key"])) == rd]
        out[rd] = lift_vs_base(rd_rows)
    return out


_DEFAULT_EDGE_EDGES = (2.0, 3.0, 5.0, 8.0, 12.0)


def edge_bucket_monotonicity(rows: Sequence[Mapping[str, Any]],
                             edges: Sequence[float] = _DEFAULT_EDGE_EDGES) -> dict[str, Any]:
    """Bucket gated rows by net_edge_cents and report per-bucket accuracy. THE key
    diagnostic for 'is edge>=2 a real knob': if accuracy does not rise with net
    edge, the threshold is curve-fit noise, not signal. ``is_monotone`` is True iff
    the populated buckets' accuracies are non-decreasing in edge."""
    bounds = list(edges) + [float("inf")]
    buckets: list[dict[str, Any]] = []
    for i in range(len(bounds) - 1):
        lo_e, hi_e = bounds[i], bounds[i + 1]
        b_rows = [r for r in rows
                  if r.get("net_edge_cents") is not None
                  and lo_e <= float(r["net_edge_cents"]) < hi_e]
        n = len(b_rows)
        hits = _hits(b_rows)
        label = f"[{lo_e:g},{hi_e:g})" if math.isfinite(hi_e) else f"[{lo_e:g},inf)"
        buckets.append({
            "label": label,
            "lo": lo_e,
            "n": n,
            "hits": hits,
            "accuracy": (hits / n) if n else None,
            "pnl_avg_cents": roi(b_rows)["pnl_avg_cents"],
        })
    populated = [b for b in buckets if b["n"] > 0]
    is_monotone = all(
        populated[i]["accuracy"] <= populated[i + 1]["accuracy"] + 1e-12
        for i in range(len(populated) - 1)
    ) if len(populated) >= 2 else None
    return {"buckets": buckets, "is_monotone": is_monotone, "n_populated": len(populated)}


# --------------------------------------------------------------------------- #
# blowup SHADOW screen — read-only diagnostics (NEVER gates a decision)
# --------------------------------------------------------------------------- #
SHADOW_ONLY = "SHADOW_ONLY"

_SHADOW_THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75)


def auc(scored: Sequence[tuple[float, int]]) -> float | None:
    """Rank-based AUC (Mann-Whitney) for a risk score predicting the positive label
    (label 1 = loss). ``scored`` is (score, label) pairs. Handles ties via average
    ranks. None when either class is empty. PURE — no numpy."""
    pos = [s for s, lab in scored if lab == 1]
    neg = [s for s, lab in scored if lab == 0]
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return None
    order = sorted(((s, lab) for s, lab in scored), key=lambda t: t[0])
    ranks = [0.0] * len(order)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1][0] == order[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank over the tie block
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    sum_ranks_pos = sum(ranks[k] for k in range(len(order)) if order[k][1] == 1)
    u_pos = sum_ranks_pos - n_pos * (n_pos + 1) / 2.0
    return u_pos / (n_pos * n_neg)


def _risk_label_pairs(rows: Sequence[Mapping[str, Any]],
                      score_fn) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for r in rows:
        s = score_fn(r)
        if s is None:
            continue
        out.append((float(s), 1 if int(r.get("correct") or 0) == 0 else 0))
    return out


def screen_shadow_report(rows: Sequence[Mapping[str, Any]], *,
                         min_promote_n: int = 50) -> dict[str, Any]:
    """Read-only diagnostics for the record-only blowup SHADOW screen over the gated +
    settled population. Reports blowup_risk's discriminative power (AUC + loss-rate
    terciles) ALONGSIDE the transportable ``distance_sigma`` signal, and a threshold
    sweep showing how many WINNERS each cutoff would touch — the honest test the
    in-sample ``T* = max(risk among winners)`` hides. The verdict is permanently
    ``SHADOW_ONLY``: this function only measures; it never authorises gating.

    A blowup score is derived from each row's stored decision-time fields (so it is
    reproducible and leakage-free), independent of whatever ``screen_version`` was
    stamped when the row was written."""
    gated = gated_resolved(rows)
    blow_pairs = _risk_label_pairs(
        gated, lambda r: screen.blowup_risk(
            r.get("selected_probability"), r.get("conservative_probability"),
            r.get("evidence_quality")))
    dist_pairs = _risk_label_pairs(
        gated, lambda r: screen.distance_risk(r.get("distance_sigma")))
    n_scored = len(blow_pairs)
    n_losses = sum(1 for _, lab in blow_pairs if lab == 1)

    # Loss-rate terciles by blowup_risk — the claimed monotone gradient, measured.
    scored_sorted = sorted(blow_pairs, key=lambda t: t[0])
    buckets: list[dict[str, Any]] = []
    if n_scored >= 3:
        cut = n_scored // 3
        slices = [scored_sorted[:cut], scored_sorted[cut:2 * cut], scored_sorted[2 * cut:]]
        for idx, sl in enumerate(slices):
            losses = sum(1 for _, lab in sl if lab == 1)
            buckets.append({
                "tercile": ("low", "mid", "high")[idx],
                "lo": round(sl[0][0], 4) if sl else None,
                "hi": round(sl[-1][0], 4) if sl else None,
                "n": len(sl),
                "losses": losses,
                "loss_rate": round(losses / len(sl), 4) if sl else None,
            })

    # The circular in-sample threshold and the honest sweep around it.
    winner_risks = [s for s, lab in blow_pairs if lab == 0]
    max_risk_winner = round(max(winner_risks), 4) if winner_risks else None
    sweep: list[dict[str, Any]] = []
    for thr in _SHADOW_THRESHOLDS:
        winners_touched = sum(1 for s, lab in blow_pairs if lab == 0 and s > thr)
        losers_caught = sum(1 for s, lab in blow_pairs if lab == 1 and s > thr)
        sweep.append({
            "threshold": thr,
            "winners_touched": winners_touched,
            "losers_caught": losers_caught,
            "clean": winners_touched == 0 and losers_caught > 0,
        })

    return {
        "available": n_scored > 0,
        "verdict": SHADOW_ONLY,
        "n_gated": len(gated),
        "n_scored": n_scored,
        "n_losses": n_losses,
        "min_promote_n": int(min_promote_n),
        "n_met": n_scored >= int(min_promote_n),
        "blowup_auc": (round(a, 4) if (a := auc(blow_pairs)) is not None else None),
        "distance_auc": (round(d, 4) if (d := auc(dist_pairs)) is not None else None),
        "loss_terciles": buckets,
        "max_risk_winner": max_risk_winner,
        "threshold_sweep": sweep,
        "note": ("record-only; unproven; was inert and below-chance (AUC~0.49) "
                 "out-of-sample — see screen.py. distance_sigma is the transportable "
                 "companion signal. NEVER gates a decision."),
    }


def verdict_from_agg(agg: Mapping[str, Any], *, min_promote_n: int,
                     prior: float = PRIOR_NO_RATE) -> dict[str, Any]:
    """Derive a promotion verdict from an aggregate carrying n/right(or hits)/
    accuracy/ci_low/base_rate. Usable directly off the ledger ``scoreboard`` aggs
    (which expose ``right``) or off ``lift_vs_base`` (which exposes ``hits``)."""
    n = int(agg.get("n") or 0)
    hits = int(agg.get("hits") if agg.get("hits") is not None else (agg.get("right") or 0))
    ci_low = agg.get("ci_low")
    base = agg.get("base_rate")
    if n < int(min_promote_n):
        state = INSUFFICIENT
    else:
        p_value = binom_test_greater(hits, n, base) if base is not None else 1.0
        if (ci_low is not None and base is not None
                and ci_low > base and p_value < 0.05):
            state = BEATS
        else:
            state = NOT_SEPARABLE
    edge = agg.get("edge_over_base")
    return {
        "state": state,
        "label": _VERDICT_LABEL[state],
        "n": n,
        "min_promote_n": int(min_promote_n),
        "edge_over_base": edge,
        "ci_low": ci_low,
        "base_rate": base,
    }


def validation_report(rows: Sequence[Mapping[str, Any]], *, min_promote_n: int = 50,
                      prior: float = PRIOR_NO_RATE) -> dict[str, Any]:
    """Full read-only validation report over a settled-row population. ``rows`` is
    the ledger's ``resolved_rows`` (every settled row, any side/kind)."""
    all_resolved = [dict(r) for r in rows]
    gated = gated_resolved(all_resolved)
    overall = lift_vs_base(gated)
    overall.update(roi(gated))
    verdict = verdict_from_agg(overall, min_promote_n=min_promote_n, prior=prior)

    sides = by_side(gated)
    regimes_realized = regime_split_realized(gated, all_resolved)
    # Promotion needs the YES side AND the YES-prone realized regime to clear the
    # bar too — an overall-only "BEATS" off NO-prone NO rows is exactly the
    # non-separability the handoff warns about.
    yes_proven = sides["YES"]["n"] >= min_promote_n and sides["YES"]["separable"]
    yes_prone_proven = (regimes_realized["YES_PRONE"]["n"] >= min_promote_n
                        and regimes_realized["YES_PRONE"]["separable"])
    fully_proven = bool(verdict["state"] == BEATS and yes_proven and yes_prone_proven)

    return {
        "available": True,
        "n_resolved_total": len(all_resolved),
        "n_gated": len(gated),
        "min_promote_n": int(min_promote_n),
        "prior": prior,
        "overall": overall,
        "verdict": verdict,
        "by_side": sides,
        "by_interval": by_interval(gated),
        "by_regime_directional": regime_split_directional(gated),
        "by_regime_realized": regimes_realized,
        "edge_buckets": edge_bucket_monotonicity(gated),
        "yes_side_proven": yes_proven,
        "yes_prone_regime_proven": yes_prone_proven,
        "fully_proven": fully_proven,
    }
