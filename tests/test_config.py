from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from closepilot.config import ConfigError, ControlConfig, load_config, parse_config


def _valid() -> dict[str, Any]:
    return {
        "variance_warning_pct": 5,
        "variance_review_pct": 10,
        "commentary_required_pct": 5,
        "allowed_currencies": ["EUR", "USD", "PLN"],
        "allowed_periods": ["2026-10"],
        "mapping_valid_values": ["VALID"],
        "evidence_required_controls": ["C01"],
    }


def test_repository_config_loads_exact_values(config: ControlConfig) -> None:
    assert config.variance_warning_pct == Decimal("5")
    assert config.variance_review_pct == Decimal("10")
    assert config.commentary_required_pct == Decimal("5")
    assert config.allowed_currencies == frozenset({"EUR", "USD", "PLN"})
    assert config.allowed_periods == frozenset({"2026-10"})
    assert config.mapping_valid_values == frozenset({"VALID"})
    assert config.evidence_required_controls == frozenset({"C01"})


def test_missing_setting_is_rejected() -> None:
    data = _valid()
    del data["variance_review_pct"]
    with pytest.raises(ConfigError, match="missing required setting 'variance_review_pct'"):
        parse_config(data)


@pytest.mark.parametrize("bad", ["ten", True, None, [10], -1, "NaN", "Infinity"])
def test_invalid_threshold_is_rejected(bad: object) -> None:
    data = _valid()
    data["variance_review_pct"] = bad
    with pytest.raises(ConfigError, match="variance_review_pct"):
        parse_config(data)


def test_warning_above_review_is_rejected() -> None:
    data = _valid()
    data["variance_warning_pct"] = 12
    with pytest.raises(ConfigError, match="must not exceed"):
        parse_config(data)


def test_unquoted_period_is_rejected() -> None:
    data = _valid()
    data["allowed_periods"] = [202610]
    with pytest.raises(ConfigError, match="allowed_periods"):
        parse_config(data)


def test_non_mapping_is_rejected() -> None:
    with pytest.raises(ConfigError, match="YAML mapping"):
        parse_config(["not", "a", "mapping"])


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("variance_warning_pct: [5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed YAML"):
        load_config(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read configuration file"):
        load_config(tmp_path / "absent.yaml")


def test_python_object_tags_are_not_executed(tmp_path: Path) -> None:
    path = tmp_path / "evil.yaml"
    path.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed YAML"):
        load_config(path)


def test_evidence_required_controls_may_be_empty() -> None:
    data = _valid()
    data["evidence_required_controls"] = []
    assert parse_config(data).evidence_required_controls == frozenset()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["C01", "C99"], "unknown control IDs: C99"),
        ("C01", "must be a list of control IDs"),
        ([1], "must be a list of control IDs"),
    ],
)
def test_invalid_evidence_required_controls_rejected(value: object, message: str) -> None:
    data = _valid()
    data["evidence_required_controls"] = value
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_missing_evidence_required_controls_rejected() -> None:
    data = _valid()
    del data["evidence_required_controls"]
    with pytest.raises(ConfigError, match="missing required setting 'evidence_required_controls'"):
        parse_config(data)
