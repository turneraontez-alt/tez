from __future__ import annotations

from types import MethodType


def patch_store(store):
    """Make the existing SignalStore learn only from actual V2 decisions.

    No schema change is required: the patch narrows the old broad queries from
    WATCH/CANDIDATE/CONFIRMED to ENTRY CONFIRMED only and keeps one decision per
    ticker, preventing both YES and NO from entering the same learning corpus.
    """

    def pending_settlements(self, limit=600):
        return self.query(
            "SELECT DISTINCT ON (ticker) id, ticker, side, entry_ask, fee, state "
            "FROM signals WHERE outcome='pending' AND ticker IS NOT NULL "
            "AND side IN ('YES','NO') AND state='ENTRY CONFIRMED' "
            "ORDER BY ticker, ts ASC LIMIT %s",
            (limit,),
        )

    def representative_settled(self, limit=4000):
        return self.query(
            "SELECT DISTINCT ON (ticker) id, asset, ticker, side, signal_type, "
            "state, edge, fair_prob, win_prob, entry_ask, fee, spread, "
            "confirmation_count, required_edge, seconds_remaining, outcome, "
            "realized_return, sent, ts FROM signals "
            "WHERE side IN ('YES','NO') AND outcome IN ('win','loss') "
            "AND ticker IS NOT NULL AND state='ENTRY CONFIRMED' "
            "ORDER BY ticker, ts ASC LIMIT %s",
            (limit,),
        )

    def entry_record(self):
        rows = self.query(
            "SELECT outcome, COUNT(*) AS n FROM signals WHERE sent=TRUE "
            "AND state='ENTRY CONFIRMED' AND outcome IN ('win','loss') "
            "GROUP BY outcome"
        )
        wins = losses = 0
        for row in rows:
            if row.get("outcome") == "win":
                wins = int(row.get("n") or 0)
            elif row.get("outcome") == "loss":
                losses = int(row.get("n") or 0)
        total = wins + losses
        return {"wins": wins, "losses": losses, "total": total, "win_rate": wins / total if total else None}

    store.pending_settlements = MethodType(pending_settlements, store)
    store.representative_settled = MethodType(representative_settled, store)
    store.entry_record = MethodType(entry_record, store)
    return store
