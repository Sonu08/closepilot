"""Reviewer check: proves a command comes from the human Finance Reviewer's own shell.

``closepilot reviewer init`` stores the reviewer's name and a salted scrypt hash of a
secret in ``.closepilot/reviewer.json`` (git-ignored). Every reviewer command that changes
a case must then find the same secret in the ``CLOSEPILOT_REVIEWER_SECRET`` environment
variable of the shell it runs in.

Why this keeps agents out: Switch starts agent sessions with a fixed set of environment
variables, so the secret set in the human's shell never reaches an agent; the agents'
permission files allow only the agent wrapper, which always runs ``closepilot agent``; and
the stored hash cannot be turned back into the secret.

Sealed human events: the agents run as the same operating-system user, so they could
replace the reviewer file (and set up a secret of their own) or append a hand-made
"human" line to the audit log. To make both detectable, every human event is sealed with
an HMAC whose key is derived from the secret itself (never stored), and every reviewer
command that reads or changes cases first checks the seal of every human event in the
log. A forged line has no valid seal, and a re-initialised reviewer cannot reproduce the
seals of the real one, so the reviewer interface refuses to act on such a log.

Limits, stated plainly: this is a local demonstration control, not identity management.
Anyone who can run commands as the same operating-system user *and* knows the secret is
treated as the reviewer. Agent-side views (``closepilot agent``) cannot check seals, since
agents never hold the key. Deleting the whole log and the reviewer file and starting over
is not detectable from inside; the log hash recorded in an exported decision record is
the outside anchor. Choose a secret that is not stored anywhere an agent can read.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from closepilot.audit import AuditIntegrityError, event_to_json
from closepilot.models import HUMAN_ROLE, Action, ActorType, AuditEvent
from closepilot.workflow import PermissionDenied, check_human_name

SECRET_ENV = "CLOSEPILOT_REVIEWER_SECRET"  # noqa: S105 - variable name, not a secret
REVIEWER_FILE = Path(".closepilot/reviewer.json")
MIN_SECRET_CHARS = 12
MAX_SECRET_CHARS = 1024
SALT_BYTES = 16
SEAL_FIELD = "seal"
# Fixed: a reviewer file naming other parameters is refused, so a tampered file can
# neither weaken the hash nor make the derivation hang or crash.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
# The seal key is a second derivation of the secret, so the stored hash reveals nothing
# about it.
_SEAL_SALT_SUFFIX = b"closepilot-seal-v1"


class ReviewerError(Exception):
    """The reviewer is not set up, or the reviewer check failed."""


@dataclass(frozen=True)
class Reviewer:
    name: str
    seal_key: bytes = field(default=b"", repr=False)

    def seal(self, event: AuditEvent) -> AuditEvent:
        """Return ``event`` with its seal added (used as the audit log's finaliser)."""
        if not self.seal_key:
            raise ReviewerError("this reviewer has no seal key")
        return replace(event, details={**event.details, SEAL_FIELD: _seal(self.seal_key, event)})

    def verify_log(self, events: Sequence[AuditEvent]) -> None:
        """Fail closed unless every human event carries this reviewer's valid seal."""
        bad = [
            e.event_id
            for e in events
            if _sealable(e)
            and not hmac.compare_digest(str(e.details.get(SEAL_FIELD, "")), _seal(self.seal_key, e))
        ]
        if bad:
            shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
            raise AuditIntegrityError(
                f"the audit log holds human events not sealed by the current reviewer "
                f"({shown}): a forged line, or the reviewer was set up again. Nothing was "
                "done; a human must inspect the log"
            )


def _sealable(event: AuditEvent) -> bool:
    # A refusal records an attempt, not an action; it is never sealed.
    return event.actor_type is ActorType.HUMAN and event.action is not Action.ACCESS_DENIED


def _seal(key: bytes, event: AuditEvent) -> str:
    unsealed = {k: v for k, v in event.details.items() if k != SEAL_FIELD}
    payload = event_to_json(replace(event, details=unsealed)).encode("ascii")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


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
    salt = secrets.token_bytes(SALT_BYTES)
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
        params = {k: record[k] for k in _SCRYPT}
        name = str(record["name"])
        if record.get("kdf") != "scrypt" or params != _SCRYPT:
            raise ValueError("unsupported key derivation parameters")
        if len(salt) != SALT_BYTES or len(expected) != _SCRYPT["dklen"]:
            raise ValueError("salt or hash has the wrong length")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewerError(f"the reviewer file {path} is unreadable or malformed") from exc
    secret = _check_secret(secret)
    if not hmac.compare_digest(_derive(secret, salt, **_SCRYPT), expected):
        raise ReviewerError(
            "the reviewer secret does not match. If you did not change it, the reviewer "
            "file may have been replaced: act on nothing until a human has checked "
            f"{path} and the audit log"
        )
    seal_key = _derive(secret, salt + _SEAL_SALT_SUFFIX, **_SCRYPT)
    return Reviewer(name=name or HUMAN_ROLE, seal_key=seal_key)
