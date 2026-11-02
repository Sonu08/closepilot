from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from closepilot.audit import AuditIntegrityError, AuditLog, event_to_json
from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.models import (
    CONTROL_ANALYST,
    EXCEPTION_INVESTIGATOR,
    HUMAN_ROLE,
    Action,
    Actor,
    ActorType,
    AuditEvent,
    Case,
    CaseState,
    Decision,
    Recommendation,
)
from closepilot.validation import validate_package
from closepilot.workflow import (
    CaseExists,
    CaseNotFound,
    CaseWorkflow,
    EvidenceRequired,
    InvalidInput,
    InvalidTransition,
    PermissionDenied,
    assert_agent_state_invariant,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HUMAN = Actor(ActorType.HUMAN, HUMAN_ROLE)
CA, INV = CONTROL_ANALYST, EXCEPTION_INVESTIGATOR
S = CaseState


def _clock() -> Callable[[], datetime]:
    start = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
    ticks = iter(range(10_000))
    return lambda: start + timedelta(seconds=next(ticks))


@pytest.fixture
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit" / "events.jsonl")


@pytest.fixture
def wf(log: AuditLog, config: ControlConfig) -> CaseWorkflow:
    return CaseWorkflow(log, config, clock=_clock())


def _open(
    wf: CaseWorkflow, case_id: str = "EX-003", controls: tuple[str, ...] = ("C01", "C05")
) -> Case:
    return wf.open_case(CA, case_id, "FC-2026-10", list(controls), "C01 REVIEW_REQUIRED: 13.89%")


def _investigating(wf: CaseWorkflow, case_id: str = "EX-003", **kw: tuple[str, ...]) -> Case:
    _open(wf, case_id, **kw)
    return wf.handoff(CA, case_id, INV.name, "Material variance: please investigate.")


def _ready(wf: CaseWorkflow, case_id: str = "EX-003") -> Case:
    _investigating(wf, case_id)
    wf.request_evidence(INV, case_id, "Commentary and supporting evidence required.")
    wf.attach_evidence(HUMAN, case_id, "EVD-2026-10-003", "Synthetic programme approval memo.")
    return wf.recommend(
        INV, case_id, Recommendation.APPROVE, "Genuine variance; evidence attached."
    )


def _actions(log: AuditLog) -> list[str]:
    return [e.action.value for e in log.read()]


# --- golden path ------------------------------------------------------------------------


def test_golden_workflow_end_to_end(wf: CaseWorkflow, log: AuditLog) -> None:
    case = _open(wf)
    assert (case.state, case.owner, case.evidence_required) == (S.OPEN, CA.name, True)

    case = wf.handoff(CA, "EX-003", INV.name, "Material variance: please investigate.")
    assert (case.state, case.owner) == (S.INVESTIGATING, INV.name)

    case = wf.request_evidence(INV, "EX-003", "Commentary and supporting evidence required.")
    assert (case.state, case.evidence_requested) == (S.AWAITING_EVIDENCE, True)

    case = wf.add_commentary(
        HUMAN,
        "EX-003",
        "The variance relates to an approved customer-event programme. Keep the case "
        "pending until supporting evidence is attached.",
    )
    assert case.state is S.AWAITING_EVIDENCE

    case = wf.attach_evidence(HUMAN, "EX-003", "EVD-2026-10-003", "Synthetic approval memo.")
    assert (case.state, case.evidence_references) == (S.INVESTIGATING, ("EVD-2026-10-003",))

    case = wf.recommend(
        INV, "EX-003", Recommendation.APPROVE, "Genuine variance; evidence attached."
    )
    assert (case.state, case.recommendation) == (S.READY_FOR_HUMAN_REVIEW, Recommendation.APPROVE)

    case = wf.decide(HUMAN, "EX-003", Decision.APPROVED, "I reviewed the synthetic evidence.")
    assert (case.state, case.decision, case.decided_by) == (
        S.APPROVED,
        Decision.APPROVED,
        HUMAN_ROLE,
    )

    case = wf.close(HUMAN, "EX-003")
    assert case.state is S.CLOSED

    events = log.read()
    assert [e.action for e in events] == [
        Action.OPEN_CASE,
        Action.HANDOFF,
        Action.REQUEST_EVIDENCE,
        Action.ADD_COMMENTARY,
        Action.ATTACH_EVIDENCE,
        Action.RECOMMEND,
        Action.DECIDE,
        Action.CLOSE,
    ]
    decision = events[6]
    assert decision.actor_type is ActorType.HUMAN
    assert decision.actor_name == HUMAN_ROLE
    assert decision.human_decision is Decision.APPROVED
    assert decision.recommendation is Recommendation.APPROVE
    assert decision.evidence_references == ("EVD-2026-10-003",)
    assert decision.control_id == "C01"
    assert (decision.previous_state, decision.new_state) == (S.READY_FOR_HUMAN_REVIEW, S.APPROVED)
    assert decision.rationale == "I reviewed the synthetic evidence."
    assert [e.timestamp_utc for e in events][:2] == ["2026-11-02T09:00:00Z", "2026-11-02T09:00:01Z"]
    assert wf.get("EX-003") == case  # replay reproduces the returned state


def test_human_can_reject_from_ready(wf: CaseWorkflow) -> None:
    _ready(wf)
    case = wf.decide(HUMAN, "EX-003", Decision.REJECTED, "Evidence does not support the spend.")
    assert (case.state, case.decision) == (S.REJECTED, Decision.REJECTED)
    assert wf.close(HUMAN, "EX-003").state is S.CLOSED


# --- human-only gate --------------------------------------------------------------------


@pytest.mark.parametrize("agent", [CA, INV, Actor(ActorType.AGENT, "rogue-agent")])
@pytest.mark.parametrize("decision", [Decision.APPROVED, Decision.REJECTED])
def test_agent_cannot_decide_and_attempt_is_logged(
    wf: CaseWorkflow, log: AuditLog, agent: Actor, decision: Decision
) -> None:
    _ready(wf)
    before = wf.get("EX-003")
    with pytest.raises(PermissionDenied):
        wf.decide(agent, "EX-003", decision, "Looks fine to me.")
    after = wf.get("EX-003")
    assert after.state is S.READY_FOR_HUMAN_REVIEW
    assert after.decision is None
    denied = log.read()[-1]
    assert denied.action is Action.ACCESS_DENIED
    assert (denied.actor_type, denied.actor_name) == (ActorType.AGENT, agent.name)
    assert denied.details == {"attempted_action": "DECIDE"}
    assert (denied.previous_state, denied.new_state) == (S.READY_FOR_HUMAN_REVIEW,) * 2
    assert denied.human_decision is None
    assert after == replace(before, event_ids=(*before.event_ids, denied.event_id))


@pytest.mark.parametrize(
    ("attempt", "setup"),
    [
        (lambda wf: wf.close(INV, "EX-003"), "approved"),
        (lambda wf: wf.attach_evidence(INV, "EX-003", "EVD-FAKE"), "awaiting"),
        (lambda wf: wf.add_commentary(INV, "EX-003", "Commentary from an agent."), "awaiting"),
        (lambda wf: wf.return_for_rework(INV, "EX-003", "Redo."), "ready"),
        (lambda wf: wf.recommend(HUMAN, "EX-003", Recommendation.APPROVE, "x"), "investigating"),
        (lambda wf: wf.recommend(CA, "EX-003", Recommendation.APPROVE, "x"), "investigating"),
        (lambda wf: wf.request_evidence(CA, "EX-003", "x"), "investigating"),
        (lambda wf: wf.handoff(INV, "EX-003", CA.name, "x"), "open"),
    ],
)
def test_role_permissions(
    wf: CaseWorkflow, log: AuditLog, attempt: Callable[[CaseWorkflow], Case], setup: str
) -> None:
    if setup == "open":
        _open(wf)
    elif setup == "investigating":
        _investigating(wf)
    elif setup == "awaiting":
        _investigating(wf)
        wf.request_evidence(INV, "EX-003", "Need evidence.")
    elif setup == "ready":
        _ready(wf)
    else:
        _ready(wf)
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Approved.")
    state_before = wf.get("EX-003").state
    with pytest.raises(PermissionDenied):
        attempt(wf)
    assert wf.get("EX-003").state is state_before
    assert log.read()[-1].action is Action.ACCESS_DENIED


def test_agent_cannot_open_case_as_investigator(wf: CaseWorkflow, log: AuditLog) -> None:
    with pytest.raises(PermissionDenied, match="may not perform OPEN_CASE"):
        wf.open_case(INV, "EX-003", "FC-2026-10", ["C01"], "x")
    assert log.read() == []  # no case exists to attach the attempt to


def test_agent_state_invariant_holds_independently_of_rule_table() -> None:
    forged = AuditEvent(
        event_id="EVT-000009",
        timestamp_utc="2026-11-02T09:00:00Z",
        submission_id="FC-2026-10",
        case_id="EX-003",
        actor_type=ActorType.AGENT,
        actor_name=INV.name,
        action=Action.DECIDE,
        previous_state=S.READY_FOR_HUMAN_REVIEW,
        new_state=S.APPROVED,
        human_decision=Decision.APPROVED,
    )
    with pytest.raises(PermissionDenied, match="only a human may move a case to APPROVED"):
        assert_agent_state_invariant(forged)
    with pytest.raises(PermissionDenied, match="only a human may record a decision"):
        assert_agent_state_invariant(replace(forged, new_state=S.READY_FOR_HUMAN_REVIEW))
    for state in (S.OPEN, S.INVESTIGATING, S.AWAITING_EVIDENCE, S.READY_FOR_HUMAN_REVIEW):
        assert_agent_state_invariant(replace(forged, new_state=state, human_decision=None))


# --- state machine ----------------------------------------------------------------------


def test_human_cannot_decide_before_ready(wf: CaseWorkflow, log: AuditLog) -> None:
    _investigating(wf)
    count = len(log.read())
    with pytest.raises(InvalidTransition, match="DECIDE is not allowed from INVESTIGATING"):
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Approve.")
    assert len(log.read()) == count  # invalid transitions are rejected without an event


@pytest.mark.parametrize(
    ("setup", "attempt", "message"),
    [
        (
            "investigating",
            lambda wf: wf.handoff(CA, "EX-003", INV.name, "x"),
            "HANDOFF is not allowed from INVESTIGATING",
        ),
        (
            "open",
            lambda wf: wf.recommend(INV, "EX-003", Recommendation.REJECT, "x"),
            "RECOMMEND is not allowed from OPEN",
        ),
        (
            "ready",
            lambda wf: wf.close(HUMAN, "EX-003"),
            "CLOSE is not allowed from READY_FOR_HUMAN_REVIEW",
        ),
        (
            "open",
            lambda wf: wf.attach_evidence(HUMAN, "EX-003", "EVD-1"),
            "ATTACH_EVIDENCE is not allowed from OPEN",
        ),
        (
            "closed",
            lambda wf: wf.add_commentary(HUMAN, "EX-003", "x"),
            "ADD_COMMENTARY is not allowed from CLOSED",
        ),
        ("closed", lambda wf: wf.close(HUMAN, "EX-003"), "CLOSE is not allowed from CLOSED"),
    ],
)
def test_invalid_transitions_are_rejected(
    wf: CaseWorkflow, setup: str, attempt: Callable[[CaseWorkflow], Case], message: str
) -> None:
    if setup == "open":
        _open(wf)
    elif setup == "investigating":
        _investigating(wf)
    elif setup == "ready":
        _ready(wf)
    else:
        _ready(wf)
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Approved.")
        wf.close(HUMAN, "EX-003")
    with pytest.raises(InvalidTransition, match=message):
        attempt(wf)


@pytest.mark.parametrize("first", [Decision.APPROVED, Decision.REJECTED])
def test_decision_cannot_be_overridden_or_reopened(wf: CaseWorkflow, first: Decision) -> None:
    _ready(wf)
    wf.decide(HUMAN, "EX-003", first, "Decided.")
    for decision in Decision:
        with pytest.raises(InvalidTransition, match="DECIDE is not allowed from"):
            wf.decide(HUMAN, "EX-003", decision, "Changed my mind.")
    with pytest.raises(InvalidTransition):
        wf.return_for_rework(HUMAN, "EX-003", "Reopen.")
    with pytest.raises(InvalidTransition):
        wf.request_evidence(HUMAN, "EX-003", "More.")
    assert wf.get("EX-003").decision is first


def test_human_rework_and_further_evidence_paths(wf: CaseWorkflow) -> None:
    _ready(wf)
    case = wf.return_for_rework(HUMAN, "EX-003", "Explain the cost-centre split.")
    assert (case.state, case.recommendation) == (S.INVESTIGATING, None)
    case = wf.recommend(INV, "EX-003", Recommendation.APPROVE, "Split explained.")
    case = wf.request_evidence(HUMAN, "EX-003", "Attach the signed programme budget.")
    assert case.state is S.AWAITING_EVIDENCE


def test_unknown_case_and_duplicate_open(wf: CaseWorkflow) -> None:
    with pytest.raises(CaseNotFound):
        wf.handoff(CA, "EX-404", INV.name, "x")
    with pytest.raises(CaseNotFound):
        wf.get("EX-404")
    _open(wf)
    with pytest.raises(CaseExists):
        _open(wf)


# --- evidence condition -----------------------------------------------------------------


def test_cannot_recommend_approval_without_required_evidence(wf: CaseWorkflow) -> None:
    _investigating(wf)  # C01 case: evidence required by configuration
    with pytest.raises(EvidenceRequired, match="before approval can be recommended"):
        wf.recommend(INV, "EX-003", Recommendation.APPROVE, "Looks genuine.")
    assert wf.get("EX-003").state is S.INVESTIGATING
    case = wf.recommend(INV, "EX-003", Recommendation.REJECT, "No support provided.")
    assert case.recommendation is Recommendation.REJECT


def test_human_cannot_approve_evidence_required_case_without_evidence(wf: CaseWorkflow) -> None:
    _investigating(wf)
    wf.recommend(INV, "EX-003", Recommendation.REJECT, "No support provided.")
    with pytest.raises(EvidenceRequired, match="cannot be approved"):
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Approve anyway.")
    assert wf.decide(HUMAN, "EX-003", Decision.REJECTED, "Agreed.").state is S.REJECTED


def test_evidence_not_required_for_non_configured_control(wf: CaseWorkflow) -> None:
    case = _investigating(wf, "EX-009", controls=("C03",))
    assert case.evidence_required is False
    case = wf.recommend(INV, "EX-009", Recommendation.APPROVE, "Mapping fixed upstream.")
    assert case.state is S.READY_FOR_HUMAN_REVIEW


def test_requested_evidence_becomes_required(wf: CaseWorkflow) -> None:
    _investigating(wf, "EX-009", controls=("C03",))
    wf.request_evidence(INV, "EX-009", "Need the mapping change ticket.")
    with pytest.raises(InvalidTransition):  # still AWAITING_EVIDENCE
        wf.recommend(INV, "EX-009", Recommendation.APPROVE, "x")
    wf.attach_evidence(HUMAN, "EX-009", "TICKET-1234")
    assert wf.recommend(INV, "EX-009", Recommendation.APPROVE, "Ticket attached.").state is (
        S.READY_FOR_HUMAN_REVIEW
    )


def test_missing_evidence_keeps_case_awaiting(wf: CaseWorkflow) -> None:
    _investigating(wf)
    wf.request_evidence(INV, "EX-003", "Need evidence.")
    wf.add_commentary(HUMAN, "EX-003", "Evidence to follow.")
    assert wf.get("EX-003").state is S.AWAITING_EVIDENCE


# --- input validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference", ["../secrets.txt", "C:\\evidence.pdf", "", "a" * 65, "-leading-dash", "x y"]
)
def test_invalid_evidence_reference_rejected(wf: CaseWorkflow, reference: str) -> None:
    _investigating(wf)
    wf.request_evidence(INV, "EX-003", "Need evidence.")
    with pytest.raises(InvalidInput, match="evidence reference"):
        wf.attach_evidence(HUMAN, "EX-003", reference)


