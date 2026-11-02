"""Local development CLI. Switch is the primary interface; this is the fallback."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from closepilot.config import ConfigError, load_config
from closepilot.ingestion import IngestionError, load_package
from closepilot.models import RecordResult, Status
from closepilot.validation import validate_package

DEFAULT_CONFIG = Path("config/controls.yaml")
EXIT_OK = 0
EXIT_INPUT_ERROR = 2


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def result_to_dict(result: RecordResult) -> dict[str, Any]:
    v = result.variance
    return {
        "record_id": result.record.record_id,
        "line": result.record.line_number,
        "status": result.status.value,
        "actual": None if v is None else _money(v.actual),
        "forecast": None if v is None else _money(v.forecast),
        "variance_amount": None if v is None else _money(v.amount),
        "variance_pct": None if v is None else _pct(v.pct),
        "parse_issues": list(result.record.parse_issues),
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


def summarise(results: Sequence[RecordResult]) -> dict[str, int]:
    counts = Counter(r.status for r in results)
    summary = {"total_records": len(results)}
    summary.update({status.value: counts.get(status, 0) for status in Status})
    return summary


def _render_text(results: Sequence[RecordResult]) -> str:
    lines: list[str] = []
    for result in results:
        d = result_to_dict(result)
        lines.append(f"Record: {d['record_id']}  ->  {d['status']}")
        if d["actual"] is not None:
            pct = "undefined" if d["variance_pct"] is None else f"{d['variance_pct']}%"
            lines.append(f"  Actual: {d['actual']}  Forecast: {d['forecast']}")
            lines.append(f"  Variance amount: {d['variance_amount']}  Variance percentage: {pct}")
        # Parse issues are already reported as C07 findings; print them only once.
        for f in d["findings"]:
            lines.append(f"  [{f['control_id']}] {f['status']}: {f['message']}")
            if f["threshold_pct"] is not None:
                lines.append(f"  Configured review threshold: {f['threshold_pct']}%")
    summary = summarise(results)
    lines.append("")
    lines.append("Summary: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return "\n".join(lines)


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        records = load_package(args.package)
    except (ConfigError, IngestionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    results = validate_package(records, config)
    if args.json:
        payload = {
            "summary": summarise(results),
            "results": [result_to_dict(r) for r in results],
        }
        print(json.dumps(payload, indent=2))
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    result: int = handler(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
