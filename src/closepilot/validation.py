"""Deterministic control engine.

Calculation rules (documented, tested):

* variance amount = actual - forecast (exact Decimal arithmetic).
* variance pct    = amount / |forecast| * 100, rounded ROUND_HALF_UP to 2 dp.
  The absolute forecast keeps the sign of the percentage equal to the sign of the
  amount when forecasts are negative.
* Thresholds are compared against the *rounded* percentage, so a value displayed
  as 10.00% never triggers a "> 10%" rule.
* Zero forecast: actual = 0 -> pct = 0.00; actual != 0 -> pct undefined (None)
  and C01 returns REVIEW_REQUIRED.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from closepilot.config import ControlConfig
from closepilot.models import Finding, Record, RecordResult, Status, Variance

_TWO_DP = Decimal("0.01")
_HUNDRED = Decimal(100)


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
                f"absolute variance {abs(variance.pct):.2f}% exceeds review threshold "
                f"{threshold:.2f}%"
            ),
            threshold_pct=threshold,
        )
    return None


def check_c07_numeric_integrity(record: Record) -> list[Finding]:
    """C07 (numeric part): a missing or malformed amount is unusable -> ERROR.

    Fails closed: if either amount is unusable, an ERROR is returned even when
    no parse issue text was recorded, so such a record can never be PASS.
    """
    findings = [
        Finding(control_id="C07", status=Status.ERROR, message=issue)
        for issue in record.parse_issues
    ]
    if not findings and (record.actual is None or record.forecast is None):
        findings.append(
            Finding(control_id="C07", status=Status.ERROR, message="unusable numeric value")
        )
    return findings


def validate_record(record: Record, config: ControlConfig) -> RecordResult:
    integrity = check_c07_numeric_integrity(record)
    if record.actual is None or record.forecast is None:
        # No variance can be computed; the record is ERROR via C07.
        return RecordResult(record=record, variance=None, findings=tuple(integrity))
    variance = compute_variance(record.actual, record.forecast)
    findings = integrity + [
        f for f in (check_c01_material_variance(variance, config),) if f is not None
    ]
    return RecordResult(record=record, variance=variance, findings=tuple(findings))


def validate_package(records: Iterable[Record], config: ControlConfig) -> list[RecordResult]:
    return [validate_record(record, config) for record in records]
