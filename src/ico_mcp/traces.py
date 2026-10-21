"""Run traces: one JSON line per ``run`` in ``runs/YYYY-MM-DD.jsonl``.

Traces feed ``crystallize`` (runs and successes per operator), so failed
runs are recorded too. Results are summarized to keep the files small.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ico_mcp.protocol import JsonObject, JsonValue

MAX_OUTPUTS = 10
MAX_STRING = 1000


def write(
    directory: Path,
    *,
    started: datetime,
    duration: float,
    spec: JsonObject,
    input: JsonValue,
    result: JsonObject | None,
    error: JsonObject | None,
    progress: list[JsonValue],
    pipeline: str | None = None,
    params: JsonObject | None = None,
    drafts: JsonObject | None = None,
    profile: list[JsonValue] | None = None,
    resources: list[JsonValue] | None = None,
) -> Path:
    """Append a trace line for one run; ``started`` must be timezone-aware.

    ``progress`` is the final state of each progress node; ``pipeline`` the
    name of a saved pipeline run by name; ``params`` the values given for
    its parameters; ``drafts`` the fingerprint of each draft operator's
    module (which draft code the run used); ``profile`` the profile of
    every node of a profiled run (``profiles.trace``); ``resources`` its
    resource rows (``profiles.resources``).
    """
    started = started.astimezone(UTC)
    trace: JsonObject = {
        "ts": started.isoformat().replace("+00:00", "Z"),
        "duration": round(duration, 3),
        "refs": _strings(op_refs(spec)),
        "spec": spec,
        "input": _shorten(input),
        "pipeline": pipeline,
        "params": _shorten(params) if params is not None else None,
        "drafts": drafts,
        "ok": error is None,
        "result": _summary(result) if result is not None else None,
        "error": cast(JsonObject, _shorten(error)) if error is not None else None,
        "progress": progress,
        "profile": profile,
    }
    if resources:
        trace["resources"] = resources
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{started:%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")
    return path


def op_refs(spec: JsonValue) -> list[str]:
    """Registry references (``op`` fields) used anywhere in a spec, sorted."""
    refs: set[str] = set()

    def walk(value: JsonValue) -> None:
        if isinstance(value, dict):
            ref = value.get("op")
            if isinstance(ref, str):
                refs.add(ref)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)
    return sorted(refs)


def _summary(result: JsonObject) -> JsonObject:
    outputs = result.get("outputs")
    errors = result.get("errors")
    return {
        "total": result.get("total"),
        "truncated": result.get("truncated"),
        "outputs": _shorten(
            outputs[:MAX_OUTPUTS] if isinstance(outputs, list) else outputs
        ),
        "errors": len(errors) if isinstance(errors, list) else 0,
    }


def _shorten(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if isinstance(value, list):
        return [_shorten(v) for v in value]
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    return value


def _strings(values: list[str]) -> list[JsonValue]:
    out: list[JsonValue] = []
    out.extend(values)
    return out
