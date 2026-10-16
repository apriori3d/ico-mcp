"""The MCP server (stdio): a fixed set of tools over the kernel.

The server never imports operator code: operators live in the kernel
process, and the server sees them only as JSON cards.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import JsonValue

from ico_mcp import pipelines, traces
from ico_mcp.config import Config, ConfigError, load_config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, KernelError

INSTRUCTIONS = """\
Build and run data pipelines from a registry of typed operators.

1. Look for a saved pipeline first (list_pipelines, get_pipeline).
2. Otherwise compose one from existing operators: search_operators step by
   step (by input_type / output_type / text / tags), then validate, then run.
   Specs are JSON: {"kind": "op", "op": "name@version", "params": {...}} for
   an operator, {"kind": "seq", "children": [...]} for a chain.
3. Write a new operator only when a step is missing. Keep operators small,
   typed and parametric; configuration goes into constructor params, not
   into closures; heavy models load lazily.
4. Pass data by reference (paths or URIs), never as bytes.
5. When a pipeline works and is likely to be reused, save_pipeline, then
   suggest crystallizing it to the human.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def create_server(config: Config) -> MCPServer[None]:
    """The ico-mcp server for a repository."""
    kernel = KernelClient(config)

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await kernel.aclose()

    server = MCPServer[None](config.name, instructions=INSTRUCTIONS, lifespan=lifespan)

    async def call(method: str, params: JsonObject) -> JsonObject:
        with tool_errors():
            return cast(JsonObject, await kernel.request(method, params))

    @server.tool(annotations=READ_ONLY)
    async def search_operators(
        text: str | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, JsonValue]:
        """Find operators; returns compact cards and the total match count.

        Types are names as cards show them in `sig`: `Report`, `list[str]`,
        or `module:qualname` when a short name is ambiguous.
        """
        params: JsonObject = {
            "text": text,
            "input_type": input_type,
            "output_type": output_type,
            "tags": list(tags or []),
            "limit": limit,
        }
        return await call("search", params)

    @server.tool(annotations=READ_ONLY)
    async def get_operator(ref: str) -> dict[str, JsonValue]:
        """The card and source code of an operator (`name@version`).

        Read the source only to write or fix an operator, not to use one.
        """
        return await call("card", {"ref": ref})

    @server.tool(annotations=READ_ONLY)
    async def validate(spec: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Check a spec without running it; issues carry spec paths."""
        return await call("validate", {"spec": spec})

    @server.tool(annotations=WRITES)
    async def run(
        spec: dict[str, JsonValue],
        input: JsonValue = None,
        max_items: int | None = None,
    ) -> dict[str, JsonValue]:
        """Run a spec once on a JSON input; returns outputs and per-item errors.

        Large data goes by reference: pass paths, get paths back.
        """
        params: JsonObject = {"spec": spec, "input": input}
        if max_items is not None:
            params["max_items"] = max_items
        started, clock = datetime.now(UTC), time.monotonic()
        result: JsonObject | None = None
        error: JsonObject | None = None
        try:
            result = cast(JsonObject, await kernel.request("run", params))
        except KernelError as e:
            error = e.to_dict()
        traces.write(
            config.runs_dir,
            started=started,
            duration=time.monotonic() - clock,
            spec=spec,
            input=input,
            result=result,
            error=error,
        )
        if error is not None:
            raise ToolError(json.dumps(error, ensure_ascii=False))
        return cast(JsonObject, result)

    @server.tool(annotations=WRITES)
    async def save_pipeline(
        name: str,
        spec: dict[str, JsonValue],
        description: str,
        replace: bool = False,
    ) -> dict[str, JsonValue]:
        """Save a spec under a name; it must validate without errors.

        An existing pipeline is overwritten only with `replace=true`.
        """
        doc = pipelines.document(cast(JsonObject, spec), description)
        with tool_errors():
            existed = pipelines.exists(config.pipelines_dir, name)
            if existed and not replace:
                raise pipelines.exists_error(name)
        checked = await call("validate", {"spec": doc})
        issues = cast(list[JsonObject], checked["issues"])
        errors = [issue for issue in issues if issue.get("severity") == "error"]
        if errors:
            raise ToolError(
                json.dumps(
                    {
                        "code": "invalid_pipeline",
                        "message": f"pipeline {name!r} does not validate",
                        "issues": cast(list[JsonValue], errors),
                    },
                    ensure_ascii=False,
                )
            )
        with tool_errors():
            path = pipelines.save(config.pipelines_dir, name, doc, replace=replace)
        return {
            "name": name,
            "path": str(path.relative_to(config.root)),
            "replaced": existed,
        }

    @server.tool(annotations=READ_ONLY)
    async def list_pipelines() -> dict[str, JsonValue]:
        """Saved pipelines with their descriptions."""
        directory = config.pipelines_dir
        items: list[JsonValue] = []
        for name in pipelines.names(directory):
            doc = pipelines.load(directory, name)
            items.append({"name": name, "description": pipelines.description(doc)})
        return {"pipelines": items}

    @server.tool(annotations=READ_ONLY)
    async def get_pipeline(name: str) -> dict[str, JsonValue]:
        """A saved pipeline: its description and spec document."""
        with tool_errors():
            doc = pipelines.load(config.pipelines_dir, name)
        return {
            "name": name,
            "description": pipelines.description(doc),
            "spec": {"version": doc.get("version"), "root": doc.get("root")},
        }

    @server.tool(annotations=WRITES)
    async def reload() -> dict[str, JsonValue]:
        """Restart the kernel and reload all operators (after a merge or edits).

        Returns the load report of the stable and the draft package.
        """
        with tool_errors():
            report = await kernel.start()
        return cast(JsonObject, report)

    return server


@contextmanager
def tool_errors() -> Generator[None]:
    """Report a ``KernelError`` to the agent as a JSON tool error."""
    try:
        yield
    except KernelError as e:
        raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None


# ─── Entry point ───


def default_root() -> Path:
    """The repository that contains this package (``src/ico_mcp``)."""
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ico-mcp", description="MCP server over an ICO operator registry."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root with ico.toml (default: the repository of this package)",
    )
    args = parser.parse_args(argv)
    root = cast(Path | None, args.root) or default_root()
    try:
        config = load_config(root)
    except ConfigError as e:
        hint = "" if args.root else "; pass --root <repository>"
        sys.exit(f"ico-mcp: {e}{hint}")
    anyio.run(create_server(config).run_stdio_async)
