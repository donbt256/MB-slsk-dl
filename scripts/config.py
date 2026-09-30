from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / os.environ.get("MB_SLSK_CONFIG", "config.yml")


def _load() -> dict[str, Any]:
    if not CONFIG_FILE.is_file():
        raise FileNotFoundError(f"Missing configuration file: {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a YAML object: {CONFIG_FILE}")

    return data


_CONFIG = _load()


def get(path: str, default: Any = None) -> Any:
    value: Any = _CONFIG
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def env_or(path: str, env_name: str, default: Any) -> Any:
    value = os.environ.get(env_name)
    if value is not None and value != "":
        return value
    return get(path, default)


def integer(path: str, env_name: str | None = None, default: int = 0) -> int:
    value = os.environ.get(env_name) if env_name else None
    if value is None or value == "":
        value = get(path, default)
    return int(value)


def string(path: str, env_name: str | None = None, default: str = "") -> str:
    value = os.environ.get(env_name) if env_name else None
    if value is None:
        value = get(path, default)
    return str(value)


def path(path_key: str, env_name: str | None = None, default: str = "") -> Path:
    return ROOT / string(path_key, env_name, default)
