"""Static checks on the committed Switch assets (template and per-agent permissions)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from closepilot.models import HANDOFF_TARGETS, KNOWN_AGENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "switch" / "closepilot-room.template.yaml"
AGENTS_DIR = REPO_ROOT / "switch" / "agents"


def _template() -> dict[str, Any]:
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_template_creates_exactly_the_case_working_agents_in_their_folders() -> None:
    agents = _template()["agents"]
    assert {a["name"] for a in agents} == HANDOFF_TARGETS
    for agent in agents:
        assert agent["directory"] == f"{{closepilot_home}}/switch/agents/{agent['name']}"
        assert agent["provider"] == "claude"
        assert agent["auto_approve"] is False  # never bypass permissions
        assert agent["addressing"] == "owner-agents"


def test_template_instructions_use_no_builtin_placeholders() -> None:
    """Built-ins such as {$creator} are only documented for specific fields (the room's
    users list); instructions must not depend on them being expanded."""
    for agent in _template()["agents"]:
        assert "{$" not in agent["instructions"], agent["name"]
    assert "{$" not in _template()["room"]["instructions"]


@pytest.mark.parametrize("agent", sorted(KNOWN_AGENTS))
def test_agent_permissions_allow_only_the_wrapper_and_hide_credentials(agent: str) -> None:
    settings = json.loads(
        (AGENTS_DIR / agent / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    permissions = settings["permissions"]
    assert permissions["allow"] == ["Bash(../../bin/closepilot-agent:*)"]
    deny = set(permissions["deny"])
    assert {"Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"} <= deny
    # Switch may write agent credentials under the home folder or the working directory.
    assert {"Read(./.switch/**)", "Read(~/.switch/**)"} <= deny


# --- close room v2 group template ----------------------------------------------------

V2_TEMPLATE = REPO_ROOT / "switch" / "closepilot-close-v2.template.yaml"
REVIEW_ROOM = "{room_prefix}-review"


def _v2() -> dict[str, Any]:
    data = yaml.safe_load(V2_TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_v2_agents_are_the_known_agents_in_their_folders() -> None:
    agents = {a["name"]: a for a in _v2()["agents"]}
    assert set(agents) == KNOWN_AGENTS
    for name, agent in agents.items():
        assert agent["directory"] == f"{{closepilot_home}}/switch/agents/{name}"
        assert agent["provider"] == "claude"
        assert agent["auto_approve"] is False
        assert agent["addressing"] == "owner-agents"
        # Every agent can be reused, so the template runs again on a server that has them.
        assert agent.get("allow_existing") is True


def test_v2_entity_rooms_check_their_own_registered_entity() -> None:
    registry = yaml.safe_load((REPO_ROOT / "config" / "submissions.yaml").read_text("utf-8"))
    entity_rooms = [r for r in _v2()["rooms"] if r["name"] != REVIEW_ROOM]
    assert len(entity_rooms) == 3
    for room in entity_rooms:
        assert room["agents"] == ["control-analyst", "exception-investigator"]
        submission = room["kickoff"].split()[-1]
        assert room["kickoff"].strip() == f"@control-analyst check {submission}"
        entity = registry["submissions"][submission]["entity"]
        assert entity in room["description"] and entity in room["instructions"]


def test_v2_review_room_holds_the_coordinator_and_the_human() -> None:
    [review] = [r for r in _v2()["rooms"] if r["name"] == REVIEW_ROOM]
    assert review["agents"] == ["close-coordinator"]
    assert review["users"] == ["{$creator}"]
    assert review["kickoff"].strip().startswith("@close-coordinator ")


def test_v2_links_join_every_entity_room_to_the_review_room_both_ways() -> None:
    doc = _v2()
    rooms = {r["name"] for r in doc["rooms"]}
    pairs = {(link["from"], link["to"]) for link in doc["links"]}
    assert all(a in rooms and b in rooms and a != b for a, b in pairs)
    for room in rooms - {REVIEW_ROOM}:
        assert (room, REVIEW_ROOM) in pairs and (REVIEW_ROOM, room) in pairs
    assert "room" not in doc and "kickoff" not in doc  # group form only


def test_v2_instructions_use_no_builtin_placeholders() -> None:
    doc = _v2()
    for item in [*doc["agents"], *doc["rooms"]]:
        assert "{$" not in item["instructions"], item["name"]


def test_v2_room_names_come_from_one_prefix() -> None:
    doc = _v2()
    prefix = doc["params"]["room_prefix"]
    assert prefix["default"] == "closepilot-oct"
    assert re.fullmatch(prefix["pattern"], "close-oct")
    assert not re.fullmatch(prefix["pattern"], "Close Oct!")
    names = sorted(r["name"] for r in doc["rooms"])
    assert names == sorted(f"{{room_prefix}}-{s}" for s in ("de", "pl", "us", "review"))
