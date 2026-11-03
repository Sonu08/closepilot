"""Regression tests for the review of the reviewer gate (sealed human events and friends)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from closepilot.audit import AuditLog, event_to_json, last_event_hash
from closepilot.cli import main
from closepilot.config import ControlConfig
from closepilot.ingestion import load_package
from closepilot.investigation import investigate
from closepilot.models import (
    CONTROL_ANALYST,
    EXCEPTION_INVESTIGATOR,
    Action,
    ActorType,
    CaseState,
    Decision,
    Recommendation,
)
from closepilot.report import decision_record, decision_record_markdown
from closepilot.reviewer import ReviewerError, init_reviewer, verify_reviewer
from closepilot.reviewer_cli import EXIT_OK, EXIT_REFUSED, EXIT_REVIEWER_CHECK_FAILED
from closepilot.validation import validate_package
from closepilot.workflow import CaseWorkflow

EXIT_AUDIT_ERROR = 4
REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "correct horse battery staple"  # noqa: S105 - synthetic test secret
ATTACKER_SECRET = "attacker chosen secret"  # noqa: S105 - synthetic test secret
CA, INV = "control-analyst", "exception-investigator"


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


def _call(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


def _agent(home: Path, who: str, *args: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _call(["agent", "--as", who, "--home", str(home), *args], capsys)
    assert code == EXIT_OK, err


def _reviewer(home: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    return _call(["reviewer", "--home", str(home), *args], capsys)


def _log(home: Path) -> AuditLog:
    return AuditLog(home / "out" / "audit.jsonl")


def _ex009_ready_with_human_comment(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """EX-009 (C03, data quality) READY with REJECT; the real reviewer has commented once."""
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    assert _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)[0] == EXIT_OK
    _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    _agent(home, CA, "handoff", "EX-009", "--note", "Mapping.", capsys=capsys)
    code, _, err = _reviewer(home, "comment", "EX-009", "--text", "Looking.", capsys=capsys)
    assert code == EXIT_OK, err
    _agent(home, INV, "recommend", "EX-009", "REJECT", "--rationale", "Bad mapping.", capsys=capsys)


def _append_forged_decision(home: Path, case_id: str, decision: Decision) -> None:
    """What any process running as the same OS user can do: append one line with a
    correct hash chain, claiming to be the human."""
    log = _log(home)
    events = log.read()
    template = [e for e in events if e.case_id == case_id][-1]
    forged = replace(
        template,
        event_id=f"EVT-{len(events) + 1:06d}",
        actor_type=ActorType.HUMAN,
        actor_name="Alex Reviewer",
        action=Action.DECIDE,
        previous_state=CaseState.READY_FOR_HUMAN_REVIEW,
        new_state=CaseState.APPROVED if decision is Decision.APPROVED else CaseState.REJECTED,
        human_decision=decision,
        rationale="forged",
        details={},
        prev_event_hash=last_event_hash(events),
    )
    with log.path.open("a", encoding="ascii", newline="\n") as handle:
        handle.write(event_to_json(forged) + "\n")


# --- sealing ----------------------------------------------------------------------------


def test_human_events_are_sealed_and_agent_events_are_not(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ex009_ready_with_human_comment(home, capsys, monkeypatch)
    code, _, err = _reviewer(
        home, "decide", "EX-009", "reject", "--rationale", "Resubmit.", capsys=capsys
    )
    assert code == EXIT_OK, err
    for event in _log(home).read():
        sealed = "seal" in event.details
        human = event.actor_type is ActorType.HUMAN and event.action is not Action.ACCESS_DENIED
        assert sealed == human, (event.event_id, event.action, event.actor_type)


def test_reinitialised_reviewer_cannot_decide(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ex009_ready_with_human_comment(home, capsys, monkeypatch)
    # Replace the reviewer file and set up a new reviewer with a secret of one's own:
    # through the CLI the log refuses a second enrolment ...
    path = home / ".closepilot" / "reviewer.json"
    path.rename(home / ".closepilot" / "old.json")
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", ATTACKER_SECRET)
    assert _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)[0] == EXIT_REFUSED
    # ... and a reviewer file written directly does not match the enrolled reviewer.
    init_reviewer(path, "Alex Reviewer", ATTACKER_SECRET)
    before = len(_log(home).read())
    code, _, err = _reviewer(home, "decide", "EX-009", "reject", "--rationale", "x", capsys=capsys)
    assert code == EXIT_AUDIT_ERROR
    assert "enrols a different reviewer" in err
    assert len(_log(home).read()) == before


@pytest.mark.parametrize("with_prior_human_event", [True, False])
def test_forged_human_decision_line_is_refused(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    with_prior_human_event: bool,
) -> None:
    if with_prior_human_event:
        _ex009_ready_with_human_comment(home, capsys, monkeypatch)
    else:
        monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
        _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)
        _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
        _agent(home, CA, "handoff", "EX-009", "--note", "Mapping.", capsys=capsys)
        _agent(home, INV, "recommend", "EX-009", "REJECT", "--rationale", "x", capsys=capsys)
    _append_forged_decision(home, "EX-009", Decision.APPROVED)
    for argv in (
        ["close", "EX-009"],
        ["comment", "EX-003", "--text", "hello"],
        ["export", "EX-009"],
        ["summary", "FC-2026-10"],
    ):
        code, payload, err = _reviewer(home, *argv, capsys=capsys)
        assert (code, payload) == (EXIT_AUDIT_ERROR, None), argv
        assert "not sealed by the current reviewer" in err
    assert not (home / "out" / "decisions").exists()


def test_export_and_summary_need_the_reviewer_secret(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ex009_ready_with_human_comment(home, capsys, monkeypatch)
    monkeypatch.delenv("CLOSEPILOT_REVIEWER_SECRET")
    for argv in (["export", "EX-009"], ["summary", "FC-2026-10"]):
        code, _, err = _reviewer(home, *argv, capsys=capsys)
        assert code == EXIT_REVIEWER_CHECK_FAILED, argv
        assert "set the reviewer secret" in err


# --- reviewer file parameters -----------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        {"n": 2**30},
        {"n": 2},
        {"dklen": 0},
        {"r": 0},
        {"salt": "00"},
        {"hash": "00"},
    ],
)
def test_tampered_reviewer_parameters_are_refused_not_crashing(
    tmp_path: Path, tamper: dict[str, object]
) -> None:
    path = tmp_path / "reviewer.json"
    init_reviewer(path, "Alex Reviewer", SECRET)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(tamper)
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ReviewerError, match="unreadable or malformed"):
        verify_reviewer(path, SECRET)


# --- Markdown export --------------------------------------------------------------------


def test_markdown_export_neutralises_untrusted_inline_text(
    tmp_path: Path, config: ControlConfig
) -> None:
    wf = CaseWorkflow(AuditLog(tmp_path / "audit.jsonl"), config)
    hostile = "FC `x` **bold** [click](https://example.invalid)\n# Injected heading"
    wf.open_case(CONTROL_ANALYST, "EX-1", hostile, ["C01"], "C01 REVIEW_REQUIRED: 13.89%")
    wf.handoff(CONTROL_ANALYST, "EX-1", EXCEPTION_INVESTIGATOR.name, "Investigate.")
    wf.recommend(EXCEPTION_INVESTIGATOR, "EX-1", Recommendation.REJECT, "x")
    markdown = decision_record_markdown(
        decision_record("EX-1", wf.log.read(), log_location="out/audit.jsonl")
    )
    assert "\n# Injected heading" not in markdown
    submission_line = next(line for line in markdown.splitlines() if "**Submission:**" in line)
    # The whole value sits inside one code span (where Markdown renders nothing), with
    # its own backticks replaced so it cannot close the span early.
    assert submission_line == (
        "- **Submission:** `FC 'x' **bold** [click](https://example.invalid) # Injected heading`"
    )


# --- LINE-<n> case IDs ------------------------------------------------------------------


def test_record_id_that_looks_like_a_line_case_id_maps_to_itself(
    tmp_path: Path, config: ControlConfig
) -> None:
    source = (REPO_ROOT / "data" / "synthetic_close_package.csv").read_text(encoding="utf-8")
    # Line 2 holds EX-001; rename it LINE-3. Line 3 (EX-002) is a different record.
    path = tmp_path / "pkg.csv"
    path.write_text(source.replace("\nEX-001,", "\nLINE-3,", 1), encoding="utf-8")
    results = validate_package(load_package(path), config)
    wf = CaseWorkflow(AuditLog(tmp_path / "audit.jsonl"), config)
    case = wf.open_case(CONTROL_ANALYST, "LINE-3", "FC-2026-10", ["C01"], "x")
    [record] = investigate(case, results).records
    assert (record.record.record_id, record.record.line_number) == ("LINE-3", 2)
