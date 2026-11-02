"""Deterministic control engine.

Calculation rules (documented, tested):

* variance amount = actual - forecast (exact Decimal arithmetic).
* variance pct    = amount / |forecast| * 100, rounded ROUND_HALF_UP to 2 dp.
  Using |forecast| keeps the sign of the percentage equal to the sign of the
  amount when forecasts are negative.
* Thresholds are compared against the *rounded* percentage, so a value displayed
  as 10.00% never triggers a "> 10%" rule.
* Zero forecast: actual = 0 -> pct = 0.00; actual != 0 -> pct undefined (None).
  An undefined percentage is treated as exceeding every threshold: C01 returns
  REVIEW_REQUIRED and C05 requires commentary. C02 does not apply.

Control catalogue (thresholds from config/controls.yaml):

* C01 Material variance    |pct| >  review                  -> REVIEW_REQUIRED
* C02 Moderate variance    warning <= |pct| <= review        -> WARNING
* C03 Master-data mapping  mapping_status not allowed        -> REVIEW_REQUIRED
* C04 Duplicate record     record_id occurs > 1 in package  -> REVIEW_REQUIRED
* C05 Required commentary  |pct| >= commentary_required and
                           commentary blank                 -> REVIEW_REQUIRED
* C06 Currency and period  currency / period not allowed     -> REVIEW_REQUIRED
* C07 Required fields      required field blank, amount
                           malformed, or row field-count
                           mismatch                          -> ERROR

Blank values are reported once, by C07; C03, C04 and C06 only judge non-blank values.
A row whose field count does not match the header is structurally malformed: its
values may sit in the wrong columns, so it receives only the C07 field-count finding,
is not counted for C04 and is excluded from control totals.
Matching is exact and case-sensitive (record_id, currency, period, mapping_status).
Every finding stays visible; the record status is the highest severity.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from closepilot.config import ControlConfig
from closepilot.models import (
    CONTROL_IDS,
    REQUIRED_COLUMNS,
    Finding,
    Record,
    RecordResult,
    Status,
    Variance,
)
from closepilot.untrusted import quote

_TWO_DP = Decimal("0.01")
_HUNDRED = Decimal(100)

# Every column must be present and non-blank except commentary, whose
# requirement depends on the variance (C05). actual/forecast are checked
# during ingestion, which records their parse issues.
_REQUIRED_TEXT_FIELDS: tuple[str, ...] = tuple(
    c for c in REQUIRED_COLUMNS if c not in {"commentary", "actual", "forecast"}
)


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def compute_variance(actual: Decimal, forecast: Decimal) -> Variance:
    amount = actual - forecast
    pct: Decimal | None
    if forecast == 0:
        pct = Decimal("0.00") if actual == 0 else None
    else:
        pct = (amount / abs(forecast) * _HUNDRED).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    return Variance(actual=actual, forecast=forecast, amount=amount, pct=pct)


def check_c01_material_variance(variance: Variance, config: ControlConfig) -> Finding | None:
    """C01: absolute variance pct strictly above the review threshold -> REVIEW_REQUIRED."""
    threshold = config.variance_review_pct
    if variance.pct is None:
        return Finding(
            control_id="C01",
            status=Status.REVIEW_REQUIRED,
            message=(
                "variance percentage is undefined (zero forecast, non-zero actual); "
                "human review required"
            ),
            threshold_pct=threshold,
        )
    if abs(variance.pct) > threshold:
        return Finding(
            control_id="C01",
            status=Status.REVIEW_REQUIRED,
            message=(
                f"absolute variance {_fmt(abs(variance.pct))}% exceeds review threshold "
                f"{_fmt(threshold)}%"
            ),
            threshold_pct=threshold,
        )
    return None


def check_c02_moderate_variance(variance: Variance, config: ControlConfig) -> Finding | None:
    """C02: warning <= |pct| <= review (both inclusive) -> WARNING."""
    if variance.pct is None:
        return None
    low, high = config.variance_warning_pct, config.variance_review_pct
    if low <= abs(variance.pct) <= high:
        return Finding(
            control_id="C02",
            status=Status.WARNING,
            message=(
                f"absolute variance {_fmt(abs(variance.pct))}% is within the warning band "
                f"{_fmt(low)}% to {_fmt(high)}%"
            ),
            threshold_pct=low,
        )
    return None


def check_c03_mapping(record: Record, config: ControlConfig) -> Finding | None:
    """C03: a non-blank mapping_status outside the allowed values -> REVIEW_REQUIRED."""
    value = record.raw.get("mapping_status", "")
    if value and value not in config.mapping_valid_values:
        allowed = ", ".join(sorted(config.mapping_valid_values))
        return Finding(
            control_id="C03",
            status=Status.REVIEW_REQUIRED,
            message=f"mapping_status {quote(value)} is not an allowed value (allowed: {allowed})",
        )
    return None


def check_c04_duplicate(record: Record, id_counts: Counter[str]) -> Finding | None:
    """C04: a non-blank record_id occurring more than once in the package -> REVIEW_REQUIRED."""
    count = id_counts.get(record.record_id, 0)
    if record.record_id and count > 1:
        return Finding(
            control_id="C04",
            status=Status.REVIEW_REQUIRED,
            message=f"record_id {quote(record.record_id)} appears {count} times in this package",
        )
    return None


def check_c05_commentary(
    record: Record, variance: Variance, config: ControlConfig
) -> Finding | None:
    """C05: |pct| >= commentary threshold (or undefined) with blank commentary."""
    threshold = config.commentary_required_pct
    required = variance.pct is None or abs(variance.pct) >= threshold
    if required and not record.raw.get("commentary", ""):
        return Finding(
            control_id="C05",
            status=Status.REVIEW_REQUIRED,
            message=(
                f"commentary is required when absolute variance is at least {_fmt(threshold)}% "
                "(or undefined) but is blank"
            ),
            threshold_pct=threshold,
        )
    return None


def check_c06_currency_period(record: Record, config: ControlConfig) -> list[Finding]:
    """C06: a non-blank currency or period outside the configured values -> REVIEW_REQUIRED."""
    findings: list[Finding] = []
    currency = record.raw.get("currency", "")
    if currency and currency not in config.allowed_currencies:
        allowed = ", ".join(sorted(config.allowed_currencies))
        findings.append(
            Finding(
                control_id="C06",
                status=Status.REVIEW_REQUIRED,
                message=f"currency {quote(currency)} is not supported (allowed: {allowed})",
            )
        )
    period = record.raw.get("period", "")
    if period and period not in config.allowed_periods:
        allowed = ", ".join(sorted(config.allowed_periods))
        findings.append(
            Finding(
                control_id="C06",
                status=Status.REVIEW_REQUIRED,
                message=f"period {quote(period)} is not an allowed reporting period "
                f"(allowed: {allowed})",
            )
        )
    return findings


def check_c07_required_fields(record: Record) -> list[Finding]:
    """C07: blank required field, malformed amount or field-count mismatch -> ERROR.

    Fails closed: if either amount is unusable, an ERROR is returned even when
    no parse issue text was recorded, so such a record can never be PASS.
    A malformed row reports only its field-count issue; its other fields are untrusted.
    """
    if record.malformed_row:
        issues = record.parse_issues or ("row: field count does not match header",)
        return [Finding(control_id="C07", status=Status.ERROR, message=m) for m in issues]
    messages = [
        f"{name}: missing value" for name in _REQUIRED_TEXT_FIELDS if not record.raw.get(name, "")
    ]
    messages.extend(record.parse_issues)
    if not record.parse_issues and (record.actual is None or record.forecast is None):
        messages.append("unusable numeric value")
    return [Finding(control_id="C07", status=Status.ERROR, message=m) for m in messages]


def validate_record(
    record: Record, config: ControlConfig, id_counts: Counter[str] | None = None
) -> RecordResult:
    """Run every control on one record. ``id_counts`` enables the package-level C04 check."""
    if record.malformed_row:
        return RecordResult(
            record=record, variance=None, findings=tuple(check_c07_required_fields(record))
        )
    findings: list[Finding | None] = [
        check_c03_mapping(record, config),
        check_c04_duplicate(record, id_counts or Counter()),
        *check_c06_currency_period(record, config),
        *check_c07_required_fields(record),
    ]
    variance: Variance | None = None
    if record.actual is not None and record.forecast is not None:
        variance = compute_variance(record.actual, record.forecast)
        findings += [
            check_c01_material_variance(variance, config),
            check_c02_moderate_variance(variance, config),
            check_c05_commentary(record, variance, config),
        ]
    # Stable sort: catalogue order, original order within a control.
    ordered = sorted((f for f in findings if f is not None), key=lambda f: f.control_id)
    return RecordResult(record=record, variance=variance, findings=tuple(ordered))


def validate_package(records: Iterable[Record], config: ControlConfig) -> list[RecordResult]:
    records = list(records)
    id_counts = Counter(r.record_id for r in records if r.record_id and not r.malformed_row)
    return [validate_record(record, config, id_counts) for record in records]


@dataclass(frozen=True)
class CurrencyTotal:
    records: int
    actual: Decimal
    forecast: Decimal

    @property
    def variance(self) -> Decimal:
        return self.actual - self.forecast


@dataclass(frozen=True)
class PackageSummary:
    total_records: int
    status_counts: dict[Status, int]
    findings_by_control: dict[str, int]
    control_totals: dict[str, CurrencyTotal]
    records_excluded_from_totals: int


def summarise(results: Sequence[RecordResult]) -> PackageSummary:
    """Package summary.

    Control totals are grouped by the record's currency value (never summed across
    currencies) and include every record whose amounts are usable, flagged or not.
    Duplicate records (C04) are therefore counted once per occurrence. Records with
    unusable amounts or a malformed row are counted in ``records_excluded_from_totals``.
    """
    statuses = Counter(r.status for r in results)
    by_control = Counter(f.control_id for r in results for f in r.findings)
    totals: dict[str, CurrencyTotal] = {}
    excluded = 0
    for result in results:
        v = result.variance
        if v is None:
            excluded += 1
            continue
        currency = result.record.raw.get("currency", "") or "(blank)"
        prev = totals.get(currency, CurrencyTotal(0, Decimal(0), Decimal(0)))
        totals[currency] = CurrencyTotal(
            prev.records + 1, prev.actual + v.actual, prev.forecast + v.forecast
        )
    return PackageSummary(
        total_records=len(results),
        status_counts={s: statuses.get(s, 0) for s in Status},
        findings_by_control={c: by_control.get(c, 0) for c in CONTROL_IDS},
        control_totals=dict(sorted(totals.items())),
        records_excluded_from_totals=excluded,
    )
