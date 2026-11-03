"""Case workflow: state machine, role permissions and the human-only decision gate.

The audit log is the single source of truth. A case's current state is derived by
replaying its events through :func:`apply_event`, the same function that validates a new
action before it is written. Every rule therefore holds for live actions *and* for
whatever is found in the log: an event that breaks a rule (for example an agent
"approving" a case after the log was hand-edited) makes replay fail closed.

Who may do what
---------------

=================  ==========================================  =====================
Action             From state(s) -> to state                   Permitted actors
=================  ==========================================  =====================
OPEN_CASE          (none) -> OPEN                              control-analyst, human
HANDOFF            OPEN -> INVESTIGATING                       control-analyst, human
REQUEST_EVIDENCE   INVESTIGATING, READY -> AWAITING_EVIDENCE   exception-investigator,
                                                               human
ATTACH_EVIDENCE    AWAITING_EVIDENCE -> INVESTIGATING          human only
ADD_COMMENTARY     any undecided state -> unchanged            human only
RECOMMEND          INVESTIGATING -> READY_FOR_HUMAN_REVIEW     exception-investigator
RETURN_FOR_REWORK  READY -> INVESTIGATING                      human only
DECIDE             READY -> APPROVED or REJECTED               human only
CLOSE              APPROVED, REJECTED -> CLOSED                human only
=================  ==========================================  =====================

Additional rules:

* Independently of the table, an agent can never produce a state outside OPEN,
  INVESTIGATING, AWAITING_EVIDENCE and READY_FOR_HUMAN_REVIEW, and only a DECIDE event
  may carry a human decision.
* Evidence condition: if the case was opened for a control listed in
  ``evidence_required_controls`` or evidence was requested, approval can be neither
  recommended nor decided until a human has attached at least one evidence reference.
  Evidence references are labels supplied by the human; ClosePilot records them and does
  not claim to have inspected any document behind them.
* APPROVED and REJECTED can only move to CLOSED: a decision is never overridden or
  reopened, and a rejection cannot be reversed by any actor.
* A permission failure on an existing case is itself recorded as an ACCESS_DENIED event
  before the error is raised. Invalid transitions and bad input are rejected without an
  event, because nothing was attempted beyond the caller's own authority.

Trust boundary: this module trusts the ``Actor`` it is given. It guarantees that an
actor of type AGENT can never approve or reject; establishing that a HUMAN actor really is
the human is the job of the calling interface (see the CLI reviewer check).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from closepilot.audit import AuditIntegrityError, AuditLog
from closepilot.config import ControlConfig
from closepilot.models import (
    AGENT_STATES,
    CONTROL_ANALYST,
    CONTROL_IDS,
    EXCEPTION_INVESTIGATOR,
    KNOWN_AGENTS,
    Action,
    Actor,
    ActorType,
    AuditEvent,
    Case,
    CaseState,
    Decision,
    DetailValue,
    Recommendation,
    RecordResult,
    Status,
)

MAX_TEXT_CHARS = 2000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class WorkflowError(Exception):
    """Base class for rejected workflow actions."""


class CaseNotFound(WorkflowError):
    pass


class CaseExists(WorkflowError):
    pass


class InvalidTransition(WorkflowError):
    pass


class PermissionDenied(WorkflowError):
    pass


class EvidenceRequired(WorkflowError):
    pass


class InvalidInput(WorkflowError):
    pass


@dataclass(frozen=True)
class _Rule:
    from_states: frozenset[CaseState | None]
    agents: frozenset[str]
    humans: bool


_UNDECIDED: frozenset[CaseState | None] = frozenset(AGENT_STATES)
_S = CaseState

_RULES: dict[Action, _Rule] = {
    Action.OPEN_CASE: _Rule(frozenset({None}), frozenset({CONTROL_ANALYST.name}), True),
    Action.HANDOFF: _Rule(frozenset({_S.OPEN}), frozenset({CONTROL_ANALYST.name}), True),
    Action.REQUEST_EVIDENCE: _Rule(
        frozenset({_S.INVESTIGATING, _S.READY_FOR_HUMAN_REVIEW}),
        frozenset({EXCEPTION_INVESTIGATOR.name}),
        True,
    ),
    Action.ATTACH_EVIDENCE: _Rule(frozenset({_S.AWAITING_EVIDENCE}), frozenset(), True),
    Action.ADD_COMMENTARY: _Rule(_UNDECIDED, frozenset(), True),
    Action.RECOMMEND: _Rule(
        frozenset({_S.INVESTIGATING}), frozenset({EXCEPTION_INVESTIGATOR.name}), False
    ),
    Action.RETURN_FOR_REWORK: _Rule(frozenset({_S.READY_FOR_HUMAN_REVIEW}), frozenset(), True),
    Action.DECIDE: _Rule(frozenset({_S.READY_FOR_HUMAN_REVIEW}), frozenset(), True),
    Action.CLOSE: _Rule(frozenset({_S.APPROVED, _S.REJECTED}), frozenset(), True),
}


def _target_state(
    action: Action, current: CaseState | None, decision: Decision | None
) -> CaseState:
    match action:
        case Action.OPEN_CASE:
            return _S.OPEN
        case Action.HANDOFF | Action.ATTACH_EVIDENCE | Action.RETURN_FOR_REWORK:
            return _S.INVESTIGATING
        case Action.REQUEST_EVIDENCE:
            return _S.AWAITING_EVIDENCE
        case Action.RECOMMEND:
            return _S.READY_FOR_HUMAN_REVIEW
        case Action.DECIDE:
            if decision is None:
                raise InvalidInput("a decision is required")
            return _S.APPROVED if decision is Decision.APPROVED else _S.REJECTED
        case Action.CLOSE:
            return _S.CLOSED
        case Action.ADD_COMMENTARY | Action.ACCESS_DENIED:
            if current is None:
                raise CaseNotFound("case does not exist")
            return current
    raise InvalidTransition(f"unknown action {action}")  # pragma: no cover


# --- input validation -------------------------------------------------------------------


def _text(value: str | None, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise InvalidInput(f"{name} is required")
    if len(text) > MAX_TEXT_CHARS:
        raise InvalidInput(f"{name} exceeds {MAX_TEXT_CHARS} characters")
    if _CONTROL_CHARS_RE.search(text):
        raise InvalidInput(f"{name} contains control characters")
    return text


def _identifier(value: str, name: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise InvalidInput(
            f"{name} must be 1-64 characters of letters, digits, '.', '_' or '-' "
            "and start with a letter or digit"
        )
    return value


# --- core rule engine -------------------------------------------------------------------


def _authorise(event: AuditEvent, rule: _Rule) -> None:
    action = event.action.value
    if event.actor_type is ActorType.UNVERIFIED:
        raise PermissionDenied(f"an unverified actor may not perform {action}")
    if event.actor_type is ActorType.AGENT:
        if event.actor_name not in KNOWN_AGENTS:
            raise PermissionDenied(f"unknown agent {event.actor_name!r} may not perform {action}")
        if event.actor_name not in rule.agents:
            raise PermissionDenied(f"agent {event.actor_name!r} may not perform {action}")
    elif not rule.humans:
        raise PermissionDenied(f"{action} is performed by an agent, not a human")
    else:
        check_human_name(event.actor_name)


MAX_ACTOR_NAME_CHARS = 64


def check_human_name(name: str) -> None:
    """A human actor needs a plain, distinct name so the audit trail is unambiguous."""
    if not name.strip():
        raise PermissionDenied("a human actor must be named")
    if len(name) > MAX_ACTOR_NAME_CHARS:
        raise PermissionDenied(
            f"a human actor name must be at most {MAX_ACTOR_NAME_CHARS} characters"
        )
    if _CONTROL_CHARS_RE.search(name) or "\n" in name or "\r" in name or "\t" in name:
        raise PermissionDenied("a human actor name may not contain control characters")
    if name.strip().lower() in KNOWN_AGENTS:
        raise PermissionDenied(f"a human actor may not use an agent's name ({name.strip()!r})")


def assert_agent_state_invariant(event: AuditEvent) -> None:
    """Hard invariant, independent of the rule table: agents stay in agent states."""
    if event.actor_type is not ActorType.HUMAN and event.new_state not in AGENT_STATES:
        raise PermissionDenied(
            f"only a human may move a case to {event.new_state.value}; "
            f"{event.actor_name!r} is an agent"
        )
    if event.actor_type is not ActorType.HUMAN and event.human_decision is not None:
        raise PermissionDenied("only a human may record a decision")


def _evidence_satisfied(case: Case) -> bool:
    needed = case.evidence_required or case.evidence_requested
    return not needed or bool(case.evidence_references)


def _detail_list(details: dict[str, DetailValue], key: str) -> list[str]:
    value = details.get(key)
    if not isinstance(value, list):
        raise InvalidInput(f"details.{key} must be a list")
    return value


def _check_access_denied(case: Case, event: AuditEvent) -> None:
    """An ACCESS_DENIED record only states that an attempt was refused. It must leave the
    case unchanged and may not carry a decision, recommendation or evidence, so a forged
    denial cannot be used to place agent-attributed decision data in the log."""
    if event.previous_state != case.state or event.new_state != case.state:
        raise InvalidTransition("ACCESS_DENIED must leave the case unchanged")
    if event.human_decision is not None or event.recommendation is not None:
        raise InvalidTransition("ACCESS_DENIED may not carry a decision or recommendation")
    if event.evidence_references:
        raise InvalidTransition("ACCESS_DENIED may not carry evidence references")
    attempted = event.details.get("attempted_action")
    valid = {a.value for a in Action} - {Action.ACCESS_DENIED.value}
    if set(event.details) != {"attempted_action"} or attempted not in valid:
        raise InvalidTransition(
            "ACCESS_DENIED details must contain exactly a valid attempted_action"
        )


def apply_event(case: Case | None, event: AuditEvent) -> Case:
    """Validate ``event`` against ``case`` and return the resulting case.

    Raises a :class:`WorkflowError` subclass if the event is not allowed.
    """
    current = None if case is None else case.state
    if event.action is Action.ACCESS_DENIED:
        if case is None:
            raise InvalidTransition("ACCESS_DENIED must refer to an existing case")
        _check_access_denied(case, event)
        return replace(case, event_ids=(*case.event_ids, event.event_id))

    rule = _RULES[event.action]
    _authorise(event, rule)
    assert_agent_state_invariant(event)

    if case is None and event.action is not Action.OPEN_CASE:
        raise CaseNotFound(f"case {event.case_id!r} does not exist")
    if case is not None and event.action is Action.OPEN_CASE:
        raise CaseExists(f"case {event.case_id!r} already exists")
    if event.previous_state != current:
        raise InvalidTransition(
            f"event previous_state {event.previous_state} does not match case state {current}"
        )
    if current not in rule.from_states:
        state = "no case" if current is None else current.value
        raise InvalidTransition(f"{event.action.value} is not allowed from {state}")
    expected = _target_state(event.action, current, event.human_decision)
    if event.new_state != expected:
        raise InvalidTransition(
            f"{event.action.value} from {current} must lead to {expected.value}, "
            f"not {event.new_state.value}"
        )
    if event.human_decision is not None and event.action is not Action.DECIDE:
        raise InvalidTransition("only a DECIDE event may carry a human decision")
    if event.recommendation is not None and event.action not in {Action.RECOMMEND, Action.DECIDE}:
        raise InvalidTransition(f"{event.action.value} may not carry a recommendation")

    details = dict(event.details)
    refs = event.evidence_references
    if event.action is Action.OPEN_CASE:
        control_ids = _detail_list(details, "control_ids")
        if not control_ids or not set(control_ids) <= set(CONTROL_IDS):
            raise InvalidInput("details.control_ids must list known control IDs")
        if event.control_id != control_ids[0]:
            raise InvalidInput("control_id must be the case's primary control")
        evidence_required = details.get("evidence_required")
        if not isinstance(evidence_required, bool):
            raise InvalidInput("details.evidence_required must be true or false")
        if refs:
            raise InvalidInput("OPEN_CASE may not carry evidence references")
        return Case(
            case_id=_identifier(event.case_id, "case_id"),
            submission_id=_text(event.submission_id, "submission_id"),
            state=event.new_state,
            control_ids=tuple(control_ids),
            evidence_required=evidence_required,
            owner=event.actor_name,
            event_ids=(event.event_id,),
        )

    if case is None:  # pragma: no cover - excluded by the checks above; narrows the type
        raise CaseNotFound(f"case {event.case_id!r} does not exist")
    if event.submission_id != case.submission_id:
        raise InvalidInput("submission_id does not match the case")
    updated = replace(case, state=event.new_state, event_ids=(*case.event_ids, event.event_id))
    match event.action:
        case Action.HANDOFF:
            to = details.get("to")
            if not isinstance(to, str) or to not in KNOWN_AGENTS:
                raise InvalidInput(f"hand-off target must be one of {sorted(KNOWN_AGENTS)}")
            _check_no_refs(refs, event.action)
            return replace(updated, owner=to)
        case Action.REQUEST_EVIDENCE:
            _text(event.rationale, "evidence request")
            _check_no_refs(refs, event.action)
            # Any earlier recommendation predates the evidence now requested; clear it so
            # no stale recommendation is shown while the case waits for evidence.
            return replace(
                updated,
                evidence_requested=True,
                recommendation=None,
                recommendation_rationale=None,
            )
        case Action.ATTACH_EVIDENCE:
            if len(refs) != 1:
                raise InvalidInput("attach exactly one evidence reference")
            ref = _identifier(refs[0], "evidence reference")
            if ref in case.evidence_references:
                raise InvalidInput(f"evidence reference {ref!r} is already attached")
            return replace(updated, evidence_references=(*case.evidence_references, ref))
        case Action.ADD_COMMENTARY:
            text = _text(event.rationale, "commentary")
            _check_no_refs(refs, event.action)
            return replace(updated, commentary=(*case.commentary, text))
        case Action.RECOMMEND:
            if event.recommendation is None:
                raise InvalidInput("a recommendation is required")
            rationale = _text(event.rationale, "recommendation rationale")
            _check_snapshot(refs, case)
            if event.recommendation is Recommendation.APPROVE and not _evidence_satisfied(case):
                raise EvidenceRequired(
                    f"case {case.case_id} needs at least one evidence reference attached by "
                    "the Finance Reviewer before approval can be recommended"
                )
            return replace(
                updated,
                recommendation=event.recommendation,
                recommendation_rationale=rationale,
            )
        case Action.RETURN_FOR_REWORK:
            _text(event.rationale, "rework reason")
            _check_no_refs(refs, event.action)
            return replace(updated, recommendation=None, recommendation_rationale=None)
        case Action.DECIDE:
            rationale = _text(event.rationale, "decision rationale")
            _check_snapshot(refs, case)
            if event.recommendation != case.recommendation:
                raise InvalidInput("DECIDE must record the case's current recommendation")
            if event.human_decision is Decision.APPROVED and not _evidence_satisfied(case):
                raise EvidenceRequired(
                    f"case {case.case_id} cannot be approved before at least one evidence "
                    "reference has been attached"
                )
            return replace(
                updated,
                decision=event.human_decision,
                decision_rationale=rationale,
                decided_by=event.actor_name,
            )
        case Action.CLOSE:
            _check_no_refs(refs, event.action)
            return updated
    raise InvalidTransition(f"unhandled action {event.action}")  # pragma: no cover


def _check_no_refs(refs: tuple[str, ...], action: Action) -> None:
    if refs:
        raise InvalidInput(f"{action.value} may not carry evidence references")


def _check_snapshot(refs: tuple[str, ...], case: Case) -> None:
    if refs != case.evidence_references:
        raise InvalidInput("event must record the case's current evidence references")


def replay(events: Iterable[AuditEvent]) -> dict[str, Case]:
    """Rebuild every case from the log, re-validating each event against the rules."""
    cases: dict[str, Case] = {}
    for event in events:
        try:
            cases[event.case_id] = apply_event(cases.get(event.case_id), event)
        except WorkflowError as exc:
            raise AuditIntegrityError(
                f"audit event {event.event_id} violates workflow rules: {exc}"
            ) from exc
    return cases


# --- service ----------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def case_id_for(result: RecordResult) -> str:
    """The case ID a record belongs to: its record_id, or ``LINE-<n>`` when the
    record_id is not a safe identifier."""
    rid = result.record.record_id
    return rid if _ID_RE.fullmatch(rid) else f"LINE-{result.record.line_number}"


class CaseWorkflow:
    """Records workflow actions in the audit log. Every method is all-or-nothing:
    either the event is durably appended and the new case returned, or an error is
    raised and nothing about the case changed.

    ``sealer`` is applied to every human event as it is appended (see
    ``closepilot.reviewer``), and ``verifier`` checks the whole log before it is replayed;
    the reviewer interface passes both, so it never acts on a log holding a human event
    it did not seal.
    """

    def __init__(
        self,
        log: AuditLog,
        config: ControlConfig,
        clock: Callable[[], datetime] = _utc_now,
        *,
        sealer: Callable[[AuditEvent], AuditEvent] | None = None,
        verifier: Callable[[Sequence[AuditEvent]], None] | None = None,
    ) -> None:
        self.log = log
        self.config = config
        self._clock = clock
        self._sealer = sealer
        self._verifier = verifier

    def events(self) -> list[AuditEvent]:
        """The full log, checked by the verifier when one is set."""
        events = self.log.read()
        if self._verifier is not None:
            self._verifier(events)
        return events

    def _timestamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def cases(self) -> dict[str, Case]:
        return replay(self.events())

    def get(self, case_id: str) -> Case:
        case = self.cases().get(case_id)
        if case is None:
            raise CaseNotFound(f"case {case_id!r} does not exist")
        return case

    def record_denied(self, actor: Actor, case_id: str, attempted: Action, reason: str) -> None:
        """Record a refused attempt that never reached the rule engine (for example a
        failed reviewer check) as an ACCESS_DENIED event on an existing case."""
        if attempted is Action.ACCESS_DENIED:
            raise InvalidInput("attempted action must be a real action")
        with self.log.locked():
            case = replay(self.log.read()).get(case_id)
            if case is None:
                raise CaseNotFound(f"case {case_id!r} does not exist")
            event = AuditEvent(
                event_id="",
                timestamp_utc=self._timestamp(),
                submission_id=case.submission_id,
                case_id=case_id,
                actor_type=actor.actor_type,
                actor_name=actor.name,
                action=Action.ACCESS_DENIED,
                previous_state=case.state,
                new_state=case.state,
                control_id=case.control_ids[0],
                rationale=_text(reason, "reason"),
                details={"attempted_action": attempted.value},
            )
            apply_event(case, event)  # validates the denial record itself
            self.log.append(event)

    def _record(
        self,
        actor: Actor,
        action: Action,
        case_id: str,
        *,
        submission_id: str | None = None,
        decision: Decision | None = None,
        recommendation: Recommendation | None = None,
        rationale: str | None = None,
        evidence_references: tuple[str, ...] | None = None,
        details: dict[str, DetailValue] | None = None,
        control_id: str | None = None,
    ) -> Case:
        with self.log.locked():
            case = replay(self.events()).get(case_id)
            if case is None and action is not Action.OPEN_CASE:
                raise CaseNotFound(f"case {case_id!r} does not exist")
            current = None if case is None else case.state
            snapshot = action in {Action.RECOMMEND, Action.DECIDE}
            refs = evidence_references
            if refs is None:
                refs = case.evidence_references if (snapshot and case) else ()
            event = AuditEvent(
                event_id="",
                timestamp_utc=self._timestamp(),
                submission_id=(submission_id or "") if case is None else case.submission_id,
                case_id=case_id,
                actor_type=actor.actor_type,
                actor_name=actor.name,
                action=action,
                previous_state=current,
                new_state=_target_state(action, current, decision),
                control_id=control_id if case is None else case.control_ids[0],
                evidence_references=refs,
                recommendation=(
                    case.recommendation
                    if action is Action.DECIDE and case is not None
                    else recommendation
                ),
                human_decision=decision,
                rationale=None if rationale is None else rationale.strip(),
                details=details or {},
            )
            try:
                apply_event(case, event)
            except PermissionDenied as exc:
                if case is not None:
                    self.log.append(
                        replace(
                            event,
                            action=Action.ACCESS_DENIED,
                            new_state=case.state,
                            evidence_references=(),
                            recommendation=None,
                            human_decision=None,
                            rationale=str(exc),
                            details={"attempted_action": action.value},
                        )
                    )
                raise
            sealer = self._sealer if actor.actor_type is ActorType.HUMAN else None
            stored = self.log.append(event, sealer)
            return apply_event(case, stored)

    # Public actions -------------------------------------------------------------------

    def open_case(
        self,
        actor: Actor,
        case_id: str,
        submission_id: str,
        control_ids: Sequence[str],
        summary: str,
    ) -> Case:
        """Open a case. ``control_ids`` lists the primary control first."""
        _identifier(case_id, "case_id")
        evidence_required = bool(set(control_ids) & self.config.evidence_required_controls)
        return self._record(
            actor,
            Action.OPEN_CASE,
            case_id,
            submission_id=submission_id,
            rationale=_text(summary, "summary"),
            details={"control_ids": list(control_ids), "evidence_required": evidence_required},
            control_id=control_ids[0] if control_ids else None,
        )

    def handoff(self, actor: Actor, case_id: str, to_agent: str, note: str) -> Case:
        return self._record(
            actor,
            Action.HANDOFF,
            case_id,
            rationale=_text(note, "hand-off note"),
            details={"to": to_agent},
        )

    def request_evidence(self, actor: Actor, case_id: str, request: str) -> Case:
        return self._record(
            actor, Action.REQUEST_EVIDENCE, case_id, rationale=_text(request, "evidence request")
        )

    def attach_evidence(
        self, actor: Actor, case_id: str, reference: str, note: str | None = None
    ) -> Case:
        return self._record(
            actor,
            Action.ATTACH_EVIDENCE,
            case_id,
            evidence_references=(reference,),
            rationale=None if not note else _text(note, "evidence note"),
        )

    def add_commentary(self, actor: Actor, case_id: str, text: str) -> Case:
        return self._record(
            actor, Action.ADD_COMMENTARY, case_id, rationale=_text(text, "commentary")
        )

    def recommend(
        self, actor: Actor, case_id: str, recommendation: Recommendation, rationale: str
    ) -> Case:
        return self._record(
            actor,
            Action.RECOMMEND,
            case_id,
            recommendation=recommendation,
            rationale=_text(rationale, "recommendation rationale"),
        )

    def return_for_rework(self, actor: Actor, case_id: str, reason: str) -> Case:
        return self._record(
            actor, Action.RETURN_FOR_REWORK, case_id, rationale=_text(reason, "rework reason")
        )

    def decide(self, actor: Actor, case_id: str, decision: Decision, rationale: str) -> Case:
        return self._record(
            actor,
            Action.DECIDE,
            case_id,
            decision=decision,
            rationale=_text(rationale, "decision rationale"),
        )

    def close(self, actor: Actor, case_id: str, note: str | None = None) -> Case:
        return self._record(
            actor,
            Action.CLOSE,
            case_id,
            rationale=None if not note else _text(note, "closing note"),
        )

    def open_cases_for_package(
        self, actor: Actor, results: Sequence[RecordResult]
    ) -> tuple[list[str], list[str]]:
        """Open one case per record_id whose status is REVIEW_REQUIRED or ERROR.

        Rows sharing a record_id (C04 duplicates) share one case. A record_id that is
        not a safe identifier is replaced by ``LINE-<n>``. Re-running is idempotent:
        returns ``(opened, already_open)`` case IDs in package order.
        """
        grouped: dict[str, list[RecordResult]] = {}
        for result in results:
            if result.status not in {Status.REVIEW_REQUIRED, Status.ERROR}:
                continue
            grouped.setdefault(case_id_for(result), []).append(result)

        existing = self.cases()
        opened: list[str] = []
        already: list[str] = []
        for case_id, group in grouped.items():
            if case_id in existing:
                already.append(case_id)
                continue
            findings = [f for r in group for f in r.findings]
            ordered = sorted(findings, key=lambda f: (-f.status.severity, f.control_id))
            control_ids = list(dict.fromkeys(f.control_id for f in ordered))
            messages = dict.fromkeys(
                f"{f.control_id} {f.status.value}: {f.message}" for f in ordered
            )
            summary = "; ".join(messages)
            if len(summary) > MAX_TEXT_CHARS:
                summary = summary[: MAX_TEXT_CHARS - 3] + "..."
            submission = group[0].record.raw.get("submission_id", "") or "(blank)"
            try:
                self.open_case(actor, case_id, submission, control_ids, summary)
            except CaseExists:
                # Opened by a concurrent command after our snapshot; the outcome is the
                # same as if we had seen it, so stay idempotent instead of aborting.
                already.append(case_id)
                continue
            opened.append(case_id)
        return opened, already
