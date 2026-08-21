"""Shared config loading.

config.json is tracked in git with placeholder values. Real credentials go
in config.local.json instead (gitignored) — its keys override config.json's
when both are present, so config.json never needs to hold real secrets.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_config(config_path: str | Path = "config.json") -> dict:
    config_path = Path(config_path)
    config: dict = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))

    local_path = config_path.parent / "config.local.json"
    if local_path.exists():
        config.update(json.loads(local_path.read_text(encoding="utf-8")))

    return config
