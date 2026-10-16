"""Saved pipelines: ``pipelines/<name>.spec.json``.

A saved pipeline is a spec document with a description::

    {"version": 1, "description": "...", "root": {...}}
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from ico_mcp.protocol import JsonObject, JsonValue, KernelError

SUFFIX = ".spec.json"
SPEC_VERSION = 1

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def document(spec: JsonObject, description: str) -> JsonObject:
    """A spec document with a description; a node spec is wrapped."""
    if "root" in spec:
        version, root = spec.get("version"), spec["root"]
    else:
        version, root = SPEC_VERSION, spec
    return {"version": version, "description": description, "root": root}


def save(directory: Path, name: str, doc: JsonObject, *, replace: bool) -> Path:
    """Write a pipeline document.

    Raises:
        KernelError: ``bad_request`` for an invalid name, or if the pipeline
            exists and ``replace`` is false.
    """
    path = _path(directory, name)
    if path.exists() and not replace:
        raise exists_error(name)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path


def exists(directory: Path, name: str) -> bool:
    """Whether a pipeline is saved (``bad_request`` for an invalid name)."""
    return _path(directory, name).is_file()


def exists_error(name: str) -> KernelError:
    return KernelError(
        "bad_request",
        f"pipeline {name!r} already exists; pass replace=true to overwrite it",
        {"name": name},
    )


def load(directory: Path, name: str) -> JsonObject:
    """Read a pipeline document.

    Raises:
        KernelError: ``not_found`` if there is no such pipeline.
    """
    path = _path(directory, name)
    if not path.is_file():
        raise KernelError("not_found", f"pipeline {name!r} not found", {"name": name})
    return cast(JsonObject, json.loads(path.read_text()))


def names(directory: Path) -> list[str]:
    """Names of the saved pipelines, sorted."""
    return sorted(p.name[: -len(SUFFIX)] for p in directory.glob(f"*{SUFFIX}"))


def description(doc: JsonObject) -> JsonValue:
    return doc.get("description", "")


def _path(directory: Path, name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise KernelError(
            "bad_request",
            f"invalid pipeline name {name!r}: use lowercase letters, digits, "
            "'_' and '-'",
            {"name": name},
        )
    return directory / f"{name}{SUFFIX}"
