from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from closepilot.agent_cli import EXIT_INPUT_ERROR, EXIT_OK, EXIT_REFUSED
from closepilot.audit import AuditLog
from closepilot.cli import main
from closepilot.submissions import SubmissionError, parse_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
CA = "control-analyst"
INV = "exception-investigator"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for rel in (
        "config/controls.yaml",
        "config/submissions.yaml",
        "data/synthetic_close_package.csv",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, tmp_path / rel)
    return tmp_path


def _run(
    home: Path, agent: str, *args: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, Any, str]:
    code = main(["agent", "--as", agent, "--home", str(home), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out else None, captured.err


def _actions(home: Path) -> list[str]:
    return [e.action.value for e in AuditLog(home / "out" / "audit.jsonl").read()]


# --- submission registry ----------------------------------------------------------------


def test_registry_rejects_package_outside_home(tmp_path: Path) -> None:
    data = {"submissions": {"FC-1": {"package": "../outside.csv"}}}
    with pytest.raises(SubmissionError, match="inside the ClosePilot home"):
        parse_registry(data, tmp_path)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"submissions": []},
        {"submissions": {"../x": {"package": "a.csv"}}},
        {"submissions": {"FC-1": "a.csv"}},
    ],
)
def test_registry_rejects_malformed_data(tmp_path: Path, data: object) -> None:
    with pytest.raises(SubmissionError):
        parse_registry(data, tmp_path)


# --- read-only commands -----------------------------------------------------------------


