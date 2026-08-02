"""Manual authoritative-Kalshi command for V21's frozen pretest labels."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.kalshi_rest import BASE_URL, KalshiClient
from q15_upgrade.strategy_bots import costs
from tools import q15_rti_v15_label_evidence as label_evidence
from tools import q15_rti_v15_pretest_command as authoritative
from tools import q15_rti_v21_feature_seal as feature_seal
from tools import q15_rti_v21_modeling as modeling
from tools import q15_rti_v21_pretest_runner as runner


DEFAULT_STATE_DIR = ROOT / "reports" / "q15_rti_v21_audit"
DEFAULT_RESERVATION = DEFAULT_STATE_DIR / "pretest-reservation.json"


def load_feature_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_pretest_command_feature_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_pretest_command_feature_seal_root_not_object")
    payload = dict(value)
    feature_seal.validate_seal(payload)
    return payload


def expected_database_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    feature_seal.validate_seal(seal)
    return [
        {
            "id": int(row["parent_id"]),
            "ticker": str(row["ticker"]),
            "asset": str(row["asset"]),
            "close_time": float(row["close_time"]),
        }
        for row in seal["rows"]
    ]


class KalshiFeeVerifiedSQLiteLabelReader:
    """Verify fresh official settlements and seven series fee identities."""

    def __init__(
        self, database_path: Path, *, expected_rows: Sequence[Mapping[str, Any]],
        get_market: Callable[[str], Mapping[str, Any] | None] | None = None,
        get_series: Callable[[str], Mapping[str, Any] | None] | None = None,
        source_base_url: str = BASE_URL,
    ) -> None:
        client = KalshiClient()
        self.get_series = get_series if get_series is not None else client.get_series
        self.source_base_url = str(source_base_url).rstrip("/")
        self._fee_verification: dict[str, Any] | None = None
        self.reader = authoritative.KalshiVerifiedSQLiteLabelReader(
            database_path,
            expected_rows=expected_rows,
            get_market=get_market if get_market is not None else client.get_market,
            source_base_url=self.source_base_url,
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _verify_series_fees(self) -> dict[str, Any]:
        config = modeling.load_contract()["fee_schedule_verification"]
        if (
            costs.KALSHI_Q15_FEE_TYPE != config["required_fee_type"]
            or float(costs.KALSHI_Q15_FEE_MULTIPLIER)
            != float(config["required_fee_multiplier"])
            or float(costs.KALSHI_GENERAL_TAKER_FEE_RATE)
            != float(config["general_taker_fee_rate"])
            or costs.KALSHI_Q15_FEE_SCHEDULE_VERSION
            != config["fee_schedule_version"]
            or costs.RTI_EXECUTION_COST_MODEL_VERSION
            != config["execution_cost_model_version"]
        ):
            raise ValueError("v21_pretest_command_local_fee_contract_mismatch")
        records = []
        for ticker in sorted(str(value) for value in config["series_tickers"]):
            fetched_at = self._now_iso()
            try:
                series = self.get_series(ticker)
            except Exception as exc:
                raise ValueError(
                    "v21_pretest_command_kalshi_series_unavailable"
                ) from exc
            if not isinstance(series, Mapping):
                raise ValueError("v21_pretest_command_kalshi_series_unavailable")
            returned = str(series.get("ticker") or "")
            fee_type = str(series.get("fee_type") or "")
            try:
                multiplier = float(series["fee_multiplier"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "v21_pretest_command_kalshi_series_fee_mismatch"
                ) from exc
            if (
                returned != ticker
                or fee_type != config["required_fee_type"]
                or multiplier != float(config["required_fee_multiplier"])
            ):
                raise ValueError(
                    "v21_pretest_command_kalshi_series_fee_mismatch"
                )
            records.append({
                "series_ticker": ticker,
                "fee_type": fee_type,
                "fee_multiplier": multiplier,
                "series_last_updated_ts": series.get("last_updated_ts"),
                "fetched_at": fetched_at,
                "source_url": f"{self.source_base_url}/series/{ticker}",
            })
        return {
            "verification_status": "OFFICIAL_KALSHI_SERIES_FEE_METADATA_VERIFIED",
            "verified_at": self._now_iso(),
            "fee_schedule_version": config["fee_schedule_version"],
            "execution_cost_model_version": config[
                "execution_cost_model_version"
            ],
            "general_taker_fee_rate": float(config["general_taker_fee_rate"]),
            "series": records,
        }

    def verify_fee_precondition(self) -> dict[str, Any]:
        """Cache fresh outcome-free fee evidence before label reservation."""
        self._fee_verification = self._verify_series_fees()
        return dict(self._fee_verification)

    def __call__(
        self, row_ids: Sequence[int],
    ) -> label_evidence.VerifiedLabelMapping:
        if self._fee_verification is None:
            raise ValueError(
                "v21_pretest_command_fee_precondition_not_verified"
            )
        labels = self.reader(row_ids)
        raw_evidence = getattr(labels, "audit_evidence", None)
        if not isinstance(raw_evidence, Mapping):
            raise ValueError("v21_pretest_command_settlement_evidence_missing")
        evidence = dict(raw_evidence)
        evidence["fee_schedule_verification"] = dict(self._fee_verification)
        evidence = label_evidence.seal_evidence(evidence)
        return label_evidence.VerifiedLabelMapping(labels, evidence)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(feature_seal.DEFAULT_OUTPUT))
    parser.add_argument("--strategy-db", default=str(feature_seal.DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    seal = load_feature_seal(Path(args.seal))
    label_reader = KalshiFeeVerifiedSQLiteLabelReader(
        Path(args.strategy_db), expected_rows=expected_database_rows(seal),
    )
    # Series metadata is outcome-free.  Verify it before the one-shot label
    # reservation so a transient fee endpoint failure cannot burn the audit.
    label_reader.verify_fee_precondition()
    result = runner.run_pretest_once(
        seal=seal,
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_settlement_yes_labels=label_reader,
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
