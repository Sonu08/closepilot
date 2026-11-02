"""Load a close-package CSV safely. All cell values are treated as untrusted data."""

from __future__ import annotations

import csv
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from closepilot.models import REQUIRED_COLUMNS, Record
from closepilot.untrusted import quote

MAX_PACKAGE_BYTES = 1024 * 1024
MAX_RECORDS = 10_000

# Supported amount precision. 15 + 6 digits keeps every intermediate result, including
# the variance percentage at its extreme (largest amount over smallest forecast), well
# inside Decimal's default 28-digit context, so arithmetic is exact and never raises.
MAX_INTEGER_DIGITS = 15
MAX_FRACTION_DIGITS = 6

# Plain decimal only: optional minus sign, digits, optional fraction.
# Rejects thousands separators, exponents, NaN/Infinity, currency symbols.
_PLAIN_DECIMAL_RE = re.compile(r"-?(\d+)(?:\.(\d+))?")


class IngestionError(ValueError):
    """Raised when a package cannot be read as a whole (missing file, bad header, too large)."""


def _within_precision(match: re.Match[str]) -> bool:
    integer, fraction = match.group(1), match.group(2) or ""
    return len(integer) <= MAX_INTEGER_DIGITS and len(fraction) <= MAX_FRACTION_DIGITS


def parse_amount(value: str) -> Decimal | None:
    """Return the value as ``Decimal``, or ``None`` if blank, malformed or out of range."""
    match = _PLAIN_DECIMAL_RE.fullmatch(value.strip())
    if match is None or not _within_precision(match):
        return None
    return Decimal(match.group(0))


def _amount(row: dict[str, str], column: str, issues: list[str]) -> Decimal | None:
    text = row.get(column, "").strip()
    if not text:
        issues.append(f"{column}: missing value")
        return None
    parsed = parse_amount(text)
    if parsed is None:
        if _PLAIN_DECIMAL_RE.fullmatch(text):
            issues.append(
                f"{column}: numeric value {quote(text)} exceeds supported precision "
                f"({MAX_INTEGER_DIGITS} integer and {MAX_FRACTION_DIGITS} decimal digits)"
            )
        else:
            issues.append(f"{column}: malformed numeric value {quote(text)}")
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
        repeated = sorted(name for name, n in Counter(header).items() if n > 1)
        if repeated:
            raise IngestionError(
                f"package header repeats columns: {', '.join(quote(n) for n in repeated)}"
            )
        missing = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing:
            raise IngestionError(f"package is missing required columns: {', '.join(missing)}")
        reader.fieldnames = header

        records: list[Record] = []
        for row in reader:
            if len(records) >= MAX_RECORDS:
                raise IngestionError(f"package exceeds {MAX_RECORDS} records")
            raw = {col: (row.get(col) or "").strip() for col in REQUIRED_COLUMNS}
            # DictReader puts surplus cells under the None key and fills short rows
            # with None. Either way values may sit in the wrong columns, so the row's
            # amounts are not parsed and no other field is judged.
            if row.get(None) or any(row.get(col) is None for col in header):
                records.append(
                    Record(
                        line_number=reader.line_num,
                        raw=raw,
                        actual=None,
                        forecast=None,
                        parse_issues=(
                            f"row: field count does not match header "
                            f"({len(header)} columns expected)",
                        ),
                        malformed_row=True,
                    )
                )
                continue
            issues: list[str] = []
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
