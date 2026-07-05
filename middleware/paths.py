"""Resolve config and runtime paths for dev checkout vs installed package."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

ENV_CONFIG = "CODEXCONT_CONFIG"
ENV_HOME = "CODEXCONT_HOME"


def is_dev_checkout() -> bool:
    return (PACKAGE_ROOT / "pyproject.toml").exists()


def user_data_dir() -> Path:
    if home := os.environ.get(ENV_HOME):
        return Path(home).expanduser().resolve()
    return Path.home() / ".codexcont"


def config_path() -> Path:
    if cfg := os.environ.get(ENV_CONFIG):
        return Path(cfg).expanduser().resolve()
    if is_dev_checkout():
        return PACKAGE_ROOT / "config.toml"
    return user_data_dir() / "config.toml"


def state_dir() -> Path:
    if is_dev_checkout():
        return PACKAGE_ROOT / ".codexcont"
    return user_data_dir()


def backup_dir() -> Path:
    return user_data_dir() / "backup"


def example_config_path() -> Path:
    repo_example = PACKAGE_ROOT / "config.example.toml"
    if repo_example.exists():
        return repo_example
    bundled = Path(__file__).resolve().parent / "data" / "config.example.toml"
    return bundled


def read_example_config() -> str | None:
    repo_example = PACKAGE_ROOT / "config.example.toml"
    if repo_example.exists():
        return repo_example.read_text(encoding="utf-8")
    bundled = Path(__file__).resolve().parent / "data" / "config.example.toml"
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")
    try:
        return (
            resources.files("middleware")
            .joinpath("data/config.example.toml")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, TypeError):
        return None
