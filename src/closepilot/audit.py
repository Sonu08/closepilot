"""Append-only audit log in JSON Lines format.

Guarantees (application level only; this is NOT immutable or cryptographically
secured storage, and anyone with write access to the file can still replace it):

* Events are only ever appended; no API rewrites or deletes a line.
* Each event stores ``prev_event_hash``: the SHA-256 of the previous line's exact bytes
  (64 zeros for the first event). Editing, removing, reordering or truncating earlier
  lines is therefore *detected* on the next read, which fails closed.
* Writes happen under an exclusive lock directory, so concurrent commands (for example
  two agents in a room) cannot interleave lines or fork the hash chain.
* A write is flushed and fsynced before ``append`` returns. If writing fails, an
  ``AuditWriteError`` is raised and callers must not report the action as recorded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from closepilot.models import (
    Action,
    ActorType,
    AuditEvent,
    CaseState,
    Decision,
    DetailValue,
    Recommendation,
)

GENESIS_HASH = "0" * 64
LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05

_FIELDS: tuple[str, ...] = (
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
    "details",
    "prev_event_hash",
)


class AuditError(Exception):
    """Base class for audit-log failures."""


class AuditIntegrityError(AuditError):
    """The log is malformed or its hash chain is broken (possible tampering)."""


class AuditWriteError(AuditError):
    """An event could not be written; the action must not be reported as recorded."""


class AuditLockError(AuditError):
    """The log lock could not be acquired, or an append was attempted without it."""


def event_to_json(event: AuditEvent) -> str:
    """Canonical single-line JSON (sorted keys, ASCII only) used for storage and hashing."""
    payload: dict[str, Any] = {
        "event_id": event.event_id,
        "timestamp_utc": event.timestamp_utc,
        "submission_id": event.submission_id,
        "case_id": event.case_id,
        "actor_type": event.actor_type.value,
        "actor_name": event.actor_name,
        "action": event.action.value,
        "previous_state": None if event.previous_state is None else event.previous_state.value,
        "new_state": None if event.new_state is None else event.new_state.value,
        "control_id": event.control_id,
        "evidence_references": list(event.evidence_references),
        "recommendation": None if event.recommendation is None else event.recommendation.value,
        "human_decision": None if event.human_decision is None else event.human_decision.value,
        "rationale": event.rationale,
        "details": dict(event.details),
        "prev_event_hash": event.prev_event_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("ascii")).hexdigest()


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data[key]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _details(value: object) -> dict[str, DetailValue]:
    if not isinstance(value, dict):
        raise TypeError("details must be an object")
    result: dict[str, DetailValue] = {}
    for key, item in value.items():
        if isinstance(item, bool | str):
            result[str(key)] = item
        elif isinstance(item, list) and all(isinstance(x, str) for x in item):
            result[str(key)] = list(item)
        else:
            raise TypeError(f"details.{key} has an unsupported type")
    return result


def event_from_json(line: str) -> AuditEvent:
    data = json.loads(line)
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        raise ValueError("event does not have exactly the expected fields")
    refs = data["evidence_references"]
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        raise TypeError("evidence_references must be a list of strings")
    previous = _optional_str(data, "previous_state")
    new = _optional_str(data, "new_state")
    recommendation = _optional_str(data, "recommendation")
    decision = _optional_str(data, "human_decision")
    return AuditEvent(
        event_id=_str(data, "event_id"),
        timestamp_utc=_str(data, "timestamp_utc"),
        submission_id=_str(data, "submission_id"),
        case_id=_str(data, "case_id"),
        actor_type=ActorType(_str(data, "actor_type")),
        actor_name=_str(data, "actor_name"),
        action=Action(_str(data, "action")),
        previous_state=None if previous is None else CaseState(previous),
        new_state=None if new is None else CaseState(new),
        control_id=_optional_str(data, "control_id"),
        evidence_references=tuple(refs),
        recommendation=None if recommendation is None else Recommendation(recommendation),
        human_decision=None if decision is None else Decision(decision),
        rationale=_optional_str(data, "rationale"),
        details=_details(data["details"]),
        prev_event_hash=_str(data, "prev_event_hash"),
    )


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_name(path.name + ".lock")
        self._locked = False

    @contextmanager
    def locked(self, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Hold the exclusive write lock (an atomically created lock directory)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.mkdir(self._lock_path)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise AuditLockError(
                        f"audit log is locked ({self._lock_path}); if no other ClosePilot "
                        "command is running, a previous run was interrupted and the lock "
                        "directory may be removed by a human"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        self._locked = True
        try:
            yield
        finally:
            self._locked = False
            # Tolerate a lock directory that someone already removed, so releasing the
            # lock can never mask the error that is propagating.
            with contextlib.suppress(FileNotFoundError):
                os.rmdir(self._lock_path)

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise AuditIntegrityError("audit log ends with an incomplete line")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuditIntegrityError("audit log contains non-ASCII bytes") from exc
        return text.splitlines()

    def read(self) -> list[AuditEvent]:
        """Return all events, verifying format, sequence and hash chain."""
        events: list[AuditEvent] = []
        expected_prev = GENESIS_HASH
        for number, line in enumerate(self._read_lines(), start=1):
            try:
                event = event_from_json(line)
            except (ValueError, TypeError, KeyError) as exc:
                raise AuditIntegrityError(f"audit log line {number} is malformed: {exc}") from exc
            if event_to_json(event) != line:
                raise AuditIntegrityError(f"audit log line {number} is not in canonical form")
            if event.prev_event_hash != expected_prev:
                raise AuditIntegrityError(
                    f"audit log hash chain broken at line {number} ({event.event_id}); "
                    "an earlier line was changed, removed or reordered"
                )
            if event.event_id != _event_id(number):
                raise AuditIntegrityError(
                    f"audit log line {number} has event_id {event.event_id!r}, "
                    f"expected {_event_id(number)!r}"
                )
            events.append(event)
            expected_prev = _line_hash(line)
        return events

    def append(
        self, event: AuditEvent, finalise: Callable[[AuditEvent], AuditEvent] | None = None
    ) -> AuditEvent:
        """Append ``event``, assigning its ``event_id`` and ``prev_event_hash``.

        ``finalise``, if given, is applied once both are assigned (for example to seal a
        human event over its final position in the chain); it may change only
        ``details``. Must be called while holding :meth:`locked`. Returns the stored event.
        """
        if not self._locked:
            raise AuditLockError("append requires the audit log lock")
        existing = self.read()  # verifies the chain before extending it
        # Stored lines are canonical, so re-serialising the last event reproduces its bytes.
        prev = _line_hash(event_to_json(existing[-1])) if existing else GENESIS_HASH
        stored = replace(event, event_id=_event_id(len(existing) + 1), prev_event_hash=prev)
        if finalise is not None:
            finalised = finalise(stored)
            if replace(finalised, details=stored.details) != stored:
                raise AuditWriteError("finalise may change only an event's details")
            stored = finalised
        line = event_to_json(stored)
        size_before = self.path.stat().st_size if self.path.exists() else 0
        try:
            with self.path.open("a", encoding="ascii", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Roll back any partial write so the log and the caller agree that the event
            # was not recorded. If even that fails, say so: the next read fails closed.
            try:
                os.truncate(self.path, size_before)
            except OSError as rollback_exc:
                raise AuditWriteError(
                    f"could not write audit event ({exc}) and could not roll back the "
                    f"partial write ({rollback_exc}); the log must be checked by a human"
                ) from exc
            raise AuditWriteError(f"could not write audit event: {exc}") from exc
        return stored


def last_event_hash(events: list[AuditEvent]) -> str:
    """SHA-256 of the newest stored line (its canonical form), or the genesis hash.

    Recording this value outside the log (for example in an exported decision record)
    anchors the chain: a later edit of the newest line no longer matches it."""
    return _line_hash(event_to_json(events[-1])) if events else GENESIS_HASH


def _event_id(number: int) -> str:
    return f"EVT-{number:06d}"
