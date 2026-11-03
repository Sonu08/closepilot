from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from closepilot.audit import AuditLog
from closepilot.cli import main
from closepilot.models import Action, ActorType
from closepilot.reviewer import ReviewerError, init_reviewer, verify_reviewer
from closepilot.reviewer_cli import EXIT_OK, EXIT_REFUSED, EXIT_REVIEWER_CHECK_FAILED

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "correct horse battery staple"  # noqa: S105 - synthetic test secret
CA, INV = "control-analyst", "exception-investigator"


# --- reviewer secret --------------------------------------------------------------------


def test_init_then_verify(tmp_path: Path) -> None:
    path = tmp_path / ".closepilot" / "reviewer.json"
    assert init_reviewer(path, "Alex Reviewer", SECRET).name == "Alex Reviewer"
    stored = path.read_text(encoding="utf-8")
    assert SECRET not in stored
    assert json.loads(stored)["kdf"] == "scrypt"
    assert verify_reviewer(path, SECRET).name == "Alex Reviewer"


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        (None, "set the reviewer secret"),
        ("", "set the reviewer secret"),
        ("short", "must be 12-1024 characters"),
        ("correct horse battery stapler", "does not match"),
    ],
)
def test_verify_rejects_bad_secret(tmp_path: Path, secret: str | None, message: str) -> None:
    path = tmp_path / "reviewer.json"
    init_reviewer(path, "Alex Reviewer", SECRET)
    with pytest.raises(ReviewerError, match=message):
        verify_reviewer(path, secret)


def test_init_refuses_overwrite_weak_secret_and_agent_name(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.json"
    with pytest.raises(ReviewerError, match="12-1024 characters"):
        init_reviewer(path, "Alex Reviewer", "short")
    with pytest.raises(ReviewerError, match="agent's name"):
        init_reviewer(path, "exception-investigator", SECRET)
    init_reviewer(path, "Alex Reviewer", SECRET)
    with pytest.raises(ReviewerError, match="already set up"):
        init_reviewer(path, "Someone Else", SECRET)


def test_verify_without_setup_or_with_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.json"
    with pytest.raises(ReviewerError, match="no reviewer is set up"):
        verify_reviewer(path, SECRET)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ReviewerError, match="unreadable or malformed"):
        verify_reviewer(path, SECRET)


# --- end-to-end golden demo through the CLI --------------------------------------------


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


def _agent(home: Path, who: str, *args: str, capsys: pytest.CaptureFixture[str]) -> Any:
    code, payload, err = _call(["agent", "--as", who, "--home", str(home), *args], capsys)
    assert code == EXIT_OK, err
    return payload


