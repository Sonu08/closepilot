"""Reviewer check: proves a command comes from the human Finance Reviewer's own shell.

``closepilot reviewer init`` stores the reviewer's name and a salted scrypt hash of a
secret in ``.closepilot/reviewer.json`` (git-ignored). Every reviewer command that changes
a case must then find the same secret in the ``CLOSEPILOT_REVIEWER_SECRET`` environment
variable of the shell it runs in.

Why this keeps agents out: Switch starts agent sessions with a fixed set of environment
variables, so the secret set in the human's shell never reaches an agent; the agents'
permission files allow only the agent wrapper, which always runs ``closepilot agent``; and
the stored hash cannot be turned back into the secret.

Limits, stated plainly: this is a local demonstration control, not identity management.
Anyone who can run commands as the same operating-system user *and* knows the secret is
treated as the reviewer. Choose a secret that is not stored anywhere an agent can read.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from closepilot.models import HUMAN_ROLE
from closepilot.workflow import PermissionDenied, check_human_name

SECRET_ENV = "CLOSEPILOT_REVIEWER_SECRET"  # noqa: S105 - variable name, not a secret
REVIEWER_FILE = Path(".closepilot/reviewer.json")
MIN_SECRET_CHARS = 12
MAX_SECRET_CHARS = 1024
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class ReviewerError(Exception):
    """The reviewer is not set up, or the reviewer check failed."""


@dataclass(frozen=True)
class Reviewer:
    name: str


def _derive(secret: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)


def _check_secret(secret: str | None) -> str:
    if not secret:
        raise ReviewerError(f"set the reviewer secret in {SECRET_ENV} in your own shell")
    if not MIN_SECRET_CHARS <= len(secret) <= MAX_SECRET_CHARS:
        raise ReviewerError(
            f"the reviewer secret must be {MIN_SECRET_CHARS}-{MAX_SECRET_CHARS} characters"
        )
    return secret


def init_reviewer(path: Path, name: str, secret: str | None) -> Reviewer:
    """Create the reviewer file. Refuses to overwrite an existing one."""
    secret = _check_secret(secret)
    try:
        check_human_name(name)
    except PermissionDenied as exc:
        raise ReviewerError(str(exc)) from exc
    if path.exists():
        raise ReviewerError(
            f"a reviewer is already set up in {path}; delete that file yourself to start over"
        )
    salt = secrets.token_bytes(16)
    record = {
        "version": 1,
        "name": name.strip(),
        "kdf": "scrypt",
        **_SCRYPT,
        "salt": salt.hex(),
        "hash": _derive(secret, salt, **_SCRYPT).hex(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    with contextlib.suppress(OSError):  # best effort; Windows ignores most mode bits
        os.chmod(path, 0o600)
    return Reviewer(name=name.strip())


def verify_reviewer(path: Path, secret: str | None) -> Reviewer:
    """Return the reviewer if ``secret`` matches the stored hash, else raise."""
    if not path.exists():
        raise ReviewerError(f"no reviewer is set up; run 'closepilot reviewer init' first ({path})")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        params = {k: int(record[k]) for k in ("n", "r", "p", "dklen")}
        name = str(record["name"])
        if record.get("kdf") != "scrypt":
            raise ValueError("unsupported key derivation")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewerError(f"the reviewer file {path} is unreadable or malformed") from exc
    supplied = _derive(_check_secret(secret), salt, **params)
    if not hmac.compare_digest(supplied, expected):
        raise ReviewerError("the reviewer secret does not match")
    return Reviewer(name=name or HUMAN_ROLE)
