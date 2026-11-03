"""Submission registry: maps a submission ID to its close-package file.

Agents name a submission by ID and never pass a file path, so a message in a room cannot
point ClosePilot at an arbitrary file. Every registered path must resolve to a file
inside the ClosePilot home directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_REGISTRY_BYTES = 64 * 1024
_SUBMISSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class SubmissionError(ValueError):
    """Raised when the registry is malformed or a submission ID is unknown or unsafe."""


@dataclass(frozen=True)
class Submission:
    submission_id: str
    package: Path


def parse_registry(data: object, home: Path) -> dict[str, Submission]:
    if not isinstance(data, dict) or not isinstance(data.get("submissions"), dict):
        raise SubmissionError("registry must be a mapping with a 'submissions' mapping")
    root = home.resolve()
    registry: dict[str, Submission] = {}
    for submission_id, entry in data["submissions"].items():
        if not isinstance(submission_id, str) or not _SUBMISSION_ID_RE.fullmatch(submission_id):
            raise SubmissionError(f"invalid submission ID in registry: {submission_id!r}")
        if not isinstance(entry, dict) or not isinstance(entry.get("package"), str):
            raise SubmissionError(f"submission {submission_id!r} needs a 'package' path")
        package = (root / entry["package"]).resolve()
        if not package.is_relative_to(root):
            raise SubmissionError(
                f"submission {submission_id!r} package must be inside the ClosePilot home"
            )
        registry[submission_id] = Submission(submission_id, package)
    return registry


def load_registry(path: Path, home: Path) -> dict[str, Submission]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SubmissionError(f"cannot read submission registry '{path}': {exc.strerror}") from exc
    if size > MAX_REGISTRY_BYTES:
        raise SubmissionError(f"submission registry exceeds {MAX_REGISTRY_BYTES} bytes")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SubmissionError(f"malformed YAML in '{path}': {exc}") from exc
    return parse_registry(data, home)


def resolve(registry: dict[str, Submission], submission_id: str) -> Submission:
    if not _SUBMISSION_ID_RE.fullmatch(submission_id):
        raise SubmissionError(
            "submission ID must be 1-64 characters of letters, digits, '.', '_' or '-'"
        )
    submission = registry.get(submission_id)
    if submission is None:
        known = ", ".join(sorted(registry)) or "none"
        raise SubmissionError(f"unknown submission {submission_id!r} (registered: {known})")
    return submission
