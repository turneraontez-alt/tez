"""Extraction bridge: production v95 ledger -> challenger training samples.

The production ledger stores ROW-LEVEL predictions (with ``feature_json`` and
``official_result``). This module:

  1. ``export_production_ledger`` — run ON THE REPL against
     ``data/q15_v95_ledger_v1.sqlite3`` to dump resolved rows as JSONL
     (schema-agnostic ``SELECT *`` so it works regardless of column drift).
  2. ``load_jsonl`` / ``inspect_feature_json`` — load the dump and inspect the
     actual ``feature_json`` keys so the feature mapping can be finalised.
  3. ``rows_to_samples`` — convert rows into (timestamps, feature_dicts, y,
     control_probs, groups) that ``harness.train_predictor`` /
     ``walk_forward_evaluate`` consume. Reuses ``features.extract`` so the shadow
     model sees exactly the same decision-time features as live.

Nothing here touches production state; export is read-only.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from . import features as featmod

# Snapshot keys we try to recover from a production row / its feature_json.
_PASSTHROUGH = (
    "spot", "spot_price", "current_price", "target", "target_price", "strike", "threshold",
    "seconds_remaining", "time_remaining", "volatility_per_min", "vol_per_min",
    "yes_bid", "yes_ask", "no_bid", "no_ask", "market_implied_prob", "market_yes_prob",
    "book_imbalance", "orderbook_imbalance", "depth_contracts", "data_quality", "recent_closes",
)
_CHAMP_SIGNALS = ("momentum", "flow", "book", "wick", "context",
                  "threshold_interaction", "exchange_consensus", "derivatives", "absorption")


def export_production_ledger(sqlite_path: str, out_path: str,
                            model_version: str | None = None,
                            resolved_only: bool = True) -> int:
    """Dump resolved predictions to JSONL. Returns row count. READ-ONLY."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    where = []
    args: list[Any] = []
    if resolved_only:
        where.append("official_result IS NOT NULL")
    if model_version:
        where.append("model_version = ?")
        args.append(model_version)
    sql = "SELECT * FROM predictions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC"
    n = 0
    with open(out_path, "w") as fh:
        for row in conn.execute(sql, args):
            d = dict(row)
            fj = d.get("feature_json")
            if isinstance(fj, str):
                try:
                    d["feature_json"] = json.loads(fj)
                except (TypeError, ValueError):
                    d["feature_json"] = None
            fh.write(json.dumps(d, default=str) + "\n")
            n += 1
    conn.close()
    return n


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def inspect_feature_json(rows: Iterable[Mapping]) -> dict[str, int]:
    """Frequency of keys seen inside feature_json — used to finalise mapping."""
    counts: dict[str, int] = {}
    for r in rows:
        fj = r.get("feature_json")
        if isinstance(fj, Mapping):
            for k in fj:
                counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _row_to_snapshot(row: Mapping) -> dict:
    """Reconstruct a decision-time snapshot from a production row + feature_json."""
    snap: dict[str, Any] = {}
    fj = row.get("feature_json")
    fj = fj if isinstance(fj, Mapping) else {}
    qj = row.get("quote_json")
    if isinstance(qj, str):
        try:
            qj = json.loads(qj)
        except (TypeError, ValueError):
            qj = None
    qj = qj if isinstance(qj, Mapping) else {}

    # passthrough scalar context from any location: top-level row column,
    # feature_json, or quote_json (decision-time spot/strike/time/vol live here).
    for k in _PASSTHROUGH:
        if row.get(k) is not None:
            snap[k] = row[k]
        elif fj.get(k) is not None:
            snap[k] = fj[k]
        elif qj.get(k) is not None:
            snap[k] = qj[k]

    # champion signal values: nested under feature_values, or flat in feature_json
    signals = {}
    fv = fj.get("feature_values") if isinstance(fj.get("feature_values"), Mapping) else None
    src = fv or fj
    for s in _CHAMP_SIGNALS:
        v = src.get(s)
        # some schemas store {"value": x, "quality": y}
        if isinstance(v, Mapping):
            v = v.get("value")
        if v is not None:
            signals[s] = v
    if signals:
        snap["feature_values"] = signals
    return snap


def rows_to_samples(rows: Iterable[Mapping], *, checkpoint: str | None = None):
    """Return (timestamps, feature_dicts, y, control_probs, groups).

    Rows lacking an official YES/NO result are skipped. ``control_probs`` is the
    champion's raw probability when available (for paired comparison). ``groups``
    is the contract/close-window id so overlapping checkpoints of one contract
    can be treated as a single independent observation.
    """
    ts: list[float] = []
    feats: list[dict] = []
    y: list[int] = []
    control: list[float | None] = []
    groups: list[str] = []
    for r in rows:
        if checkpoint and str(r.get("checkpoint")) != checkpoint:
            continue
        res = str(r.get("official_result") or "").upper()
        if res not in {"YES", "NO"}:
            continue
        created = r.get("created_at")
        try:
            created = float(created)
        except (TypeError, ValueError):
            continue
        snap = _row_to_snapshot(r)
        fv = featmod.extract(snap)
        ts.append(created)
        feats.append(fv.details)
        y.append(1 if res == "YES" else 0)
        cp = r.get("raw_yes_probability")
        if cp is None:
            cp = r.get("control_probability")
        try:
            control.append(float(cp))
        except (TypeError, ValueError):
            control.append(None)
        # group: contract id with the checkpoint suffix stripped, else ticker
        gid = r.get("ticker") or r.get("contract") or f"row{len(groups)}"
        groups.append(str(gid))
    # ensure chronological order (harness/validation require it)
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    ts = [ts[i] for i in order]
    feats = [feats[i] for i in order]
    y = [y[i] for i in order]
    control = [control[i] for i in order]
    groups = [groups[i] for i in order]
    return ts, feats, y, control, groups
