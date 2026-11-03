"""Local development CLI. Switch is the primary interface; this is the fallback."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from closepilot.agent_cli import add_agent_parser
from closepilot.config import ConfigError, load_config
from closepilot.ingestion import IngestionError, load_package
from closepilot.models import RecordResult
from closepilot.reviewer_cli import add_reviewer_parser
from closepilot.validation import PackageSummary, summarise, validate_package

DEFAULT_CONFIG = Path("config/controls.yaml")
EXIT_OK = 0
EXIT_INPUT_ERROR = 2

_THRESHOLD_LABEL = {
    "C01": "review threshold",
    "C02": "warning threshold",
    "C05": "commentary threshold",
}


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _optional_money(value: Decimal | None) -> str | None:
    return None if value is None else _money(value)


def result_to_dict(result: RecordResult) -> dict[str, Any]:
    """Serialise one result. ``actual`` / ``forecast`` are reported individually, so a
    valid value is kept even when the other one is unusable; each is ``null`` only when
    that value itself is unusable (or the whole row is malformed)."""
    v = result.variance
    return {
        "record_id": result.record.record_id,
        "line": result.record.line_number,
        "status": result.status.value,
        "actual": _optional_money(result.record.actual),
        "forecast": _optional_money(result.record.forecast),
        "variance_amount": None if v is None else _money(v.amount),
        "variance_pct": None if v is None else _pct(v.pct),
        "findings": [
            {
                "control_id": f.control_id,
                "status": f.status.value,
                "message": f.message,
                "threshold_pct": _pct(f.threshold_pct),
            }
            for f in result.findings
        ],
    }


def summary_to_dict(summary: PackageSummary) -> dict[str, Any]:
    return {
        "total_records": summary.total_records,
        "status_counts": {s.value: n for s, n in summary.status_counts.items()},
        "findings_by_control": summary.findings_by_control,
        "control_totals": {
            currency: {
                "records": t.records,
                "actual": _money(t.actual),
                "forecast": _money(t.forecast),
                "variance": _money(t.variance),
            }
            for currency, t in summary.control_totals.items()
        },
        "records_excluded_from_totals": summary.records_excluded_from_totals,
    }


def _render_text(results: Sequence[RecordResult]) -> str:
    lines: list[str] = []
    for result in results:
        d = result_to_dict(result)
        lines.append(f"Record: {d['record_id']}  ->  {d['status']}")
        if d["actual"] is not None or d["forecast"] is not None:
            actual = d["actual"] or "unusable"
            forecast = d["forecast"] or "unusable"
            lines.append(f"  Actual: {actual}  Forecast: {forecast}")
        if d["variance_amount"] is not None:
            pct = "undefined" if d["variance_pct"] is None else f"{d['variance_pct']}%"
            lines.append(f"  Variance amount: {d['variance_amount']}  Variance percentage: {pct}")
        # Parse issues are reported as C07 findings; each problem is printed once.
        for f in d["findings"]:
            lines.append(f"  [{f['control_id']}] {f['status']}: {f['message']}")
            label = _THRESHOLD_LABEL.get(f["control_id"])
            if label and f["threshold_pct"] is not None:
                lines.append(f"  Configured {label}: {f['threshold_pct']}%")

    s = summary_to_dict(summarise(results))
    lines.append("")
    lines.append(f"Total records: {s['total_records']}")
    lines.append("Status: " + ", ".join(f"{k}={v}" for k, v in s["status_counts"].items()))
    lines.append(
        "Findings by control: " + ", ".join(f"{k}={v}" for k, v in s["findings_by_control"].items())
    )
    for currency, t in s["control_totals"].items():
        lines.append(
            f"Control total {currency}: records={t['records']} actual={t['actual']} "
            f"forecast={t['forecast']} variance={t['variance']}"
        )
    lines.append(f"Records excluded from totals: {s['records_excluded_from_totals']}")
    return "\n".join(lines)


def build_report(results: Sequence[RecordResult]) -> dict[str, Any]:
    return {
        "summary": summary_to_dict(summarise(results)),
        "results": [result_to_dict(r) for r in results],
    }


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        records = load_package(args.package)
    except (ConfigError, IngestionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    results = validate_package(records, config)
    if args.json:
        print(json.dumps(build_report(results), indent=2))
    else:
        print(_render_text(results))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="closepilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="run deterministic controls on a close package")
    validate.add_argument("package", type=Path, help="path to the close-package CSV")
    validate.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="controls YAML")
    validate.add_argument("--json", action="store_true", help="emit structured JSON")
    validate.set_defaults(handler=_cmd_validate)
    add_agent_parser(sub)
    add_reviewer_parser(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
