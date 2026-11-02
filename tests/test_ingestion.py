from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from closepilot import ingestion
from closepilot.ingestion import IngestionError, load_package, parse_amount
from closepilot.models import REQUIRED_COLUMNS

HEADER = ",".join(REQUIRED_COLUMNS)


def _row(actual: str, forecast: str) -> str:
    return (
        f"EX-900,FC-2026-10,2026-10,NLG-PL,CC-FLEET,6100-FUEL,{actual},{forecast},"
        "EUR,VALID,note,Regional Controller"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100", Decimal("100")),
        ("108200.00", Decimal("108200.00")),
        ("-50000.00", Decimal("-50000.00")),
        ("  12.50 ", Decimal("12.50")),
        ("0", Decimal("0")),
    ],
)
def test_parse_amount_accepts_plain_decimals(text: str, expected: Decimal) -> None:
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", "abc", "1,000.00", "1e5", "NaN", "Infinity", "€100", "=1+1", "12.", ".5"]
)
def test_parse_amount_rejects_malformed(text: str) -> None:
    assert parse_amount(text) is None


def test_load_c01_fixture(c01_package: Path) -> None:
    records = load_package(c01_package)
    assert [r.record_id for r in records] == [f"EX-00{i}" for i in range(1, 9)]
    assert records[2].actual == Decimal("108200.00")
    assert records[2].forecast == Decimal("95000.00")
    assert records[2].raw["commentary"] == ""
    assert all(r.parse_issues == () for r in records)


def test_malformed_and_missing_numbers_are_recorded_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "pkg.csv"
    path.write_text(f"{HEADER}\n{_row('12x', '')}\n", encoding="utf-8")
    [record] = load_package(path)
    assert record.actual is None
    assert record.forecast is None
    assert record.parse_issues == (
        "actual: malformed numeric value '12x'",
        "forecast: missing value",
    )


def test_missing_column_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pkg.csv"
    header = ",".join(c for c in REQUIRED_COLUMNS if c != "forecast")
    path.write_text(header + "\n", encoding="utf-8")
    with pytest.raises(IngestionError, match="missing required columns: forecast"):
        load_package(path)


def test_oversized_package_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "pkg.csv"
    path.write_text(f"{HEADER}\n{_row('1', '1')}\n", encoding="utf-8")
    monkeypatch.setattr(ingestion, "MAX_PACKAGE_BYTES", 10)
    with pytest.raises(IngestionError, match="package exceeds 10 bytes"):
        load_package(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="cannot read package"):
        load_package(tmp_path / "absent.csv")
