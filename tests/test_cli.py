from __future__ import annotations

import json
from pathlib import Path

import pytest

from closepilot.cli import EXIT_INPUT_ERROR, EXIT_OK, main

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = str(REPO_ROOT / "config" / "controls.yaml")


def test_validate_json_golden_record(c01_package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(c01_package), "--config", CONFIG, "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_records"] == 8
    assert payload["summary"]["status_counts"] == {
        "PASS": 2,
        "WARNING": 1,
        "REVIEW_REQUIRED": 5,
        "ERROR": 0,
    }
    ex003 = next(r for r in payload["results"] if r["record_id"] == "EX-003")
    assert ex003 == {
        "record_id": "EX-003",
        "line": 4,
        "status": "REVIEW_REQUIRED",
        "actual": "108200.00",
        "forecast": "95000.00",
        "variance_amount": "13200.00",
        "variance_pct": "13.89",
        "findings": [
            {
                "control_id": "C01",
                "status": "REVIEW_REQUIRED",
                "message": "absolute variance 13.89% exceeds review threshold 10.00%",
                "threshold_pct": "10.00",
            },
            {
                "control_id": "C05",
                "status": "REVIEW_REQUIRED",
                "message": (
                    "commentary is required when absolute variance is at least 5.00% "
                    "(or undefined) but is blank"
                ),
                "threshold_pct": "5.00",
            },
        ],
    }
    ex006 = next(r for r in payload["results"] if r["record_id"] == "EX-006")
    assert ex006["variance_pct"] is None


def test_validate_text_shows_calculation(
    c01_package: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(c01_package), "--config", CONFIG]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Record: EX-003  ->  REVIEW_REQUIRED" in out
    assert "Variance amount: 13200.00  Variance percentage: 13.89%" in out
    assert "Configured review threshold: 10.00%" in out
    assert "Configured commentary threshold: 5.00%" in out
    assert "Status: PASS=2, WARNING=1, REVIEW_REQUIRED=5, ERROR=0" in out
    assert "Control total EUR: records=4 actual=428210.00 forecast=395000.00" in out


def test_text_output_reports_malformed_number_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "pkg.csv"
    package.write_text(
        "record_id,submission_id,period,entity,cost_centre,account,actual,forecast,"
        "currency,mapping_status,commentary,submitted_by_role\n"
        "EX-900,FC-2026-10,2026-10,NLG-PL,CC-FLEET,6100-FUEL,12x,100.00,"
        "EUR,VALID,note,Regional Controller\n",
        encoding="utf-8",
    )
    assert main(["validate", str(package), "--config", CONFIG]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Record: EX-900  ->  ERROR" in out
    assert out.count("malformed numeric value '12x'") == 1
    assert "  [C07] ERROR: actual: malformed numeric value '12x'" in out


def test_missing_package_returns_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["validate", str(tmp_path / "absent.csv"), "--config", CONFIG])
    assert code == EXIT_INPUT_ERROR
    assert "error: cannot read package" in capsys.readouterr().err
