"""Type-check one file with pyright, in the repository environment.

Pyright runs as a separate process (``python -m pyright``); nothing of the
checked file is imported by the server.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import cast

import anyio

from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject, JsonValue


class TypecheckError(Exception):
    """Pyright could not run or produced unreadable output."""


async def typecheck(config: Config, file: Path) -> list[JsonObject]:
    """Pyright diagnostics of ``file`` in the configured mode.

    Returns:
        ``{"file", "line", "column", "rule", "severity", "message"}`` per
        diagnostic; ``line`` and ``column`` are 1-based, ``file`` is relative
        to the repository root.

    Raises:
        TypecheckError: If pyright fails to run or times out.
    """
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "pyrightconfig.json"
        project.write_text(
            json.dumps(
                {
                    "typeCheckingMode": config.typecheck,
                    "extraPaths": [str(config.root)],
                }
            )
        )
        command = [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonpath",
            sys.executable,
            "-p",
            str(project),
            str(file),
        ]
        try:
            with anyio.fail_after(config.timeouts.validate):
                done = await anyio.run_process(command, cwd=config.root, check=False)
        except TimeoutError:
            raise TypecheckError(
                f"pyright timed out after {config.timeouts.validate:g} s"
            ) from None
    try:
        output = cast(JsonObject, json.loads(done.stdout))
    except ValueError:
        stderr = done.stderr.decode(errors="replace")[-2000:]
        raise TypecheckError(f"pyright failed: {stderr}") from None
    raw = output.get("generalDiagnostics")
    diagnostics = cast(list[JsonObject], raw if isinstance(raw, list) else [])
    return [_diagnostic(config, d) for d in diagnostics]


def _diagnostic(config: Config, raw: JsonObject) -> JsonObject:
    start = cast(
        JsonObject, cast(JsonObject, raw.get("range") or {}).get("start") or {}
    )
    file = Path(str(raw.get("file", ""))).resolve()
    return {
        "file": str(file.relative_to(config.root))
        if file.is_relative_to(config.root)
        else str(file),
        "line": _int(start.get("line")) + 1,
        "column": _int(start.get("character")) + 1,
        "rule": raw.get("rule"),
        "severity": raw.get("severity"),
        "message": raw.get("message"),
    }


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) else 0
