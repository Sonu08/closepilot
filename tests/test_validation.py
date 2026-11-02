from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.models import REQUIRED_COLUMNS, Finding, Record, RecordResult, Status
from closepilot.validation import (
    check_c01_material_variance,
    compute_variance,
    validate_package,
    validate_record,
)

D = Decimal

_BASE_ROW = {
    "record_id": "EX-900",
    "submission_id": "FC-2026-10",
    "period": "2026-10",
    "entity": "NLG-PL",
    "cost_centre": "CC-FLEET",
    "account": "6100-FUEL",
    "actual": "100.00",
    "forecast": "100.00",
    "currency": "EUR",
    "mapping_status": "VALID",
    "commentary": "note",
    "submitted_by_role": "Regional Controller",
}


@pytest.mark.parametrize(
    ("actual", "forecast", "amount", "pct"),
    [
        ("108200.00", "95000.00", "13200.00", "13.89"),  # golden EX-003
        ("104990.00", "100000.00", "4990.00", "4.99"),
        ("105000.00", "100000.00", "5000.00", "5.00"),
        ("110000.00", "100000.00", "10000.00", "10.00"),
        ("110010.00", "100000.00", "10010.00", "10.01"),
        ("85000.00", "100000.00", "-15000.00", "-15.00"),  # negative variance
        ("0.00", "100000.00", "-100000.00", "-100.00"),  # zero actual
        ("0.00", "0.00", "0.00", "0.00"),  # both zero
        ("-50000.00", "-40000.00", "-10000.00", "-25.00"),  # negative values use |forecast|
        ("110005.00", "100000.00", "10005.00", "10.01"),  # 10.005 rounds half up
        ("110004.99", "100000.00", "10004.99", "10.00"),  # 10.00499 rounds down
    ],
)
def test_compute_variance_exact(actual: str, forecast: str, amount: str, pct: str) -> None:
    v = compute_variance(D(actual), D(forecast))
    assert v.amount == D(amount)
    assert v.pct == D(pct)
    assert str(v.pct) == pct  # always exactly two decimal places


def test_zero_forecast_nonzero_actual_has_undefined_pct() -> None:
    v = compute_variance(D("2500.00"), D("0.00"))
    assert v.amount == D("2500.00")
    assert v.pct is None


@pytest.mark.parametrize(
    ("actual", "forecast", "expected"),
    [
        ("104990.00", "100000.00", None),
        ("105000.00", "100000.00", None),
        ("110000.00", "100000.00", None),  # exactly at threshold: not "greater than"
        ("110010.00", "100000.00", Status.REVIEW_REQUIRED),
        ("89990.00", "100000.00", Status.REVIEW_REQUIRED),  # -10.01% uses absolute value
        ("90000.00", "100000.00", None),  # -10.00%
        ("110004.99", "100000.00", None),  # rounded 10.00 does not trigger
        ("2500.00", "0.00", Status.REVIEW_REQUIRED),  # undefined pct
        ("0.00", "0.00", None),
    ],
)
def test_c01_boundaries(
    config: ControlConfig, actual: str, forecast: str, expected: Status | None
) -> None:
    finding = check_c01_material_variance(compute_variance(D(actual), D(forecast)), config)
    if expected is None:
        assert finding is None
    else:
        assert finding is not None
        assert finding.control_id == "C01"
        assert finding.status is expected
        assert finding.threshold_pct == D("10")


def test_c01_message_states_calculation(config: ControlConfig) -> None:
    finding = check_c01_material_variance(compute_variance(D("108200.00"), D("95000.00")), config)
    assert finding is not None
    assert finding.message == "absolute variance 13.89% exceeds review threshold 10.00%"


def test_record_with_unparsed_amount_without_issue_text_fails_closed(
    config: ControlConfig,
) -> None:
    record = Record(line_number=2, raw={"record_id": "EX-X"}, actual=None, forecast=D("1"))
    result = validate_record(record, config)
    assert result.variance is None
    assert result.findings == (Finding("C07", Status.ERROR, "unusable numeric value"),)
    assert result.status is Status.ERROR


@pytest.mark.parametrize(
    ("actual", "forecast", "messages"),
    [
        ("12x", "100.00", ["actual: malformed numeric value '12x'"]),
        ("100.00", "1,000.00", ["forecast: malformed numeric value '1,000.00'"]),
        ("NaN", "100.00", ["actual: malformed numeric value 'NaN'"]),
        ("=1+1", "100.00", ["actual: malformed numeric value '=1+1'"]),
        ("", "100.00", ["actual: missing value"]),
        ("abc", "", ["actual: malformed numeric value 'abc'", "forecast: missing value"]),
    ],
)
def test_malformed_numbers_return_error_never_pass(
    config: ControlConfig, tmp_path: Path, actual: str, forecast: str, messages: list[str]
) -> None:
    path = tmp_path / "pkg.csv"
    row = dict(_BASE_ROW, actual=actual, forecast=forecast)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerow(row)
    [result] = validate_package(load_package(path), config)
    assert result.status is Status.ERROR
    assert result.variance is None
    assert [(f.control_id, f.status, f.message) for f in result.findings] == [
        ("C07", Status.ERROR, m) for m in messages
    ]


@pytest.mark.parametrize(
    "line",
    [
        # Unquoted thousands separator shifts cells: forecast reads "1", extra cell left over.
        "EX-901,FC-2026-10,2026-10,NLG-PL,CC-FLEET,6100-FUEL,100.00,1,000.00,"
        "EUR,VALID,note,Regional Controller",
        # Short row: trailing fields missing.
        "EX-902,FC-2026-10,2026-10,NLG-PL,CC-FLEET,6100-FUEL,100.00,100.00",
    ],
)
def test_field_count_mismatch_returns_error_never_pass(
    config: ControlConfig, tmp_path: Path, line: str
) -> None:
    path = tmp_path / "pkg.csv"
    path.write_text(",".join(REQUIRED_COLUMNS) + "\n" + line + "\n", encoding="utf-8")
    [result] = validate_package(load_package(path), config)
    assert result.status is Status.ERROR
    assert (
        "C07",
        Status.ERROR,
        "row: field count does not match header (12 columns expected)",
    ) in [(f.control_id, f.status, f.message) for f in result.findings]


def test_severity_precedence_keeps_all_findings() -> None:
    record = Record(line_number=2, raw={"record_id": "EX-X"}, actual=D("1"), forecast=D("1"))
    findings = (
        Finding("C02", Status.WARNING, "w"),
        Finding("C07", Status.ERROR, "e"),
        Finding("C01", Status.REVIEW_REQUIRED, "r"),
    )
    result = RecordResult(record=record, variance=None, findings=findings)
    assert result.status is Status.ERROR
    assert [f.control_id for f in result.findings] == ["C02", "C07", "C01"]


def test_c01_fixture_package_exact_outcomes(config: ControlConfig, c01_package: Path) -> None:
    results = validate_package(load_package(c01_package), config)
    outcome = {
        r.record.record_id: (r.status, r.variance.pct if r.variance else "n/a") for r in results
    }
    assert outcome == {
        "EX-001": (Status.PASS, D("0.00")),
        "EX-002": (Status.PASS, D("10.00")),
        "EX-003": (Status.REVIEW_REQUIRED, D("13.89")),
        "EX-004": (Status.REVIEW_REQUIRED, D("10.01")),
        "EX-005": (Status.REVIEW_REQUIRED, D("-15.00")),
        "EX-006": (Status.REVIEW_REQUIRED, None),
        "EX-007": (Status.PASS, D("0.00")),
        "EX-008": (Status.REVIEW_REQUIRED, D("-25.00")),
    }