def test_duplicate_evidence_reference_rejected(wf: CaseWorkflow) -> None:
    _ready(wf)
    wf.request_evidence(HUMAN, "EX-003", "One more document.")
    with pytest.raises(InvalidInput, match="already attached"):
        wf.attach_evidence(HUMAN, "EX-003", "EVD-2026-10-003")


@pytest.mark.parametrize("rationale", ["", "   ", "x" * 2001, "bad\x00byte"])
def test_decision_rationale_is_required_and_bounded(wf: CaseWorkflow, rationale: str) -> None:
    _ready(wf)
    with pytest.raises(InvalidInput, match="decision rationale"):
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, rationale)
    assert wf.get("EX-003").state is S.READY_FOR_HUMAN_REVIEW


def test_handoff_target_must_be_known_agent(wf: CaseWorkflow) -> None:
    _open(wf)
    with pytest.raises(InvalidInput, match="hand-off target"):
        wf.handoff(CA, "EX-003", "rogue-agent", "x")


@pytest.mark.parametrize("case_id", ["../EX-1", "EX 1", "", "x" * 65])
def test_case_id_must_be_safe_identifier(wf: CaseWorkflow, case_id: str) -> None:
    with pytest.raises(InvalidInput, match="case_id"):
        wf.open_case(CA, case_id, "FC-2026-10", ["C01"], "x")


