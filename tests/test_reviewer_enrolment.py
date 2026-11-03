"""The reviewer is enrolled once, in the hash-chained audit log, at ``reviewer init``."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from closepilot.audit import AuditIntegrityError, AuditLog, event_to_json, last_event_hash
from closepilot.cli import main
from closepilot.models import REVIEWER_RECORD, Action, ActorType
from closepilot.reviewer import init_reviewer, verify_reviewer
from closepilot.reviewer_cli import EXIT_OK, EXIT_REFUSED
from closepilot.workflow import replay

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


def _reviewer(home: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    return _call(["reviewer", "--home", str(home), *args], capsys)


def _agent(home: Path, who: str, *args: str, capsys: pytest.CaptureFixture[str]) -> int:
    return _call(["agent", "--as", who, "--home", str(home), *args], capsys)[0]


def _log(home: Path) -> AuditLog:
    return AuditLog(home / "out" / "audit.jsonl")


def _reviewer_file(home: Path) -> Path:
    return home / ".closepilot" / "reviewer.json"


def _init(home: Path, secret: str, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> int:
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", secret)
    return _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)[0]


def _ready_ex009(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys) == EXIT_OK
    assert _agent(home, CA, "handoff", "EX-009", "--note", "Mapping.", capsys=capsys) == EXIT_OK
    assert (
        _agent(home, INV, "recommend", "EX-009", "REJECT", "--rationale", "x", capsys=capsys)
        == EXIT_OK
    )


def test_init_enrols_the_reviewer_once_in_the_log(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(home, SECRET, monkeypatch, capsys) == EXIT_OK
    [event] = _log(home).read()
    assert (event.action, event.case_id, event.actor_type, event.actor_name) == (
        Action.ENROL_REVIEWER,
        REVIEWER_RECORD,
        ActorType.HUMAN,
        "Alex Reviewer",
    )
    assert (event.previous_state, event.new_state) == (None, None)
    assert set(event.details) == {"fingerprint", "seal"}
    reviewer = verify_reviewer(_reviewer_file(home), SECRET)
    assert event.details["fingerprint"] == reviewer.fingerprint
    reviewer.verify_log(_log(home).read())
    # The enrolment is not a case.
    assert replay(_log(home).read()) == {}


def test_second_init_is_refused_and_leaves_no_file(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(home, SECRET, monkeypatch, capsys) == EXIT_OK
    _reviewer_file(home).rename(home / ".closepilot" / "old.json")
    before = _log(home).path.read_bytes()
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", ATTACKER_SECRET)
    code, _, err = _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)
    assert code == EXIT_REFUSED
    assert "already enrolled" in err
    assert not _reviewer_file(home).exists()
    assert _log(home).path.read_bytes() == before


def test_impostor_who_enrols_first_blocks_the_real_init(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrolling before the human is not prevented, but it is visible at once: the real
    reviewer's own init is refused, so they act on nothing."""
    assert _init(home, ATTACKER_SECRET, monkeypatch, capsys) == EXIT_OK
    _reviewer_file(home).unlink()
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    code, _, err = _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)
    assert (code, "already enrolled" in err) == (EXIT_REFUSED, True)


def test_replaced_reviewer_file_before_first_action_cannot_decide(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap the seals alone left open: the file is replaced before any sealed action."""
    assert _init(home, SECRET, monkeypatch, capsys) == EXIT_OK
    _ready_ex009(home, capsys)
    _reviewer_file(home).unlink()
    init_reviewer(_reviewer_file(home), "Alex Reviewer", ATTACKER_SECRET)  # bypasses enrolment
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", ATTACKER_SECRET)
    before = len(_log(home).read())
    code, _, err = _reviewer(home, "decide", "EX-009", "reject", "--rationale", "x", capsys=capsys)
    assert code == EXIT_AUDIT_ERROR
    assert "enrols a different reviewer" in err
    assert len(_log(home).read()) == before


def test_reviewer_commands_need_an_enrolment_in_the_log(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    init_reviewer(_reviewer_file(home), "Alex Reviewer", SECRET)  # file only, no enrolment
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    _ready_ex009(home, capsys)
    code, _, err = _reviewer(home, "summary", "FC-2026-10", capsys=capsys)
    assert code == EXIT_AUDIT_ERROR
    assert "no reviewer is enrolled" in err


@pytest.mark.parametrize(
    "change",
    [
        {},  # a second, otherwise well-formed enrolment
        {"actor_type": ActorType.AGENT, "actor_name": INV},
        {"case_id": "EX-009"},
        {"details": {"fingerprint": "not-hex"}},
    ],
)
def test_forged_enrolment_lines_break_replay(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    assert _init(home, SECRET, monkeypatch, capsys) == EXIT_OK
    log = _log(home)
    events = log.read()
    forged = replace(
        events[0],
        event_id=f"EVT-{len(events) + 1:06d}",
        prev_event_hash=last_event_hash(events),
        **change,
    )
    with log.path.open("a", encoding="ascii", newline="\n") as handle:
        handle.write(event_to_json(forged) + "\n")
    with pytest.raises(AuditIntegrityError):
        replay(log.read())
    # Every interface fails closed, agents included.
    assert _agent(home, INV, "cases", capsys=capsys) == EXIT_AUDIT_ERROR


def test_enrolment_is_not_a_case_action(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _init(home, SECRET, monkeypatch, capsys) == EXIT_OK
    code, _, err = _reviewer(home, "case", REVIEWER_RECORD, capsys=capsys)
    assert code == EXIT_REFUSED
    assert "does not exist" in err
