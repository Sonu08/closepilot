"""Static checks on the committed Switch assets (template and per-agent permissions)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from closepilot.models import KNOWN_AGENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "switch" / "closepilot-room.template.yaml"
AGENTS_DIR = REPO_ROOT / "switch" / "agents"


def _template() -> dict[str, Any]:
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_template_creates_exactly_the_known_agents_in_their_folders() -> None:
    agents = _template()["agents"]
    assert {a["name"] for a in agents} == KNOWN_AGENTS
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
