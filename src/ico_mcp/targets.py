"""The state of targets that changes from order to order (server side,
files only): the network volume of a target while an order runs.

It lives in ``runs/targets/<target>.json``, not in ``ico.toml``: a volume
is created for an order (``volume_up``) and deleted after it
(``volume_down``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject


def volume(config: Config, target: str) -> JsonObject | None:
    """The current volume of a target (``id``, ``name``, ``size``,
    ``datacenter``), or ``None``."""
    path = _path(config, target)
    if not path.is_file():
        return None
    data = cast(JsonObject, json.loads(path.read_text()))
    found = data.get("volume")
    return cast(JsonObject, found) if isinstance(found, dict) else None


def set_volume(config: Config, target: str, value: JsonObject | None) -> None:
    path = _path(config, target)
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"volume": value}, indent=1))


def _path(config: Config, target: str) -> Path:
    return config.runs_dir / "targets" / f"{target}.json"
