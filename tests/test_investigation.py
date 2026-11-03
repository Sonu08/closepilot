from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from closepilot.audit import AuditLog
from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.investigation import (
    Classification,
    InvestigationError,
    NextStep,
    investigate,
    investigation_to_dict,
)
from closepilot.models import (
    CONTROL_ANALYST,
    EXCEPTION_INVESTIGATOR,
    HUMAN_ROLE,
    Actor,
    ActorType,
    Decision,
    Recommendation,
    RecordResult,
)
from closepilot.validation import validate_package
from closepilot.workflow import CaseWorkflow

REPO_ROOT = Path(__file__).resolve().parent.parent
HUMAN = Actor(ActorType.HUMAN, HUMAN_ROLE)
CA, INV = CONTROL_ANALYST, EXCEPTION_INVESTIGATOR


@pytest.fixture
def results(config: ControlConfig) -> list[RecordResult]:
    return validate_package(
        load_package(REPO_ROOT / "data" / "synthetic_close_package.csv"), config
    )


@pytest.fixture
def wf(tmp_path: Path, config: ControlConfig, results: list[RecordResult]) -> CaseWorkflow:
    workflow = CaseWorkflow(AuditLog(tmp_path / "audit.jsonl"), config)
    workflow.open_cases_for_package(CA, results)
    return workflow


def _investigating(wf: CaseWorkflow, case_id: str) -> None:
    wf.handoff(CA, case_id, INV.name, "Please investigate.")


def test_golden_ex003_is_genuine_variance_needing_commentary_and_evidence(
    wf: CaseWorkflow, results: list[RecordResult]
) -> None:
    _investigating(wf, "EX-003")
    inv = investigate(wf.get("EX-003"), results)
    assert inv.classification is Classification.VARIANCE
    assert inv.next_step is NextStep.REQUEST_EVIDENCE
    assert inv.missing == ("commentary", "evidence")
    assert inv.mapping_valid is True
    assert inv.duplicate_rows == 1
    assert inv.currency_and_period_valid is True
    assert inv.integrity_errors == ()
    assert (inv.source_commentary, inv.commentary_required, inv.commentary_provided) == (
        "",
        True,
        False,
    )
    assert inv.evidence_required is True
    conc = inv.concentration
    assert conc is not None
    assert (conc.entity, conc.cost_centre, conc.currency, conc.records) == (
        "NLG-DE",
        "CC-EVENTS",
        "EUR",
        2,
    )
    assert conc.group_variance == Decimal("13700.00")  # EX-003 13200 + EX-023 500
    assert conc.record_variance == Decimal("13200.00")
    assert conc.share_pct == Decimal("96.35")
    assert "no data-quality finding: treated as a possible genuine business variance" in (
        inv.reasons
    )


def test_golden_ex003_full_path_to_recommend_approve(
    wf: CaseWorkflow, results: list[RecordResult]
) -> None:
    _investigating(wf, "EX-003")
    wf.request_evidence(INV, "EX-003", "Commentary and supporting evidence required.")
    assert investigate(wf.get("EX-003"), results).next_step is NextStep.WAIT_FOR_REVIEWER

    wf.add_commentary(HUMAN, "EX-003", "Approved customer-event programme.")
    wf.attach_evidence(HUMAN, "EX-003", "EVD-2026-10-003")
    inv = investigate(wf.get("EX-003"), results)
    assert inv.next_step is NextStep.RECOMMEND_APPROVE
    assert inv.missing == ()
    assert inv.commentary_provided is True
    assert inv.evidence_attached == ("EVD-2026-10-003",)

    wf.recommend(INV, "EX-003", Recommendation.APPROVE, "Genuine variance; evidence attached.")
    assert investigate(wf.get("EX-003"), results).next_step is NextStep.WAIT_FOR_DECISION
    wf.decide(HUMAN, "EX-003", Decision.APPROVED, "Reviewed the synthetic evidence.")
    assert investigate(wf.get("EX-003"), results).next_step is NextStep.NONE


def test_ex010_missing_commentary_is_requested_not_rejected(
    wf: CaseWorkflow, results: list[RecordResult]
) -> None:
    _investigating(wf, "EX-010")
    inv = investigate(wf.get("EX-010"), results)
    assert inv.classification is Classification.VARIANCE
    assert inv.next_step is NextStep.REQUEST_COMMENTARY
    assert inv.missing == ("commentary",)
    assert inv.evidence_required is False
    assert inv.concentration is not None
    assert inv.concentration.share_pct == Decimal("42.86")  # 1500 of EX-002 2000 + 1500

    wf.add_commentary(HUMAN, "EX-010", "Utility tariff change from October.")
    assert investigate(wf.get("EX-010"), results).next_step is NextStep.RECOMMEND_APPROVE


@pytest.mark.parametrize(
    ("case_id", "reason"),
    [
        ("EX-009", "master-data mapping is invalid (C03)"),
        ("EX-011", "record_id occurs 2 times in the package (C04)"),
        ("EX-012", "currency or reporting period is not allowed (C06)"),
        ("EX-013", "currency or reporting period is not allowed (C06)"),
        ("EX-018", "a required field is missing or unusable (C07)"),
        ("EX-019", "a required field is missing or unusable (C07)"),
    ],
)
def test_data_quality_cases_suggest_reject(
    wf: CaseWorkflow, results: list[RecordResult], case_id: str, reason: str
) -> None:
    _investigating(wf, case_id)
    inv = investigate(wf.get(case_id), results)
    assert inv.classification is Classification.DATA_QUALITY
    assert inv.next_step is NextStep.RECOMMEND_REJECT
    assert reason in inv.reasons


def test_duplicate_and_integrity_facts(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    ex011 = investigate(wf.get("EX-011"), results)
    assert ex011.duplicate_rows == 2
    assert [r.record.line_number for r in ex011.records] == [12, 13]
    ex019 = investigate(wf.get("EX-019"), results)
    assert ex019.integrity_errors == ("actual: malformed numeric value '1O5000.00'",)
    assert ex019.concentration is None  # no usable variance
    ex009 = investigate(wf.get("EX-009"), results)
    assert ex009.mapping_valid is False


@pytest.mark.parametrize("case_id", ["EX-008", "EX-014", "EX-016"])
def test_other_material_cases_need_only_evidence(
    wf: CaseWorkflow, results: list[RecordResult], case_id: str
) -> None:
    _investigating(wf, case_id)
    inv = investigate(wf.get(case_id), results)
    assert inv.classification is Classification.VARIANCE
    assert (inv.next_step, inv.missing) == (NextStep.REQUEST_EVIDENCE, ("evidence",))
    assert inv.commentary_provided is True


def test_open_case_suggests_handoff(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    assert investigate(wf.get("EX-003"), results).next_step is NextStep.HANDOFF


def test_case_without_records_is_an_error(wf: CaseWorkflow, config: ControlConfig) -> None:
    with pytest.raises(InvestigationError, match="no record for case 'EX-003'"):
        investigate(wf.get("EX-003"), [])


def test_dict_form_is_json_ready(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    _investigating(wf, "EX-003")
    data = investigation_to_dict(investigate(wf.get("EX-003"), results))
    assert data["next_step"] == "REQUEST_EVIDENCE"
    assert data["facts"]["evidence_note"].startswith("ClosePilot records evidence references")
    assert data["records"][0]["variance_pct"] == "13.89"
    assert data["cost_centre_concentration"]["record_share_pct"] == "96.35"
