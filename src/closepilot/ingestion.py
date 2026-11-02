"""Load a close-package CSV safely. All cell values are treated as untrusted data."""

from __future__ import annotations

import csv
import re
from decimal import Decimal
from pathlib import Path

from closepilot.models import REQUIRED_COLUMNS, Record

MAX_PACKAGE_BYTES = 1024 * 1024
MAX_RECORDS = 10_000

# Plain decimal only: optional minus sign, digits, optional fraction.
# Rejects thousands separators, exponents, NaN/Infinity, currency symbols.
_AMOUNT_RE = re.compile(r"-?\d+(\.\d+)?")


class IngestionError(ValueError):
    """Raised when a package cannot be read as a whole (missing file, bad header, too large)."""


def parse_amount(value: str) -> Decimal | None:
    """Return the value as ``Decimal``, or ``None`` if it is blank or not a plain decimal."""
    text = value.strip()
    if not _AMOUNT_RE.fullmatch(text):
        return None
    return Decimal(text)


def _amount(row: dict[str, str], column: str, issues: list[str]) -> Decimal | None:
    text = row.get(column, "")
    if not text.strip():
        issues.append(f"{column}: missing value")
        return None
    parsed = parse_amount(text)
    if parsed is None:
        issues.append(f"{column}: malformed numeric value {text.strip()!r}")
    return parsed


def load_package(path: Path) -> list[Record]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise IngestionError(f"cannot read package '{path}': {exc.strerror}") from exc
    if size > MAX_PACKAGE_BYTES:
        raise IngestionError(f"package exceeds {MAX_PACKAGE_BYTES} bytes")

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = [name.strip() for name in reader.fieldnames or []]
        missing = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing:
            raise IngestionError(f"package is missing required columns: {', '.join(missing)}")
        reader.fieldnames = header

        records: list[Record] = []
        for row in reader:
            if len(records) >= MAX_RECORDS:
                raise IngestionError(f"package exceeds {MAX_RECORDS} records")
            raw = {col: (row.get(col) or "").strip() for col in REQUIRED_COLUMNS}
            issues: list[str] = []
            # DictReader puts surplus cells under the None key and fills short rows
            # with None. Either means values may be shifted into the wrong columns.
            extra = row.get(None)
            if extra or any(row.get(col) is None for col in header):
                issues.append(
                    f"row: field count does not match header ({len(header)} columns expected)"
                )
            actual = _amount(raw, "actual", issues)
            forecast = _amount(raw, "forecast", issues)
            records.append(
                Record(
                    line_number=reader.line_num,
                    raw=raw,
                    actual=actual,
                    forecast=forecast,
                    parse_issues=tuple(issues),
                )
            )
    return records
