"""Saved pipelines: ``pipelines/<name>.spec.json``.

A saved pipeline is a spec document with a description::

    {"version": 1, "description": "...", "root": {...},
     "check_params": {"out_dir": "/tmp/check"}}

``check_params`` (optional) holds values for the pipeline parameters
without a default (``{"$param": ...}`` references), used only to validate
the pipeline (``save_pipeline``, the ``crystallize`` gate), never as run
values.

A pipeline may use others through ``pipeline_ref`` nodes (``ico.spec``):
it is saved with its references, so a fix in a used pipeline reaches every
pipeline that uses it, and :func:`inline` replaces them before a spec goes
to a kernel. These are the pipelines of this directory, the library.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from ico.spec import NodeSpec, PipelineSpec, SpecError, pipeline_refs
from ico.spec import inline as inline_refs

from ico_mcp.protocol import JsonObject, JsonValue, KernelError

SUFFIX = ".spec.json"
SPEC_VERSION = 1

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def document(
    spec: JsonObject, description: str, check_params: JsonObject | None = None
) -> JsonObject:
    """A spec document with a description; a node spec is wrapped."""
    if "root" in spec:
        version, root = spec.get("version"), spec["root"]
    else:
        version, root = SPEC_VERSION, spec
    doc: JsonObject = {"version": version, "description": description, "root": root}
    if check_params:
        doc["check_params"] = check_params
    return doc


def check_params(doc: JsonObject) -> JsonObject | None:
    """The values that validate a saved pipeline, if any."""
    values = doc.get("check_params")
    return cast(JsonObject, values) if isinstance(values, dict) else None


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


def delete(directory: Path, name: str) -> Path:
    """Remove a saved pipeline.

    Raises:
        KernelError: ``not_found`` (with the saved names) if there is no such
            pipeline.
    """
    path = _path(directory, name)
    if not path.is_file():
        raise KernelError(
            "not_found",
            f"pipeline {name!r} not found",
            {"name": name, "saved": list[JsonValue](names(directory))},
        )
    path.unlink()
    return path


def names(directory: Path) -> list[str]:
    """Names of the saved pipelines, sorted."""
    return sorted(p.name[: -len(SUFFIX)] for p in directory.glob(f"*{SUFFIX}"))


def uses(spec: JsonObject) -> list[str]:
    """The pipelines a spec or document refers to directly."""
    try:
        return pipeline_refs(_parse(spec))
    except SpecError:
        return []


def users(directory: Path, name: str) -> list[str]:
    """The saved pipelines that use ``name``, directly or through others."""
    direct = {other: set(uses(load(directory, other))) for other in names(directory)}
    found: set[str] = set()
    todo = [name]
    while todo:
        target = todo.pop()
        for other, used in direct.items():
            if target in used and other not in found and other != name:
                found.add(other)
                todo.append(other)
    return sorted(found)


def closure(directory: Path, chosen: Sequence[str]) -> list[str]:
    """``chosen`` and the saved pipelines they use, transitively; sorted."""
    seen: set[str] = set()
    todo = list(chosen)
    while todo:
        name = todo.pop()
        if name in seen or not exists(directory, name):
            continue
        seen.add(name)
        todo.extend(uses(load(directory, name)))
    return sorted(seen)


def inline(
    directory: Path, spec: JsonObject, overrides: Mapping[str, JsonObject] | None = None
) -> JsonObject:
    """``spec`` (a document or a node) with its ``pipeline_ref`` nodes
    replaced by the saved pipelines they name; other fields of a document
    are kept. ``overrides`` stand for saved pipelines (a document being
    saved, so that a reference to itself is a cycle).

    Raises:
        KernelError: ``spec_error`` for an unknown pipeline, a cycle, an
            unknown ``bind`` key, ... (with the path of the reference).
    """
    parsed = _parse(spec)
    if not pipeline_refs(parsed):
        return spec
    extra = dict(overrides or {})

    def library(name: str) -> PipelineSpec:
        doc = extra.get(name)
        if doc is None:
            if not exists(directory, name):
                raise KeyError(name)
            doc = load(directory, name)
        return cast(PipelineSpec, _parse(doc))

    try:
        inlined = inline_refs(parsed, library)
    except SpecError as e:
        raise KernelError("spec_error", str(e), _error_data(e)) from None
    if isinstance(inlined, PipelineSpec):
        return {**spec, "root": inlined.root.to_dict()}
    return inlined.to_dict()


def _parse(spec: JsonObject) -> PipelineSpec | NodeSpec:
    if "root" in spec:
        root = spec["root"]
        return PipelineSpec(NodeSpec.from_dict(cast(dict[str, JsonValue], root)))
    return NodeSpec.from_dict(spec)


def _error_data(e: SpecError) -> JsonObject:
    return {"reason": e.reason, "path": str(e.path) if e.path is not None else None}


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
