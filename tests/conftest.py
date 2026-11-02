from __future__ import annotations

from pathlib import Path

import pytest

from closepilot.config import ControlConfig, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def config() -> ControlConfig:
    return load_config(REPO_ROOT / "config" / "controls.yaml")


@pytest.fixture
def c01_package() -> Path:
    return FIXTURES / "c01_package.csv"
