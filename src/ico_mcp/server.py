"""The MCP server (stdio): a fixed set of tools over the kernel.

The server never imports operator code: operators live in the kernel
process, and the server sees them only as JSON cards. It hosts the ICO Live
Board (the dashboard) with the runs it starts, so the board's URL and
history survive kernel restarts.
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
from ico import PlanView
from ico.tools.board import BoardRun, BoardRuns, LiveBoard  # pyright: ignore[reportMissingTypeStubs]  # ico.tools is a namespace package
from ico.tools.board.runs import ProgressSnapshot  # pyright: ignore[reportMissingTypeStubs]
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, JsonValue

from ico_mcp import pipelines, traces
from ico_mcp.config import Config, ConfigError, load_config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.progress import ProgressReporter
from ico_mcp.proposals import Example, ProposalError, propose
from ico_mcp.proposals import propose_types as stage_types
from ico_mcp.protocol import JsonObject, KernelError

INSTRUCTIONS = """\
Build and run data pipelines from a registry of typed operators.

1. Look for a saved pipeline first (list_pipelines, get_pipeline).
2. Otherwise compose one from existing operators: search_operators step by
   step (by input_type / output_type / text / tags), then validate, then run.
   Specs are JSON: {"kind": "op", "op": "name@version", "params": {...}} for
   an operator, {"kind": "seq", "children": [...]} for a chain. Example:
   split words, then upper-case each word, counting words as progress:
   {"kind": "seq", "children": [{"kind": "op", "op": "words@1"},
    {"kind": "stream", "children": [{"kind": "seq", "children": [
     {"kind": "progress", "params": {"total": 40}},
     {"kind": "op", "op": "upper@1"}]}]}]}
   A progress node counts the items that pass it: put it inside the stream.
3. Write a new operator (propose_operator) only when a step is missing.
   Keep operators small, typed and parametric; configuration goes into
   constructor params, not into closures; heavy models load lazily. Give
   examples for operators with JSON inputs and outputs.
4. Reuse domain types (list_types, get_type) before creating one with
   propose_types, in a types-only module. Types are immutable dataclasses
   or Pydantic models with a docstring. Large data crosses the pipeline
   boundary by reference (e.g. an ImageRef with a path); inside a pipeline
   it may stay in memory. Draft modules import each other relatively
   (from .frames import Frame) and stable modules absolutely
   (from <package>.frames import Frame).
5. Pass data by reference (paths or URIs), never as bytes. For a long run,
   give the human the dashboard URL (in run results, or the dashboard tool):
   it shows the progress of every node and the pipeline plan.
6. When a pipeline works and is likely to be reused, save_pipeline, then
   suggest crystallizing it to the human.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True)


class OperatorExample(BaseModel):
    """Applying operator `ref` to `input` must return `output`."""

    ref: str
    input: JsonValue = None
    output: JsonValue = None


WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def create_server(config: Config) -> MCPServer[None]:
    """The ico-mcp server for a repository."""
    kernel = KernelClient(config)
    proposing = anyio.Lock()
    runs = BoardRuns()
    board = LiveBoard(runs)

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        board.start()
        try:
            yield
        finally:
            board.stop()
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

    @server.tool(annotations=WRITES)
    async def propose_operator(
        module: str,
        code: str,
        typecheck: bool = True,
        examples: list[OperatorExample] | None = None,
    ) -> dict[str, JsonValue]:
        """Add or replace a draft operator module, then reload the operators.

        Stages: write `<draft package>/<module>.py`, typecheck (pyright),
        load, register (at least one operator, no `Any` in signatures),
        examples. The first failing stage is reported with diagnostics; the
        file stays so that it can be fixed with another proposal. Operator
        source does not set `status`: drafts are `draft`.
        """
        given = [Example(e.ref, e.input, e.output) for e in examples or []]
        async with proposing:
            try:
                return await propose(
                    config,
                    kernel,
                    module=module,
                    code=code,
                    check_types=typecheck,
                    examples=given,
                )
            except ProposalError as e:
                raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None

    @server.tool(annotations=WRITES)
    async def propose_types(
        module: str, code: str, typecheck: bool = True
    ) -> dict[str, JsonValue]:
        """Add or replace a draft module of domain types, then reload.

        Stages: write `<draft package>/<module>.py`, typecheck (pyright),
        load, types (at least one public class, no operators). Operator
        modules import the types relatively: `from .<module> import Name`.
        """
        async with proposing:
            try:
                return await stage_types(
                    config, kernel, module=module, code=code, check_types=typecheck
                )
            except ProposalError as e:
                raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None

    @server.tool(annotations=READ_ONLY)
    async def list_types() -> dict[str, JsonValue]:
        """Domain types (stable and draft) as compact cards, used by a
        signature or not."""
        return await call("types", {})

    @server.tool(annotations=READ_ONLY)
    async def get_type(name: str) -> dict[str, JsonValue]:
        """A domain type with its module, docstring and fields; `name` as
        in search (`Frame` or `module:qualname`)."""
        return await call("type", {"name": name})

    @server.tool(annotations=READ_ONLY)
    async def validate(spec: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Check a spec without running it; issues carry spec paths."""
        return await call("validate", {"spec": spec})

    @server.tool(annotations=WRITES)
    async def run(
        ctx: Context,
        spec: dict[str, JsonValue],
        input: JsonValue = None,
        max_items: int | None = None,
    ) -> dict[str, JsonValue]:
        """Run a spec once on a JSON input; returns outputs and per-item errors.

        Large data goes by reference: pass paths, get paths back. With a
        progress token, the first progress node of the flow is reported.
        `dashboard` is the URL of the page with the progress of every node.
        """
        params: JsonObject = {"spec": spec, "input": input}
        if max_items is not None:
            params["max_items"] = max_items
        reporter = ProgressReporter()
        live = runs.start(", ".join(traces.op_refs(spec)) or "run")

        async def on_notification(message: JsonObject) -> None:
            body = message.get("params")
            if not isinstance(body, dict):
                return
            plan, snapshot = body.get("plan"), body.get("snapshot")
            if message.get("method") == "plan" and isinstance(plan, dict):
                live.plan = cast(PlanView, plan)
            if message.get("method") != "progress" or not isinstance(snapshot, dict):
                return
            live.progress(cast(ProgressSnapshot, snapshot))
            status = reporter.update(snapshot)
            if status is not None:
                # A no-op unless the client sent a progress token.
                await ctx.report_progress(status.progress, status.total, status.message)

        started, clock = datetime.now(UTC), time.monotonic()
        result: JsonObject | None = None
        error: JsonObject | None = None
        try:
            result = cast(
                JsonObject,
                await kernel.request("run", params, on_notification=on_notification),
            )
        except KernelError as e:
            error = e.to_dict()
        live.finish(
            str(error.get("message")) if error is not None else None,
            summary=_summary(result) if result is not None else None,
        )
        traces.write(
            config.runs_dir,
            started=started,
            duration=time.monotonic() - clock,
            spec=spec,
            input=input,
            result=result,
            error=error,
            progress=_final_progress(live),
        )
        if error is not None:
            error = {**error, "dashboard": board.url}
            raise ToolError(json.dumps(error, ensure_ascii=False))
        return {**cast(JsonObject, result), "dashboard": board.url}

    @server.tool(annotations=READ_ONLY)
    async def dashboard() -> dict[str, JsonValue]:
        """The URL of the dashboard: progress of every node and the pipeline
        plan of active and recent runs. Give it to the human to open."""
        return {"url": board.url}

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


def _summary(result: JsonObject) -> str:
    """One line for the dashboard: outputs, item errors, truncation."""
    total, errors = result.get("total"), result.get("errors")
    count = len(errors) if isinstance(errors, list) else 0
    parts = [f"{total} output(s)", f"{count} item error(s)"]
    if result.get("truncated"):
        parts.append("truncated")
    return " · ".join(parts)


def _final_progress(run: BoardRun) -> list[JsonValue]:
    """The final state of every progress node, for the run trace."""
    out: list[JsonValue] = []
    for node in run.view()["progress"]:
        out.append({k: node[k] for k in ("key", "label", "done", "total")})
    return out


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