def test_naive_clock_rejected(log: AuditLog, config: ControlConfig) -> None:
    wf = CaseWorkflow(log, config, clock=lambda: datetime(2026, 11, 2, 9, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        _open(wf)


# --- replay integrity -------------------------------------------------------------------


def _append_forged(log: AuditLog, event: AuditEvent) -> None:
    with log.locked():
        log.append(event)


def test_forged_agent_approval_in_log_is_rejected_on_replay(
    wf: CaseWorkflow, log: AuditLog
) -> None:
    _ready(wf)
    last = log.read()[-1]
    forged = replace(
        last,
        action=Action.DECIDE,
        actor_type=ActorType.AGENT,
        actor_name=INV.name,
        previous_state=S.READY_FOR_HUMAN_REVIEW,
        new_state=S.APPROVED,
        human_decision=Decision.APPROVED,
        rationale="Approved by agent.",
    )
    _append_forged(log, forged)  # valid hash chain, but breaks the workflow rules
    with pytest.raises(AuditIntegrityError, match=r"violates workflow rules: .*DECIDE"):
        wf.cases()
    with pytest.raises(AuditIntegrityError):
        wf.decide(HUMAN, "EX-003", Decision.APPROVED, "x")  # no action on an untrusted log


def test_forged_human_decision_on_non_decide_event_rejected(
    wf: CaseWorkflow, log: AuditLog
) -> None:
    _investigating(wf)
    last = log.read()[-1]
    forged = replace(
        last,
        action=Action.ADD_COMMENTARY,
        actor_type=ActorType.HUMAN,
        actor_name=HUMAN_ROLE,
        previous_state=S.INVESTIGATING,
        new_state=S.INVESTIGATING,
        human_decision=Decision.APPROVED,
        rationale="sneaky",
        details={},
    )
    _append_forged(log, forged)
    with pytest.raises(AuditIntegrityError, match="only a DECIDE event may carry"):
        wf.cases()


def test_access_denied_events_do_not_change_state_on_replay(
    wf: CaseWorkflow, log: AuditLog
) -> None:
    _ready(wf)
    with pytest.raises(PermissionDenied):
        wf.decide(INV, "EX-003", Decision.APPROVED, "x")
    assert wf.cases()["EX-003"].state is S.READY_FOR_HUMAN_REVIEW
    assert _actions(log)[-1] == "ACCESS_DENIED"


def test_prior_events_preserved_across_workflow(wf: CaseWorkflow, log: AuditLog) -> None:
    _investigating(wf)
    before = log.path.read_bytes()
    wf.request_evidence(INV, "EX-003", "Need evidence.")
    with pytest.raises(PermissionDenied):
        wf.decide(INV, "EX-003", Decision.APPROVED, "x")
    assert log.path.read_bytes().startswith(before)
    lines = log.path.read_text(encoding="ascii").splitlines()
    assert [json.loads(line)["action"] for line in lines] == [
        "OPEN_CASE",
        "HANDOFF",
        "REQUEST_EVIDENCE",
        "ACCESS_DENIED",
    ]
    assert lines[0] == event_to_json(log.read()[0])


# --- opening cases from a validated package --------------------------------------------


def test_open_cases_for_golden_package(wf: CaseWorkflow, config: ControlConfig) -> None:
    results = validate_package(
        load_package(REPO_ROOT / "data" / "synthetic_close_package.csv"), config
    )
    opened, already = wf.open_cases_for_package(CA, results)
    assert opened == [
        "EX-003",
        "EX-008",
        "EX-009",
        "EX-010",
        "EX-011",
        "EX-012",
        "EX-013",
        "EX-014",
        "EX-016",
        "EX-018",
        "EX-019",
    ]
    assert already == []
    cases = wf.cases()
    assert cases["EX-003"].control_ids == ("C01", "C05")
    assert cases["EX-003"].evidence_required is True
    assert cases["EX-010"].control_ids == ("C05", "C02")
    assert cases["EX-010"].evidence_required is False
    assert cases["EX-019"].control_ids == ("C07",)
    assert all(c.state is S.OPEN and c.owner == CA.name for c in cases.values())

    assert wf.open_cases_for_package(CA, results) == ([], opened)  # idempotent


def test_open_cases_uses_line_id_for_unsafe_record_id(
    wf: CaseWorkflow, config: ControlConfig, tmp_path: Path
) -> None:
    path = tmp_path / "pkg.csv"
    path.write_text(
        "record_id,submission_id,period,entity,cost_centre,account,actual,forecast,"
        "currency,mapping_status,commentary,submitted_by_role\n"
        "../../etc,FC-2026-10,2026-10,NLG-PL,CC-FLEET,6100-FUEL,200.00,100.00,"
        "EUR,VALID,note,Regional Controller\n",
        encoding="utf-8",
    )
    opened, _ = wf.open_cases_for_package(CA, validate_package(load_package(path), config))
    assert opened == ["LINE-2"]


# --- review fixes -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "smuggled",
    [
        {"human_decision": Decision.APPROVED},
        {"recommendation": Recommendation.APPROVE},
        {"evidence_references": ("EVD-FAKE",)},
        {"details": {"attempted_action": "NOT_AN_ACTION"}},
        {"details": {"attempted_action": "ACCESS_DENIED"}},
        {"details": {}},
        {"details": {"attempted_action": "DECIDE", "extra": "x"}},
    ],
)
def test_forged_access_denied_cannot_carry_decision_data(
    wf: CaseWorkflow, log: AuditLog, smuggled: dict[str, object]
) -> None:
    _open(wf)
    last = log.read()[-1]
    fields: dict[str, object] = {
        "action": Action.ACCESS_DENIED,
        "actor_type": ActorType.AGENT,
        "actor_name": INV.name,
        "previous_state": S.OPEN,
        "new_state": S.OPEN,
        "rationale": "denied",
        "details": {"attempted_action": "DECIDE"},
    }
    fields.update(smuggled)
    _append_forged(log, replace(last, **fields))  # type: ignore[arg-type]
    with pytest.raises(AuditIntegrityError, match="ACCESS_DENIED"):
        wf.cases()


def test_genuine_access_denied_on_decided_case_replays(wf: CaseWorkflow, log: AuditLog) -> None:
    _ready(wf)
    wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Approved.")
    with pytest.raises(PermissionDenied):
        wf.close(INV, "EX-003")
    denied = log.read()[-1]
    assert (denied.action, denied.new_state) == (Action.ACCESS_DENIED, S.APPROVED)
    assert wf.get("EX-003").state is S.APPROVED


def test_request_evidence_clears_stale_recommendation(wf: CaseWorkflow) -> None:
    _ready(wf)
    case = wf.request_evidence(HUMAN, "EX-003", "Attach the signed programme budget too.")
    assert case.state is S.AWAITING_EVIDENCE
    assert (case.recommendation, case.recommendation_rationale) == (None, None)
    assert wf.get("EX-003").recommendation is None


def test_open_cases_for_package_tolerates_concurrent_open(
    wf: CaseWorkflow, config: ControlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = validate_package(
        load_package(REPO_ROOT / "data" / "synthetic_close_package.csv"), config
    )
    real_cases = wf.cases
    calls = {"n": 0}

    def stale_snapshot() -> dict[str, Case]:
        calls["n"] += 1
        snapshot = real_cases()
        if calls["n"] == 1:  # another command opens EX-008 after our snapshot
            wf.open_case(CA, "EX-008", "FC-2026-10", ["C01"], "opened concurrently")
        return snapshot

    monkeypatch.setattr(wf, "cases", stale_snapshot)
    opened, already = wf.open_cases_for_package(CA, results)
    assert "EX-008" not in opened
    assert already == ["EX-008"]
    assert len(opened) == 10
    assert sorted(real_cases()) == sorted([*opened, "EX-008"])


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("exception-investigator", "may not use an agent's name"),
        ("control-analyst", "may not use an agent's name"),
        ("", "must be named"),
        ("   ", "must be named"),
        ("x" * 65, "at most 64 characters"),
        ("Finance\nReviewer", "control characters"),
    ],
)
def test_human_actor_name_is_validated(wf: CaseWorkflow, name: str, message: str) -> None:
    with pytest.raises(PermissionDenied, match=message):
        wf.open_case(Actor(ActorType.HUMAN, name), "EX-003", "FC-2026-10", ["C01"], "x")
    _open(wf)
    with pytest.raises(PermissionDenied, match=message):
        wf.add_commentary(Actor(ActorType.HUMAN, name), "EX-003", "Commentary.")
