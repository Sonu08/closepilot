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
        {"submissions": ["FC-2026-10"]},
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
        ("FC-2099-01", "unknown submission 'FC-2099-01' (registered: FC-2026-10)"),
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
