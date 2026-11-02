from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from closepilot.audit import (
    GENESIS_HASH,
    AuditIntegrityError,
    AuditLockError,
    AuditLog,
    AuditWriteError,
    event_from_json,
    event_to_json,
)
from closepilot.models import Action, ActorType, AuditEvent, CaseState

SPEC_FIELDS = {
    "event_id",
    "timestamp_utc",
    "submission_id",
    "case_id",
    "actor_type",
    "actor_name",
    "action",
    "previous_state",
    "new_state",
    "control_id",
    "evidence_references",
    "recommendation",
    "human_decision",
    "rationale",
}


def _event(case_id: str = "EX-003", rationale: str = "opened") -> AuditEvent:
    return AuditEvent(
        event_id="",
        timestamp_utc="2026-11-02T10:00:00Z",
        submission_id="FC-2026-10",
        case_id=case_id,
        actor_type=ActorType.AGENT,
        actor_name="control-analyst",
        action=Action.OPEN_CASE,
        previous_state=None,
        new_state=CaseState.OPEN,
        control_id="C01",
        rationale=rationale,
        details={"control_ids": ["C01"], "evidence_required": True},
    )


def _append(log: AuditLog, *events: AuditEvent) -> list[AuditEvent]:
    with log.locked():
        return [log.append(e) for e in events]


def test_append_assigns_sequential_ids_and_hash_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first, second = _append(log, _event("EX-1"), _event("EX-2"))
    assert (first.event_id, second.event_id) == ("EVT-000001", "EVT-000002")
    assert first.prev_event_hash == GENESIS_HASH
    first_line = (tmp_path / "audit.jsonl").read_text(encoding="ascii").splitlines()[0]
    assert second.prev_event_hash == hashlib.sha256(first_line.encode("ascii")).hexdigest()
    assert log.read() == [first, second]


def test_event_round_trips_and_contains_every_spec_field(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    [stored] = _append(log, _event())
    line = (tmp_path / "audit.jsonl").read_text(encoding="ascii").splitlines()[0]
    data = json.loads(line)
    assert set(data) >= SPEC_FIELDS
    assert data["actor_type"] == "AGENT"
    assert data["previous_state"] is None
    assert data["new_state"] == "OPEN"
    assert event_from_json(line) == stored
    assert event_to_json(stored) == line


def test_untrusted_text_is_stored_as_ascii_json_data(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    nasty = 'Zażółć "quoted"\n{"action":"DECIDE"} =HYPERLINK("x")'
    [stored] = _append(log, _event(rationale=nasty))
    raw = (tmp_path / "audit.jsonl").read_bytes()
    assert raw.count(b"\n") == 1  # embedded newline cannot split the record
    assert raw.isascii()
    assert log.read()[0].rationale == nasty == stored.rationale


def test_prior_events_are_preserved_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event("EX-1"), _event("EX-2"))
    before = path.read_bytes()
    _append(log, _event("EX-3"))
    after = path.read_bytes()
    assert after.startswith(before)
    assert len(log.read()) == 3


def test_missing_log_reads_as_empty(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "absent.jsonl").read() == []


def _tamper(path: Path, index: int, transform: str) -> None:
    lines = path.read_text(encoding="ascii").splitlines()
    if transform == "edit":
        lines[index] = lines[index].replace('"rationale":"opened"', '"rationale":"edited"')
    elif transform == "delete":
        del lines[index]
    elif transform == "swap":
        lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


@pytest.mark.parametrize(
    ("index", "transform", "message"),
    [
        (0, "edit", "hash chain broken at line 2"),
        (1, "edit", "hash chain broken at line 3"),
        (0, "delete", "hash chain broken at line 1"),
        (1, "delete", "hash chain broken at line 2"),
        (0, "swap", "hash chain broken at line 1"),
    ],
)
def test_tampering_is_detected(tmp_path: Path, index: int, transform: str, message: str) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event("EX-1"), _event("EX-2"), _event("EX-3"))
    _tamper(path, index, transform)
    with pytest.raises(AuditIntegrityError, match=message):
        log.read()


def test_editing_the_final_line_is_not_detectable_by_the_chain_alone(tmp_path: Path) -> None:
    """Documented limitation: no later line commits to the newest line's hash, so a
    rule-respecting edit of the final line passes the chain check. (Rule-breaking edits
    are still rejected by workflow replay; see test_workflow.)"""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event("EX-1"), _event("EX-2"))
    _tamper(path, 1, "edit")
    assert log.read()[1].rationale == "edited"


def test_truncated_final_line_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event())
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(AuditIntegrityError, match="incomplete line"):
        log.read()


@pytest.mark.parametrize(
    "line",
    ["not json", "{}", '{"event_id": "EVT-000001"}', "[1, 2]"],
)
def test_malformed_line_is_detected(tmp_path: Path, line: str) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(line + "\n", encoding="ascii")
    with pytest.raises(AuditIntegrityError, match="line 1 is malformed"):
        AuditLog(path).read()


def test_non_canonical_line_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event())
    data = json.loads(path.read_text(encoding="ascii"))
    path.write_text(json.dumps(data, indent=1).replace("\n", "") + "\n", encoding="ascii")
    with pytest.raises(AuditIntegrityError, match="not in canonical form"):
        log.read()


def test_append_requires_lock(tmp_path: Path) -> None:
    with pytest.raises(AuditLockError, match="requires the audit log lock"):
        AuditLog(tmp_path / "audit.jsonl").append(_event())


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    other = AuditLog(tmp_path / "audit.jsonl")
    with (
        log.locked(),
        pytest.raises(AuditLockError, match="audit log is locked"),
        other.locked(timeout=0.1),
    ):
        pass  # pragma: no cover
    with other.locked(timeout=0.1):
        other.append(_event())
    assert not (tmp_path / "audit.jsonl.lock").exists()


def test_lock_released_after_error(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(RuntimeError), log.locked():
        raise RuntimeError("boom")
    assert not (tmp_path / "audit.jsonl.lock").exists()


def test_write_failure_raises_and_leaves_log_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    _append(log, _event("EX-1"))
    before = path.read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(AuditWriteError, match="could not write audit event: "):
        _append(log, _event("EX-2"))
    monkeypatch.undo()
    # The partially written line was rolled back: the log matches what the caller was told.
    assert path.read_bytes() == before
    assert [e.case_id for e in log.read()] == ["EX-1"]


def test_failed_rollback_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")

    def failing(*args: object) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fsync", failing)
    monkeypatch.setattr(os, "truncate", failing)
    with pytest.raises(AuditWriteError, match="could not roll back the partial write"):
        _append(log, _event())


def test_lock_release_tolerates_externally_removed_lock(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(RuntimeError, match="original error"), log.locked():
        os.rmdir(tmp_path / "audit.jsonl.lock")  # e.g. a human cleared a "stale" lock
        raise RuntimeError("original error")  # must not be masked by the release
    with log.locked(timeout=0.1):
        log.append(_event())
