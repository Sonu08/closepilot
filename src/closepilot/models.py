"""Domain models. Pure data: no I/O, no Switch, no AI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

CONTROL_IDS: tuple[str, ...] = ("C01", "C02", "C03", "C04", "C05", "C06", "C07")

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
    ``malformed_row`` is True when the row's field count does not match the header: its
    values may sit in the wrong columns, so no field of the row is trusted.
    """

    line_number: int
    raw: dict[str, str]
    actual: Decimal | None
    forecast: Decimal | None
    parse_issues: tuple[str, ...] = ()
    malformed_row: bool = False

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


# --- Case workflow ----------------------------------------------------------------------


class CaseState(Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


#: The only states an agent may move a case into.
AGENT_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.OPEN,
        CaseState.INVESTIGATING,
        CaseState.AWAITING_EVIDENCE,
        CaseState.READY_FOR_HUMAN_REVIEW,
    }
)

#: States in which the case is decided or finished; no further investigation happens.
DECIDED_STATES: frozenset[CaseState] = frozenset(
    {CaseState.APPROVED, CaseState.REJECTED, CaseState.CLOSED}
)


class ActorType(Enum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class Action(Enum):
    OPEN_CASE = "OPEN_CASE"
    HANDOFF = "HANDOFF"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    ATTACH_EVIDENCE = "ATTACH_EVIDENCE"
    ADD_COMMENTARY = "ADD_COMMENTARY"
    RECOMMEND = "RECOMMEND"
    RETURN_FOR_REWORK = "RETURN_FOR_REWORK"
    DECIDE = "DECIDE"
    CLOSE = "CLOSE"
    ACCESS_DENIED = "ACCESS_DENIED"


class Recommendation(Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Decision(Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Actor:
    actor_type: ActorType
    name: str


CONTROL_ANALYST = Actor(ActorType.AGENT, "control-analyst")
EXCEPTION_INVESTIGATOR = Actor(ActorType.AGENT, "exception-investigator")
KNOWN_AGENTS: frozenset[str] = frozenset({CONTROL_ANALYST.name, EXCEPTION_INVESTIGATOR.name})
HUMAN_ROLE = "Finance Reviewer"

DetailValue = str | bool | list[str]


@dataclass(frozen=True)
class AuditEvent:
    """One append-only audit record. ``details`` carries action-specific data."""

    event_id: str
    timestamp_utc: str
    submission_id: str
    case_id: str
    actor_type: ActorType
    actor_name: str
    action: Action
    previous_state: CaseState | None
    new_state: CaseState
    control_id: str | None = None
    evidence_references: tuple[str, ...] = ()
    recommendation: Recommendation | None = None
    human_decision: Decision | None = None
    rationale: str | None = None
    details: Mapping[str, DetailValue] = field(default_factory=dict)
    prev_event_hash: str = ""


@dataclass(frozen=True)
class Case:
    """Current case view, derived by replaying audit events (never stored separately)."""

    case_id: str
    submission_id: str
    state: CaseState
    control_ids: tuple[str, ...]
    evidence_required: bool
    owner: str | None = None
    evidence_requested: bool = False
    evidence_references: tuple[str, ...] = ()
    commentary: tuple[str, ...] = ()
    recommendation: Recommendation | None = None
    recommendation_rationale: str | None = None
    decision: Decision | None = None
    decision_rationale: str | None = None
    decided_by: str | None = None
    event_ids: tuple[str, ...] = ()
