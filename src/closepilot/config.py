"""Load and validate the YAML control configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

MAX_CONFIG_BYTES = 64 * 1024


class ConfigError(ValueError):
    """Raised when the control configuration is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class ControlConfig:
    variance_warning_pct: Decimal
    variance_review_pct: Decimal
    commentary_required_pct: Decimal
    allowed_currencies: frozenset[str]
    allowed_periods: frozenset[str]
    mapping_valid_values: frozenset[str]


def _pct(data: dict[str, Any], key: str) -> Decimal:
    if key not in data:
        raise ConfigError(f"missing required setting '{key}'")
    value = data[key]
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ConfigError(f"'{key}' must be a number, got {type(value).__name__}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"'{key}' must be a number, got {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ConfigError(f"'{key}' must be a finite, non-negative number, got {value!r}")
    return result


def _str_set(data: dict[str, Any], key: str) -> frozenset[str]:
    if key not in data:
        raise ConfigError(f"missing required setting '{key}'")
    value = data[key]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"'{key}' must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(
            f"'{key}' must contain only non-empty strings (quote values like \"2026-10\")"
        )
    return frozenset(item.strip() for item in value)


def parse_config(data: object) -> ControlConfig:
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a YAML mapping")
    config = ControlConfig(
        variance_warning_pct=_pct(data, "variance_warning_pct"),
        variance_review_pct=_pct(data, "variance_review_pct"),
        commentary_required_pct=_pct(data, "commentary_required_pct"),
        allowed_currencies=_str_set(data, "allowed_currencies"),
        allowed_periods=_str_set(data, "allowed_periods"),
        mapping_valid_values=_str_set(data, "mapping_valid_values"),
    )
    if config.variance_warning_pct > config.variance_review_pct:
        raise ConfigError("variance_warning_pct must not exceed variance_review_pct")
    return config


def load_config(path: Path) -> ControlConfig:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file '{path}': {exc.strerror}") from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration file exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in '{path}': {exc}") from exc
    return parse_config(data)
