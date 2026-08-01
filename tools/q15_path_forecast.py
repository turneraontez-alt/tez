"""Train and audit the paper-only 15-minute path forecast baseline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.path_forecast.model import train_and_audit  # noqa: E402
from q15_upgrade.path_forecast.reconstruct import reconstruct_examples  # noqa: E402


def _pct(value: Any) -> str:
    try:
        return f"{100.0 * float(value):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, suffix: str = "") -> str:
    try:
        return f"{float(value):.3f}{suffix}"
    except (TypeError, ValueError):
        return "n/a"


def _markdown(report: Mapping[str, Any], *, generated_at: str, model_path: str) -> str:
    split = report.get("split") or {}
    archetype = report.get("archetype") or {}
    settlement = report.get("settlement") or {}
    trajectory = report.get("trajectory") or {}
    baseline = report.get("baselines") or {}
    decision = report.get("decision") or {}
    fractions = list(trajectory.get("fractions") or [])
    model_mae = list(trajectory.get("model_mae_bps") or [])
    naive_mae = list(trajectory.get("random_walk_mae_bps") or [])
    coverage = list(trajectory.get("interval_80_coverage") or [])
    lines = [
        "# Q15 Path Forecast Audit",
        "",
        f"Generated: `{generated_at}`  ",
        f"Model artifact: `{model_path}`  ",
        f"Model: `{report.get('model_version')}`  ",
        "Mode: **paper-only; notifications and trading disabled**",
        "",
        "## Chronological split",
        "",
        "| Train | Calibration | Test | Test windows |",
        "|---:|---:|---:|---:|",
        (
            f"| {split.get('train_examples', 0)} | {split.get('calibration_examples', 0)} | "
            f"{split.get('test_examples', 0)} | {split.get('test_windows', 0)} |"
        ),
        "",
        "All assets sharing a close time stay in the same fold. Features are point-in-time; "
        "future observations are used only for labels.",
        "",
        "## Out-of-sample performance",
        "",
        "| Target | Model | Baseline |",
        "|---|---:|---:|",
        (
            f"| Path archetype accuracy | {_pct(archetype.get('accuracy'))} | "
            f"{_pct(baseline.get('archetype_majority_accuracy'))} majority |"
        ),
        (
            f"| Settlement accuracy | {_pct(settlement.get('accuracy'))} | "
            f"{_pct(baseline.get('kalshi_market_accuracy'))} Kalshi mid |"
        ),
        (
            f"| Settlement Brier | {_num(settlement.get('binary_brier'))} | "
            f"{_num(baseline.get('kalshi_market_brier'))} Kalshi mid |"
        ),
        "",
        "## Trajectory forecast",
        "",
        "| Future path fraction | Model MAE | Random-walk MAE | 80% band coverage |",
        "|---:|---:|---:|---:|",
    ]
    for idx, fraction in enumerate(fractions):
        lines.append(
            f"| {_pct(fraction)} | {_num(model_mae[idx] if idx < len(model_mae) else None, ' bps')} | "
            f"{_num(naive_mae[idx] if idx < len(naive_mae) else None, ' bps')} | "
            f"{_pct(coverage[idx] if idx < len(coverage) else None)} |"
        )
    lines.extend([
        "",
        "## Activation decision",
        "",
        f"- Forward shadow eligible: **{bool(decision.get('forward_shadow_eligible'))}**",
        f"- Beats random-walk trajectory baseline: **{bool(decision.get('trajectory_improved_vs_random_walk'))}**",
        f"- Beats Kalshi settlement baseline: **{bool(decision.get('settlement_improved_vs_kalshi_market'))}**",
        "- Notification eligible: **False**",
        "- Trading eligible: **False**",
        "",
        "Historical performance is diagnostic only. Promotion requires a separate forward review.",
        "",
    ])
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    examples, coverage = reconstruct_examples(
        path_db=args.path_db,
        metadata_db=args.metadata_db,
    )
    model, report = train_and_audit(examples, coverage=coverage)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / args.model_name
    report_path = output_dir / "audit.json"
    markdown_path = output_dir / "audit.md"
    model.save(str(model_path))
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {**report, "generated_at": generated_at, "model_path": str(model_path.resolve())}
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(
        _markdown(payload, generated_at=generated_at, model_path=str(model_path.resolve())),
        encoding="utf-8",
    )
    print(json.dumps({
        "examples": coverage.get("examples"),
        "unique_windows": coverage.get("unique_windows"),
        "test_examples": report["split"]["test_examples"],
        "archetype_accuracy": report["archetype"].get("accuracy"),
        "settlement_accuracy": report["settlement"].get("accuracy"),
        "forward_shadow_eligible": report["decision"]["forward_shadow_eligible"],
        "model_path": str(model_path.resolve()),
        "report_path": str(report_path.resolve()),
    }, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--path-db", default="data/q15_path_recorder_v1.sqlite3")
    out.add_argument("--metadata-db", default="data/q15_v95_ledger_v1.sqlite3")
    out.add_argument("--output-dir", default="work/path-forecast")
    out.add_argument("--model-name", default="model-v1.npz")
    return out


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