def test_submissions_lists_registered_ids(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(home, CA, "submissions", capsys=capsys)[:2] == (
        EXIT_OK,
        {"submissions": ["FC-2026-10", "FC-2026-10-DE", "FC-2026-10-PL", "FC-2026-10-US"]},
    )


def test_validate_by_submission_id(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload, _ = _run(home, CA, "validate", "FC-2026-10", capsys=capsys)
    assert code == EXIT_OK
    assert payload["submission_id"] == "FC-2026-10"
    assert payload["summary"]["total_records"] == 25
    assert payload["summary"]["status_counts"] == {
        "PASS": 10,
        "WARNING": 3,
        "REVIEW_REQUIRED": 10,
        "ERROR": 2,
    }


@pytest.mark.parametrize(
    ("submission", "message"),
    [
        ("FC-2099-01", "unknown submission 'FC-2099-01' (registered: FC-2026-10, FC-2026-10-DE"),
        ("../data/synthetic_close_package.csv", "submission ID must be"),
        ("C:/Windows/win.ini", "submission ID must be"),
    ],
)
def test_validate_refuses_unknown_or_path_like_submission(
    home: Path, capsys: pytest.CaptureFixture[str], submission: str, message: str
) -> None:
    code, payload, err = _run(home, CA, "validate", submission, capsys=capsys)
    assert (code, payload) == (EXIT_INPUT_ERROR, None)
    assert message in err


def test_unknown_case_is_refused(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(home, INV, "case", "EX-404", capsys=capsys)
    assert code == EXIT_REFUSED
    assert "does not exist" in err


# --- workflow commands ------------------------------------------------------------------


def test_open_cases_is_idempotent(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload, _ = _run(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    assert code == EXIT_OK
    assert len(payload["opened"]) == 11
    assert "EX-003" in payload["opened"]
    _, again, _ = _run(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    assert (again["opened"], again["already_open"]) == ([], payload["opened"])


def test_analyst_to_investigator_flow(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    code, case, _ = _run(home, CA, "handoff", "EX-003", "--note", "C01 13.89%", capsys=capsys)
    assert (code, case["state"], case["owner"]) == (EXIT_OK, "INVESTIGATING", INV)

    # C01 needs human-attached evidence before approval can be recommended.
    code, _, err = _run(
        home, INV, "recommend", "EX-003", "APPROVE", "--rationale", "looks fine", capsys=capsys
    )
    assert code == EXIT_REFUSED
    assert "evidence reference" in err

    code, case, _ = _run(
        home, INV, "request-evidence", "EX-003", "--request", "Programme memo", capsys=capsys
    )
    assert (code, case["state"], case["evidence_requested"]) == (
        EXIT_OK,
        "AWAITING_EVIDENCE",
        True,
    )

    code, cases, _ = _run(home, INV, "cases", "--submission", "FC-2026-10", capsys=capsys)
    assert code == EXIT_OK
    assert len(cases["cases"]) == 11


def test_wrong_agent_is_refused_and_logged(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    code, _, err = _run(home, INV, "handoff", "EX-003", "--note", "x", capsys=capsys)
    assert code == EXIT_REFUSED
    assert "may not perform HANDOFF" in err
    assert _actions(home)[-1] == "ACCESS_DENIED"


@pytest.mark.parametrize("command", ["decide", "close", "attach-evidence", "add-commentary"])
def test_agents_have_no_human_commands(
    home: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent", "--as", INV, "--home", str(home), command, "EX-003"])
    assert exc.value.code == 2
    assert not (home / "out" / "audit.jsonl").exists()


def test_unknown_agent_name_is_rejected(home: Path) -> None:
    with pytest.raises(SystemExit):
        main(["agent", "--as", "Finance Reviewer", "--home", str(home), "cases"])


# --- close room v2: entity submissions, status board, coordinator --------------------

CO = "close-coordinator"


@pytest.mark.parametrize(
    ("submission", "records", "counts", "cases"),
    [
        (
            "FC-2026-10-DE",
            9,
            {"PASS": 3, "WARNING": 1, "REVIEW_REQUIRED": 4, "ERROR": 1},
            ["EX-003", "EX-010", "EX-012", "EX-016", "EX-019"],
        ),
        (
            "FC-2026-10-PL",
            8,
            {"PASS": 5, "WARNING": 1, "REVIEW_REQUIRED": 2, "ERROR": 0},
            ["EX-009", "EX-013"],
        ),
        (
            "FC-2026-10-US",
            8,
            {"PASS": 2, "WARNING": 1, "REVIEW_REQUIRED": 4, "ERROR": 1},
            ["EX-008", "EX-011", "EX-014", "EX-018"],
        ),
    ],
)
def test_entity_submission_validates_and_opens_only_its_entity(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    submission: str,
    records: int,
    counts: dict[str, int],
    cases: list[str],
) -> None:
    code, payload, _ = _run(home, CA, "validate", submission, capsys=capsys)
    assert code == EXIT_OK
    assert payload["submission_id"] == submission
    assert payload["summary"]["total_records"] == records
    assert payload["summary"]["status_counts"] == counts
    code, opened, _ = _run(home, CA, "open-cases", submission, capsys=capsys)
    assert (code, opened["opened"]) == (EXIT_OK, cases)


def test_entity_submissions_together_open_the_same_cases_as_the_full_package(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for entity in ("DE", "PL", "US"):
        _run(home, CA, "open-cases", f"FC-2026-10-{entity}", capsys=capsys)
    _, again, _ = _run(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    assert again["opened"] == []
    assert len(again["already_open"]) == 11


def test_entity_figures_match_the_full_package(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def ex003(report: Any) -> Any:
        return next(r for r in report["results"] if r["record_id"] == "EX-003")

    _, full, _ = _run(home, CA, "validate", "FC-2026-10", capsys=capsys)
    _, de, _ = _run(home, CA, "validate", "FC-2026-10-DE", capsys=capsys)
    assert ex003(de) == ex003(full)


def test_status_board_for_the_coordinator(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(home, CA, "open-cases", "FC-2026-10-PL", capsys=capsys)
    code, status, _ = _run(home, CO, "status", "FC-2026-10", capsys=capsys)
    assert code == EXIT_OK
    assert (status["submission_id"], status["records"], status["cases"]) == ("FC-2026-10", 25, 2)
    assert status["waiting_on"] == {CA: 2}
    assert status["markdown"].startswith("# Close status: `FC-2026-10`")
    code, de, _ = _run(home, CO, "status", "FC-2026-10-DE", capsys=capsys)
    assert (code, de["records"], de["cases"]) == (EXIT_OK, 9, 0)


@pytest.mark.parametrize(
    "args",
    [
        ("open-cases", "FC-2026-10-PL"),
        ("handoff", "EX-009", "--note", "x"),
        ("request-evidence", "EX-009", "--request", "x"),
        ("recommend", "EX-009", "REJECT", "--rationale", "x"),
    ],
)
def test_coordinator_can_change_nothing(
    home: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    if args[0] != "open-cases":  # leave the cases for the coordinator to try to open
        _run(home, CA, "open-cases", "FC-2026-10-PL", capsys=capsys)
    if args[0] in ("request-evidence", "recommend"):
        _run(home, CA, "handoff", "EX-009", "--note", "Mapping.", capsys=capsys)
    log = home / "out" / "audit.jsonl"
    before = len(_actions(home)) if log.exists() else 0
    code, _, err = _run(home, CO, *args, capsys=capsys)
    assert code == EXIT_REFUSED
    assert "close-coordinator" in err
    actions = _actions(home)
    assert len(actions) - before <= 1
    assert set(actions[before:]) <= {"ACCESS_DENIED"}


def test_cases_cannot_be_handed_off_to_the_coordinator(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(home, CA, "open-cases", "FC-2026-10-PL", capsys=capsys)
    with pytest.raises(SystemExit):
        main(
            [
                "agent",
                "--as",
                CA,
                "--home",
                str(home),
                "handoff",
                "EX-009",
                "--to",
                CO,
                "--note",
                "x",
            ]
        )


def test_registry_rejects_an_unsafe_entity(tmp_path: Path) -> None:
    data = {"submissions": {"FC-1": {"package": "a.csv", "entity": "../x"}}}
    with pytest.raises(SubmissionError, match="invalid entity"):
        parse_registry(data, tmp_path)


def test_entity_status_board_is_labelled_with_the_entity_submission(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(home, CA, "open-cases", "FC-2026-10-PL", capsys=capsys)
    _, pl, _ = _run(home, CO, "status", "FC-2026-10-PL", capsys=capsys)
    assert (pl["submission_id"], pl["cases"]) == ("FC-2026-10-PL", 2)
    assert pl["markdown"].startswith("# Close status: `FC-2026-10-PL`")


def test_cases_for_an_entity_submission_are_that_entitys_cases(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Found in the live v2 run: every case carries the package's submission_id, so the
    entity submission must select cases by the records they were opened from."""
    _run(home, CA, "open-cases", "FC-2026-10-DE", capsys=capsys)
    _run(home, CA, "open-cases", "FC-2026-10-PL", capsys=capsys)
    _, de, _ = _run(home, CA, "cases", "--submission", "FC-2026-10-DE", capsys=capsys)
    assert [c["case_id"] for c in de["cases"]] == ["EX-003", "EX-010", "EX-012", "EX-016", "EX-019"]
    _, full, _ = _run(home, CA, "cases", "--submission", "FC-2026-10", capsys=capsys)
    assert len(full["cases"]) == 7
    _, everything, _ = _run(home, CA, "cases", capsys=capsys)
    assert len(everything["cases"]) == 7