def _reviewer(home: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    return _call(["reviewer", "--home", str(home), *args], capsys)


def _events(home: Path) -> list[Any]:
    return AuditLog(home / "out" / "audit.jsonl").read()


def test_golden_demo_end_to_end(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    code, payload, _ = _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)
    assert (code, payload["reviewer"]) == (EXIT_OK, "Alex Reviewer")

    # Scenes 1-3: the Control Analyst validates, opens cases and hands off EX-003.
    summary = _agent(home, CA, "validate", "FC-2026-10", capsys=capsys)["summary"]
    assert summary["status_counts"] == {"PASS": 10, "WARNING": 3, "REVIEW_REQUIRED": 10, "ERROR": 2}
    assert "EX-003" in _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys)["opened"]
    _agent(home, CA, "handoff", "EX-003", "--note", "Material variance.", capsys=capsys)

    # Scene 4: deterministic investigation.
    inv = _agent(home, INV, "investigate", "EX-003", capsys=capsys)
    assert (inv["next_step"], inv["missing"]) == ("REQUEST_EVIDENCE", ["commentary", "evidence"])
    _agent(
        home,
        INV,
        "request-evidence",
        "EX-003",
        "--request",
        "Commentary and evidence.",
        capsys=capsys,
    )

    # Scenes 5-6: the human adds commentary and attaches a synthetic evidence reference.
    code, case, _ = _reviewer(
        home,
        "comment",
        "EX-003",
        "--text",
        "Approved customer-event programme. Keep pending until evidence.",
        capsys=capsys,
    )
    assert (code, case["state"]) == (EXIT_OK, "AWAITING_EVIDENCE")
    code, case, _ = _reviewer(
        home,
        "attach-evidence",
        "EX-003",
        "EVD-2026-10-003",
        "--note",
        "Synthetic programme approval memo.",
        capsys=capsys,
    )
    assert (code, case["state"]) == (EXIT_OK, "INVESTIGATING")

    # Scene 7: recommendation only once the evidence condition holds.
    assert _agent(home, INV, "investigate", "EX-003", capsys=capsys)["next_step"] == (
        "RECOMMEND_APPROVE"
    )
    _agent(
        home,
        INV,
        "recommend",
        "EX-003",
        "APPROVE",
        "--rationale",
        "Genuine variance in one cost centre; commentary and evidence reference attached.",
        capsys=capsys,
    )

    # Agents have no decide command at all.
    with pytest.raises(SystemExit):
        main(["agent", "--as", INV, "--home", str(home), "decide", "EX-003", "approve"])
    capsys.readouterr()

    # An unverified attempt is refused and recorded; the case is unchanged.
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", "not the right secret")
    code, payload, err = _reviewer(
        home, "decide", "EX-003", "approve", "--rationale", "x", capsys=capsys
    )
    assert (code, payload) == (EXIT_REVIEWER_CHECK_FAILED, None)
    assert "reviewer check failed: the reviewer secret does not match" in err
    denied = _events(home)[-1]
    assert (denied.action, denied.actor_type, denied.actor_name) == (
        Action.ACCESS_DENIED,
        ActorType.UNVERIFIED,
        "unverified-reviewer",
    )
    assert denied.details == {"attempted_action": "DECIDE"}
    monkeypatch.delenv("CLOSEPILOT_REVIEWER_SECRET")
    code, _, err = _reviewer(home, "decide", "EX-003", "approve", "--rationale", "x", capsys=capsys)
    assert code == EXIT_REVIEWER_CHECK_FAILED
    assert "set the reviewer secret" in err

    # Scene 8: the human decides.
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    code, case, _ = _reviewer(
        home,
        "decide",
        "EX-003",
        "approve",
        "--rationale",
        "I reviewed the synthetic evidence. Approve EX-003.",
        capsys=capsys,
    )
    assert (code, case["state"], case["decided_by"]) == (EXIT_OK, "APPROVED", "Alex Reviewer")
    code, case, _ = _reviewer(home, "close", "EX-003", capsys=capsys)
    assert (code, case["state"]) == (EXIT_OK, "CLOSED")

    # Scene 9: the decision record.
    code, exported, _ = _reviewer(home, "export", "EX-003", capsys=capsys)
    assert exported == {
        "case_id": "EX-003",
        "final_state": "CLOSED",
        "json": "out/decisions/EX-003.json",
        "markdown": "out/decisions/EX-003.md",
    }
    record = json.loads((home / "out/decisions/EX-003.json").read_text(encoding="utf-8"))
    assert record["human_decision"]["decision"] == "APPROVED"
    assert record["human_decision"]["decided_by"] == "Alex Reviewer"
    assert record["human_decision"]["actor_type"] == "HUMAN"
    assert record["human_decision"]["recommendation_at_decision"] == "APPROVE"
    assert record["human_decision"]["evidence_at_decision"] == ["EVD-2026-10-003"]
    assert record["evidence"][0]["reference"] == "EVD-2026-10-003"
    assert record["evidence_statement"].startswith("ClosePilot records the evidence references")
    assert [r["recommendation"] for r in record["recommendations"]] == ["APPROVE"]
    assert record["refused_attempts"][0]["attempted_action"] == "DECIDE"
    assert record["investigation"]["classification"] == "VARIANCE"
    assert len(record["audit"]["log_last_event_hash"]) == 64
    markdown = (home / "out/decisions/EX-003.md").read_text(encoding="utf-8")
    assert "# Decision record: EX-003" in markdown
    assert "- **Decided by:** Alex Reviewer (HUMAN)" in markdown
    assert "has not inspected the documents behind them" in markdown

    # Scene 10: package summary.
    code, summary, _ = _reviewer(home, "summary", "FC-2026-10", capsys=capsys)
    assert summary["package_status"] == "EXCEPTIONS_OUTSTANDING"
    assert summary["controls"] == {"PASS": 10, "WARNING": 3, "REVIEW_REQUIRED": 10, "ERROR": 2}
    assert summary["cases"] == 11
    assert summary["human_approved"] == ["EX-003"]
    assert summary["human_rejected"] == []
    assert "EX-003" not in summary["outstanding"]
    assert summary["decision_log"] == "out/audit.jsonl"
    assert all(
        e.actor_type is not ActorType.AGENT or e.human_decision is None for e in _events(home)
    )


def test_reviewer_workflow_refusal_exit_code(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    _reviewer(home, "init", "--name", "Alex Reviewer", capsys=capsys)
    _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    code, _, err = _reviewer(home, "decide", "EX-003", "approve", "--rationale", "x", capsys=capsys)
    assert code == EXIT_REFUSED
    assert "DECIDE is not allowed from OPEN" in err


def test_reviewer_commands_need_setup(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLOSEPILOT_REVIEWER_SECRET", SECRET)
    _agent(home, CA, "open-cases", "FC-2026-10", capsys=capsys)
    code, _, err = _reviewer(home, "comment", "EX-003", "--text", "hello", capsys=capsys)
    assert code == EXIT_REVIEWER_CHECK_FAILED
    assert "no reviewer is set up" in err
    assert _events(home)[-1].actor_type is ActorType.UNVERIFIED


def test_unverified_actor_cannot_act_on_replay(tmp_path: Path) -> None:
    from closepilot.config import load_config
    from closepilot.models import CONTROL_ANALYST, Actor
    from closepilot.workflow import CaseWorkflow, PermissionDenied

    wf = CaseWorkflow(
        AuditLog(tmp_path / "a.jsonl"), load_config(REPO_ROOT / "config" / "controls.yaml")
    )
    wf.open_case(CONTROL_ANALYST, "EX-003", "FC-2026-10", ["C01"], "x")
    with pytest.raises(PermissionDenied, match="unverified actor may not perform ADD_COMMENTARY"):
        wf.add_commentary(Actor(ActorType.UNVERIFIED, "unverified-reviewer"), "EX-003", "hi")
