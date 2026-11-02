"""Golden demo package: hand-derived expectations plus the committed expected-results fixture.

The hand-derived table below is the independent source of truth. The JSON fixture in
samples/expected_results.json must match both this table and the live CLI output, so the
fixture can never silently drift or be regenerated into agreement with a bug.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from closepilot.cli import EXIT_OK, build_report, main
from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.models import RecordResult, Status
from closepilot.validation import summarise, validate_package

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PACKAGE = REPO_ROOT / "data" / "synthetic_close_package.csv"
EXPECTED_RESULTS = REPO_ROOT / "samples" / "expected_results.json"

P, W, R, E = Status.PASS, Status.WARNING, Status.REVIEW_REQUIRED, Status.ERROR

# (record_id, status, variance pct or None/"n/a", controls, scenario)
HAND_DERIVED: list[tuple[str, Status, str | None, list[str], str]] = [
    ("EX-001", P, "0.00", [], "normal record on plan"),
    ("EX-002", P, "2.44", [], "within tolerance; blank commentary allowed below 5%"),
    ("EX-003", R, "13.89", ["C01", "C05"], "golden material variance, commentary missing"),
    ("EX-004", P, "4.99", [], "just below warning threshold"),
    ("EX-005", W, "5.00", ["C02"], "exactly at warning threshold"),
    ("EX-006", W, "7.50", ["C02"], "between warning and review thresholds"),
    ("EX-007", W, "10.00", ["C02"], "exactly at review threshold"),
    ("EX-008", R, "-15.00", ["C01"], "negative variance above review threshold"),
    ("EX-009", R, "0.00", ["C03"], "invalid master-data mapping"),
    ("EX-010", R, "5.00", ["C02", "C05"], "missing commentary at threshold"),
    ("EX-011", R, "0.00", ["C04"], "duplicate record id (first)"),
    ("EX-011", R, "0.00", ["C04"], "duplicate record id (second)"),
    ("EX-012", R, "0.00", ["C06"], "unsupported currency"),
    ("EX-013", R, "0.00", ["C06"], "invalid reporting period"),
    ("EX-014", R, None, ["C01"], "zero forecast, non-zero actual"),
    ("EX-015", P, "0.00", [], "actual and forecast both zero"),
    ("EX-016", R, "-100.00", ["C01"], "zero actual"),
    ("EX-017", P, "-4.00", [], "negative actual and forecast (rebate)"),
    ("EX-018", E, "0.00", ["C07"], "missing required field"),
    ("EX-019", E, "n/a", ["C07"], "malformed numeric value"),
    ("EX-020", P, "0.00", [], "normal"),
    ("EX-021", P, "2.00", [], "normal"),
    ("EX-022", P, "-0.94", [], "normal"),
    ("EX-023", P, "2.33", [], "normal, same cost centre as EX-003"),
    ("EX-024", P, "-2.00", [], "normal"),
]


@pytest.fixture
def results(config: ControlConfig) -> list[RecordResult]:
    return validate_package(load_package(GOLDEN_PACKAGE), config)


def _pct(result: RecordResult) -> str | None:
    if result.variance is None:
        return "n/a"
    return None if result.variance.pct is None else str(result.variance.pct)


def test_golden_package_matches_hand_derived_outcomes(results: list[RecordResult]) -> None:
    actual = [
        (r.record.record_id, r.status, _pct(r), [f.control_id for f in r.findings]) for r in results
    ]
    expected = [(rid, status, pct, controls) for rid, status, pct, controls, _ in HAND_DERIVED]
    assert actual == expected


def test_golden_primary_exception_calculation(results: list[RecordResult]) -> None:
    ex003 = next(r for r in results if r.record.record_id == "EX-003")
    assert ex003.variance is not None
    assert ex003.variance.actual == Decimal("108200.00")
    assert ex003.variance.forecast == Decimal("95000.00")
    assert ex003.variance.amount == Decimal("13200.00")
    assert ex003.variance.pct == Decimal("13.89")
    assert ex003.record.raw["mapping_status"] == "VALID"
    assert ex003.record.raw["commentary"] == ""


def test_golden_unusable_actual_keeps_valid_forecast(results: list[RecordResult]) -> None:
    ex019 = next(r for r in build_report(results)["results"] if r["record_id"] == "EX-019")
    assert ex019["actual"] is None
    assert ex019["forecast"] == "105000.00"
    assert ex019["variance_amount"] is None
    assert ex019["variance_pct"] is None


def test_golden_summary(results: list[RecordResult]) -> None:
    summary = summarise(results)
    assert summary.total_records == 25
    assert summary.status_counts == {P: 10, W: 3, R: 10, E: 2}
    assert summary.findings_by_control == {
        "C01": 4,
        "C02": 4,
        "C03": 1,
        "C04": 2,
        "C05": 2,
        "C06": 2,
        "C07": 2,
    }
    totals = {
        c: (t.records, t.actual, t.forecast, t.variance) for c, t in summary.control_totals.items()
    }
    assert totals == {
        "EUR": (7, Decimal("561700.00"), Decimal("556500.00"), Decimal("5200.00")),
        "GBP": (1, Decimal("20000.00"), Decimal("20000.00"), Decimal("0.00")),
        "PLN": (8, Decimal("310296.00"), Decimal("310000.00"), Decimal("296.00")),
        "USD": (8, Decimal("316300.00"), Decimal("319000.00"), Decimal("-2700.00")),
    }
    assert summary.records_excluded_from_totals == 1


def test_validate_golden_package_text_summary(capsys: pytest.CaptureFixture[str]) -> None:
    config_path = str(REPO_ROOT / "config" / "controls.yaml")
    assert main(["validate", str(GOLDEN_PACKAGE), "--config", config_path]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Total records: 25" in out
    assert "Status: PASS=10, WARNING=3, REVIEW_REQUIRED=10, ERROR=2" in out
    assert "Findings by control: C01=4, C02=4, C03=1, C04=2, C05=2, C06=2, C07=2" in out
    assert "Records excluded from totals: 1" in out
    assert "Record: EX-019  ->  ERROR\n  Actual: unusable  Forecast: 105000.00\n  [C07]" in out


def test_expected_results_fixture_matches_live_output(results: list[RecordResult]) -> None:
    committed: dict[str, Any] = json.loads(EXPECTED_RESULTS.read_text(encoding="utf-8"))
    assert committed == build_report(results)


def test_expected_results_fixture_matches_hand_derived_table() -> None:
    committed: dict[str, Any] = json.loads(EXPECTED_RESULTS.read_text(encoding="utf-8"))
    rows = [
        (
            r["record_id"],
            r["status"],
            "n/a" if r["variance_amount"] is None else r["variance_pct"],
            [f["control_id"] for f in r["findings"]],
        )
        for r in committed["results"]
    ]
    assert rows == [(rid, s.value, pct, c) for rid, s, pct, c, _ in HAND_DERIVED]
