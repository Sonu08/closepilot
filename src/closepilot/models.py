"""Domain models. Pure data: no I/O, no Switch, no AI."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

REQUIRED_COLUMNS: tuple[str, ...] = (
    "record_id",
    "submission_id",
    "period",
    "entity",
    "cost_centre",
    "account",
    "actual",
    "forecast",
    "currency",
    "mapping_status",
    "commentary",
    "submitted_by_role",
)


class Status(Enum):
    """Control result status. ``severity`` defines deterministic precedence."""

    PASS = "PASS"  # noqa: S105 - control status, not a credential
    WARNING = "WARNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ERROR = "ERROR"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY: dict[Status, int] = {
    Status.PASS: 0,
    Status.WARNING: 1,
    Status.REVIEW_REQUIRED: 2,
    Status.ERROR: 3,
}


@dataclass(frozen=True)
class Record:
    """One row of a close package.

    ``raw`` keeps every original string value (untrusted input, never executed).
    ``actual`` / ``forecast`` are ``None`` when the source value is missing or malformed;
    the reason is recorded in ``parse_issues``.
    """

    line_number: int
    raw: dict[str, str]
    actual: Decimal | None
    forecast: Decimal | None
    parse_issues: tuple[str, ...] = ()

    @property
    def record_id(self) -> str:
        return self.raw.get("record_id", "")


@dataclass(frozen=True)
class Variance:
    """Exact variance calculation.

    ``pct`` is ``None`` when mathematically undefined (non-zero actual, zero forecast).
    """

    actual: Decimal
    forecast: Decimal
    amount: Decimal
    pct: Decimal | None


@dataclass(frozen=True)
class Finding:
    """A single control outcome that is not PASS. Every finding stays visible."""

    control_id: str
    status: Status
    message: str
    threshold_pct: Decimal | None = None


@dataclass(frozen=True)
class RecordResult:
    record: Record
    variance: Variance | None
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def status(self) -> Status:
        """Highest-severity status across findings; PASS when there are none."""
        if not self.findings:
            return Status.PASS
        return max((f.status for f in self.findings), key=lambda s: s.severity)
