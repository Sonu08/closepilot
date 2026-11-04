from __future__ import annotations

from pathlib import Path

import pytest

from closepilot.audit import AuditLog
from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.models import (
    CONTROL_ANALYST,
    EXCEPTION_INVESTIGATOR,
    HUMAN_ROLE,
    CaseState,
    Recommendation,
    RecordResult,
)
from closepilot.status import WAITING_ON, package_status, status_markdown
from closepilot.validation import validate_package
from closepilot.workflow import CaseWorkflow, InvalidInput

REPO_ROOT = Path(__file__).resolve().parent.parent
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


def test_every_state_has_a_next_step() -> None:
    assert set(WAITING_ON) == set(CaseState)


def test_golden_status_after_opening(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    status = package_status("FC-2026-10", results, wf.cases())
    assert (status["records"], status["cases"]) == (25, 11)
    assert {e["entity"]: (e["records"], e["cases"]) for e in status["entities"]} == {
        "NLG-DE": (9, 5),
        "NLG-PL": (8, 2),
        "NLG-US": (8, 4),
    }
    assert status["waiting_on"] == {CA.name: 11}
    assert all(r["next_step"] == "hand off to the investigator" for r in status["case_list"])


def test_waiting_on_follows_the_workflow(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    wf.handoff(CA, "EX-009", INV.name, "Mapping.")
    wf.handoff(CA, "EX-003", INV.name, "Variance.")
    wf.recommend(INV, "EX-009", Recommendation.REJECT, "Invalid mapping (C03).")
    wf.request_evidence(INV, "EX-003", "Programme approval memo.")
    rows = {r["case_id"]: r for r in package_status("FC-2026-10", results, wf.cases())["case_list"]}
    assert (rows["EX-009"]["state"], rows["EX-009"]["waiting_on"], rows["EX-009"]["next_step"]) == (
        "READY_FOR_HUMAN_REVIEW",
        HUMAN_ROLE,
        "decide",
    )
    assert rows["EX-009"]["recommendation"] == "REJECT"
    assert (rows["EX-003"]["state"], rows["EX-003"]["waiting_on"]) == (
        "AWAITING_EVIDENCE",
        HUMAN_ROLE,
    )
    assert rows["EX-012"]["waiting_on"] == CA.name


def test_status_ignores_other_submissions(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    status = package_status("FC-2099-01", results, wf.cases())
    assert (status["cases"], status["waiting_on"]) == (0, {})


def test_markdown_board(wf: CaseWorkflow, results: list[RecordResult]) -> None:
    md = status_markdown(package_status("FC-2026-10", results, wf.cases()))
    assert md.startswith("# Close status: `FC-2026-10`\n")
    assert "- `control-analyst`: 11" in md
    assert "| `NLG-DE` | 9 | 5 | OPEN 5 |" in md
    assert "Only the Finance Reviewer approves or rejects." in md


def test_markdown_neutralises_hostile_entity_text() -> None:
    status = {
        "submission_id": "FC-1",
        "records": 1,
        "cases": 0,
        "waiting_on": {},
        "entities": [
            {"entity": "X | `y` \n# Injected", "records": 1, "cases": 0, "cases_by_state": {}}
        ],
        "case_list": [],
    }
    md = status_markdown(status)
    assert "\n# Injected" not in md
    assert "| `X \\| 'y' # Injected` | 1 | 0 | - |" in md


def test_cases_cannot_be_handed_to_the_coordinator(wf: CaseWorkflow) -> None:
    with pytest.raises(InvalidInput, match="hand-off target must be one of"):
        wf.handoff(CA, "EX-003", "close-coordinator", "x")
