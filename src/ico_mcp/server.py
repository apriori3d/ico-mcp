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
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager
from importlib import resources
from pathlib import Path
from typing import Annotated, cast

import anyio
from ico.tools.board import BoardRuns, LiveBoard
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, JsonValue, WithJsonSchema

from ico_mcp import drafts, pipelines, scaffold
from ico_mcp.config import Config, ConfigError, find_root, load_config
from ico_mcp.crystallize import Crystallization, Crystallizations
from ico_mcp.crystallize import crystallize as open_pull_request
from ico_mcp.jobs import Jobs
from ico_mcp.kernel_client import KernelClient
from ico_mcp.progress import ProgressReporter
from ico_mcp.proposals import Edit, Example, ProposalError, propose
from ico_mcp.proposals import delete_module as remove_module
from ico_mcp.proposals import propose_types as stage_types
from ico_mcp.protocol import JsonObject, KernelError
from ico_mcp.version import DEFAULT_PACKAGES, ServerStatus

INSTRUCTIONS = """\
Build and run data pipelines from a registry of typed operators. The guide
tool returns the full reference (spec kinds and params, errors, writing
operators and types, runs); call it whenever these notes are not enough.

1. Look for a saved pipeline first (list_pipelines shows its parameters)
   and run it by name: run(pipeline=name, input=..., params={...}).
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
5. Pass data by reference (paths or URIs), never as bytes. Start a long run
   with run(wait=false); a run still going after about 45 s returns
   state "running" as well. Then call wait_run(run_id) until it is no
   longer running (no sleep loops; it waits at most about 45 s per call),
   and stop it with cancel_run if needed; sample_run(run_id, node) shows
   what flows out of a node meanwhile. Give the human the dashboard URL
   (in run results, or the dashboard tool): it shows the progress of every
   node and the pipeline plan.
6. When a pipeline works and is likely to be reused, save_pipeline, then
   suggest crystallizing it to the human: crystallize opens a pull request
   that makes its draft operators stable (still running after about 45 s:
   call wait_crystallize(crystallize_id) until it ends); after the merge,
   the human pulls and you call reload.
7. If a tool named here or in the guide is missing from your tool list,
   call server_status: a server process older than its code is reported
   as stale, and the human has to reconnect the server.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True)

GUIDE = "agent-guide.md"
"""The guide for agents, shipped with the package; the domain adds
``docs/domain.md``."""
"""The agent guide, relative to the repository root (a clone extends it)."""


JsonInput = Annotated[
    JsonValue,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "integer"},
                {"type": "boolean"},
                {"type": "object"},
                {"type": "array"},
                {"type": "null"},
            ],
            "description": "A JSON value; a path is a plain string, not quoted.",
        }
    ),
]
"""A JSON value with an explicit schema: without one, clients pass strings
with their quotes."""


class ModuleEdit(BaseModel):
    """Replace the only occurrence of `old` in the draft module by `new`."""

    old: str
    new: str


class OperatorExample(BaseModel):
    """Applying operator `ref` to `input` must return `output`."""

    ref: str
    input: JsonInput = None
    output: JsonInput = None


WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DELETES = ToolAnnotations(read_only_hint=False, destructive_hint=True)

WAIT_MIN = 1.0
"""Bounds of a wait_run timeout: clients have their own tool-call timeouts."""
SAMPLE_MAX = 20  # as the kernel's MAX_SAMPLES


def create_server(
    config: Config, *, code: Sequence[Path] = DEFAULT_PACKAGES
) -> MCPServer[None]:
    """The ico-mcp server for a repository.

    ``code`` are the packages whose fingerprint tells whether the code on
    disk is newer than the running process (``server_status``).
    """
    status = ServerStatus(config.root, code)
    kernel = KernelClient(config)
    proposing = anyio.Lock()
    runs = BoardRuns()
    board = LiveBoard(runs, host=config.board.host, port=config.board.port)
    jobs = Jobs(config, runs, kernel)
    crystallizations = Crystallizations()

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        drafts.retire(config)  # before the kernel loads anything
        board.start()
        try:
            async with jobs, crystallizations:
                yield
        finally:
            board.stop()
            await kernel.aclose()

    server = MCPServer[None](
        config.name,
        instructions=instructions(status.header, config),
        lifespan=lifespan,
    )

    async def server_report() -> JsonObject:
        return status.report([tool.name for tool in await server.list_tools()])

    async def check(doc: JsonObject) -> JsonObject:
        """Validate a pipeline document with its ``check_params``."""
        request: JsonObject = {"spec": doc}
        values = pipelines.check_params(doc)
        if values is not None:
            request["params"] = values
        return await call("validate", request)

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

        Read the source only to write or fix an operator, not to use one. A
        draft operator also has `module_source`, the text of its whole
        module, to write `edits` for propose_operator.
        """
        return await call("card", {"ref": ref})

    @server.tool(annotations=WRITES)
    async def propose_operator(
        module: str,
        code: str | None = None,
        edits: list[ModuleEdit] | None = None,
        typecheck: bool = True,
        examples: list[OperatorExample] | None = None,
    ) -> dict[str, JsonValue]:
        """Add or replace a draft operator module, then reload the operators.

        Give the whole module as `code`, or, for a small change of an
        existing draft, `edits`: `[{"old", "new"}]` replacements applied in
        order, each `old` occurring exactly once (see `module_source` of
        get_operator). Stages: write `<draft package>/<module>.py`,
        typecheck (pyright), load, register (at least one operator, no `Any`
        in signatures), examples. The first failing stage is reported with
        diagnostics; the file stays so that it can be fixed with another
        proposal (a failed edit changes nothing). Operator source does not
        set `status`: drafts are `draft`.
        """
        given = [Example(e.ref, e.input, e.output) for e in examples or []]
        async with proposing:
            try:
                return await propose(
                    config,
                    kernel,
                    module=module,
                    code=code,
                    edits=_edits(edits),
                    check_types=typecheck,
                    examples=given,
                )
            except ProposalError as e:
                raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None

    @server.tool(annotations=WRITES)
    async def propose_types(
        module: str,
        code: str | None = None,
        edits: list[ModuleEdit] | None = None,
        typecheck: bool = True,
    ) -> dict[str, JsonValue]:
        """Add or replace a draft module of domain types, then reload.

        Give the whole module as `code`, or `edits` for a small change of an
        existing draft (as for propose_operator). Stages: write
        `<draft package>/<module>.py`, typecheck (pyright), load, types (at
        least one public class, no operators). Operator modules import the
        types relatively: `from .<module> import Name`.
        """
        async with proposing:
            try:
                return await stage_types(
                    config,
                    kernel,
                    module=module,
                    code=code,
                    edits=_edits(edits),
                    check_types=typecheck,
                )
            except ProposalError as e:
                raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None

    @server.tool(annotations=DELETES)
    async def delete_module(module: str) -> dict[str, JsonValue]:
        """Remove a draft module (operators or types) and the examples of
        its operators, then reload. Reports the saved pipelines that used
        its operators (`changes.removed`, each with its `pipelines`) and
        validates them again (`pipelines`). Refused for a stable module and
        for a module other drafts import (`imported_by`: change them first).
        Use it when a refactoring moves operators to other modules: a new
        module cannot register a name an old one still holds."""
        async with proposing:
            try:
                return await remove_module(config, kernel, module)
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
    async def validate(
        spec: dict[str, JsonValue], params: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Check a spec without running it; issues carry spec paths.

        `params` gives values for the pipeline parameters (`{"$param": ...}`
        references) without a default; missing ones are reported. The
        result also lists the parameters the spec declares.
        """
        request: JsonObject = {"spec": spec}
        if params is not None:
            request["params"] = params
        return await call("validate", request)

    @server.tool(annotations=WRITES)
    async def run(
        ctx: Context,
        spec: dict[str, JsonValue] | None = None,
        pipeline: str | None = None,
        input: JsonInput = None,
        params: dict[str, JsonValue] | None = None,
        max_items: int | None = None,
        wait: bool = True,
        profile: bool = False,
    ) -> dict[str, JsonValue]:
        """Run a spec, or a saved pipeline by name, once on a JSON input;
        returns outputs and per-item errors.

        Give exactly one of `spec` and `pipeline`. `params` gives values for
        the pipeline parameters (`{"$param": ...}` references; defaults
        apply to the others). Pass a string input as is, e.g. /data/a.mp4,
        not quoted. Large data goes by reference: pass paths, get paths
        back. Each run has a kernel process of its own. With `wait=false`
        the call returns the `run_id` at once: poll `run_status`, stop with
        `cancel_run`. A run longer than `timeouts.wait` (45 s by default)
        also returns `state: running` (with `waited`) and goes on in the
        background: call `wait_run(run_id)` until it ends. With a progress
        token, the first progress node of the flow is reported. `dashboard` is the URL of the page with the
        progress of every node.

        `profile=true` measures the time of every node (about 4 µs per call
        or item): use it when a run is slow. The result's `profile.top` lists
        the nodes with the largest self time, with `first call` (lazy setup:
        a first call far slower than the others), `util` (pool utilization) and `transfer` (worker
        overhead) in `info`; the dashboard shows every node on the plan.
        """
        if (spec is None) == (pipeline is None):
            raise ToolError(
                json.dumps(
                    {
                        "code": "bad_request",
                        "message": "give exactly one of spec and pipeline",
                    }
                )
            )
        _check_input(input)
        if pipeline is not None:
            with tool_errors():
                spec = _saved(config, pipeline)
        assert spec is not None
        reporter = ProgressReporter()

        async def on_progress(snapshot: JsonObject) -> None:
            status = reporter.update(snapshot)
            if status is not None:
                # A no-op unless the client sent a progress token.
                await ctx.report_progress(status.progress, status.total, status.message)

        job = jobs.start(
            spec,
            input,
            max_items,
            on_progress if wait else None,
            pipeline=pipeline,
            values=params,
            profile=profile,
        )
        if not wait:
            return {"run_id": job.id, "state": "running", "dashboard": board.url}
        waited = config.timeouts.wait
        with anyio.move_on_after(waited):
            await job.finished.wait()
        if not job.finished.is_set():
            # Longer than a tool call may last: the run goes on in the
            # background, followed with wait_run.
            job.listeners.remove(on_progress)
            return {
                "run_id": job.id,
                "state": "running",
                "waited": waited,
                "dashboard": board.url,
            }
        if job.error is not None:
            error = {**job.error, "run_id": job.id, "dashboard": board.url}
            raise ToolError(json.dumps(error, ensure_ascii=False))
        assert job.result is not None
        return {**job.result, "run_id": job.id, "dashboard": board.url}

    @server.tool(annotations=READ_ONLY)
    async def run_status(run_id: str) -> dict[str, JsonValue]:
        """The state and progress of a run, as on the dashboard; a finished
        run also has its `result` or its `kernel_error`."""
        with tool_errors():
            return jobs.get(run_id).status()

    @server.tool(annotations=READ_ONLY)
    async def wait_run(
        ctx: Context, run_id: str, timeout: float | None = None
    ) -> dict[str, JsonValue]:
        """Wait until a run ends, or `timeout` seconds pass; then return its
        status, as run_status does. `timeout` is at most `timeouts.wait` (45
        s by default, below the client's tool-call timeout) and defaults to
        it; a longer one is cut to it. Still `running` (with `waited`): call
        wait_run again (no sleep loops needed). With a progress token, the
        first progress node is reported while waiting."""
        limit = config.timeouts.wait
        if timeout is not None and timeout < WAIT_MIN:
            raise ToolError(
                json.dumps(
                    {
                        "code": "bad_request",
                        "message": f"timeout must be at least {WAIT_MIN:g} s",
                    }
                )
            )
        waited = min(timeout if timeout is not None else limit, limit)
        with tool_errors():
            job = jobs.get(run_id)
        reporter = ProgressReporter()

        async def on_progress(snapshot: JsonObject) -> None:
            status = reporter.update(snapshot)
            if status is not None:
                # A no-op unless the client sent a progress token.
                await ctx.report_progress(status.progress, status.total, status.message)

        # The board keeps nodes in flow order: start from the first one.
        for node in job.board.view()["progress"][:1]:
            await on_progress(cast(JsonObject, dict(node)))
        job.listeners.append(on_progress)
        try:
            with anyio.move_on_after(waited):
                await job.finished.wait()
        finally:
            job.listeners.remove(on_progress)
        status = job.status()
        if not job.finished.is_set():
            status["waited"] = waited
        return status

    @server.tool(annotations=WRITES)
    async def cancel_run(run_id: str) -> dict[str, JsonValue]:
        """Stop a run between items (in workers, also between the inner items
        of a long item): workers stop cleanly, and the status has the outputs
        produced so far (`result`, with `cancelled`) and `kernel_error`
        "cancelled". A run that has not stopped within 10 s (one long
        operator call) is killed, without a result. A finished run is left
        as is. Returns the run status."""
        with tool_errors():
            job = jobs.cancel(run_id)
        await job.finished.wait()
        return job.status()

    @server.tool(annotations=READ_ONLY)
    async def sample_run(
        run_id: str, node: str, count: int = 3
    ) -> dict[str, JsonValue]:
        """Show what flows out of a node of a running run: the next `count`
        (1 to 20) results of `node`, a plan key ("0.1.2") or a label
        ("resize@1", or "resize" when one version is used), as short
        previews (`type`, `preview`). A stream or source previews as an
        iterator: sample the operator inside the stream. Waits at most
        `timeouts.wait`, or until the run ends (`complete` false: fewer
        came). An unknown or ambiguous label lists the plan's nodes."""
        if not 1 <= count <= SAMPLE_MAX:
            raise ToolError(
                json.dumps(
                    {
                        "code": "bad_request",
                        "message": f"count must be from 1 to {SAMPLE_MAX}",
                    }
                )
            )
        with tool_errors():
            return await jobs.sample(run_id, node, count)

    @server.tool(annotations=READ_ONLY)
    async def guide() -> str:
        """The guide for agents: workflow, every spec kind with its params,
        error formats, how to write operators and types, runs, and the notes
        of this domain (its pipelines, workflow and conventions). Markdown."""
        return guide_text(config)

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
        params: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        """Save a spec under a name; it must validate without errors.

        The spec keeps its `{"$param": ...}` references. `params` gives
        values for the parameters without a default: they are needed to
        validate the pipeline, saved as its `check_params` and never used as
        run values. An existing pipeline is overwritten only with
        `replace=true`.
        """
        doc = pipelines.document(cast(JsonObject, spec), description, params)
        with tool_errors():
            existed = pipelines.exists(config.pipelines_dir, name)
            if existed and not replace:
                raise pipelines.exists_error(name)
        checked = await check(doc)
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
            "params": checked.get("params"),
        }

    @server.tool(annotations=READ_ONLY)
    async def list_pipelines() -> dict[str, JsonValue]:
        """Saved pipelines with their descriptions and parameters (name,
        required, default)."""
        directory = config.pipelines_dir
        items: list[JsonValue] = []
        for name in pipelines.names(directory):
            doc = pipelines.load(directory, name)
            item: JsonObject = {"name": name, "description": pipelines.description(doc)}
            try:
                declared = (await check(doc)).get("params")
            except ToolError as e:  # e.g. an operator was removed
                item["params"], item["error"] = None, str(e)
            else:
                item["params"] = [
                    {k: p[k] for k in ("name", "required", "default") if k in p}
                    for p in cast(list[JsonObject], declared or [])
                ]
            items.append(item)
        return {"pipelines": items}

    @server.tool(annotations=READ_ONLY)
    async def get_pipeline(name: str) -> dict[str, JsonValue]:
        """A saved pipeline: its description, spec document, the parameters
        it declares and the values that validate it (`check_params`)."""
        with tool_errors():
            doc = pipelines.load(config.pipelines_dir, name)
        try:
            declared = (await check(doc)).get("params")
        except ToolError:
            declared = None
        return {
            "name": name,
            "description": pipelines.description(doc),
            "spec": {"version": doc.get("version"), "root": doc.get("root")},
            "params": declared,
            "check_params": pipelines.check_params(doc),
        }

    @server.tool(annotations=DELETES)
    async def delete_pipeline(name: str) -> dict[str, JsonValue]:
        """Delete a saved pipeline (its file in the pipelines directory).

        A pipeline already in a crystallization branch stays in that branch.
        """
        with tool_errors():
            path = pipelines.delete(config.pipelines_dir, name)
        return {"name": name, "path": str(path.relative_to(config.root))}

    @server.tool(annotations=WRITES)
    async def crystallize(
        refs: list[str],
        title: str,
        summary: str,
        pipelines: list[str] | None = None,
    ) -> dict[str, JsonValue]:
        """Open a GitHub pull request that makes draft operators stable.

        Moves the modules of `refs` (and the draft modules they import
        relatively) into the stable package with their examples and the
        given saved `pipelines`, in a branch of its own; the working tree is
        not changed. A gate (load, pyright strict, no draft imports,
        examples, validate pipelines) must pass before anything is pushed.
        Returns the pull request URL and the gate report. A crystallization
        longer than `timeouts.wait` (45 s by default) returns `state:
        running` with a `crystallize_id` and its `stage` (check, git, gate,
        publish) and goes on in the background: call
        `wait_crystallize(crystallize_id)` until it ends. Ask the human
        first: this pushes a branch and opens a pull request.
        """
        job = crystallizations.start(
            lambda on_stage: open_pull_request(
                config,
                kernel,
                refs=refs,
                title=title,
                summary=summary,
                pipeline_names=pipelines or [],
                drafts_lock=proposing,
                on_stage=on_stage,
            )
        )
        return await crystallize_outcome(job, config.timeouts.wait)

    @server.tool(annotations=READ_ONLY)
    async def wait_crystallize(
        crystallize_id: str, timeout: float | None = None
    ) -> dict[str, JsonValue]:
        """Wait until a crystallization ends, or `timeout` seconds pass (at
        most `timeouts.wait`, the default); then return what crystallize
        returns: the pull request, a failed stage as an error, or `state:
        running` with its `stage` and `waited` (call again)."""
        if timeout is not None and timeout < WAIT_MIN:
            raise ToolError(
                json.dumps(
                    {
                        "code": "bad_request",
                        "message": f"timeout must be at least {WAIT_MIN:g} s",
                    }
                )
            )
        limit = config.timeouts.wait
        with tool_errors():
            job = crystallizations.get(crystallize_id)
        return await crystallize_outcome(
            job, min(timeout if timeout is not None else limit, limit)
        )

    async def crystallize_outcome(
        job: Crystallization, waited: float
    ) -> dict[str, JsonValue]:
        with anyio.move_on_after(waited):
            await job.finished.wait()
        if job.error is not None:
            error = {**job.error, "crystallize_id": job.id}
            raise ToolError(json.dumps(error, ensure_ascii=False))
        if job.result is None:
            return job.running(waited)
        return {**job.result, "crystallize_id": job.id, "state": "done"}

    @server.tool(annotations=WRITES)
    async def reload() -> dict[str, JsonValue]:
        """Restart the kernel and reload all operators (after a merge or edits).

        Drafts crystallized by a merged pull request are retired first.
        Returns the load report of the stable and the draft package, and
        `drafts`: retired drafts, drafts whose imports were rewritten to the
        stable package, and conflicts (a draft that differs from its stable
        module). `server` is the `server_status` report: the kernel loads
        new operator code, but a `stale` server needs a reconnect.
        """
        retired = drafts.retire(config)
        with tool_errors():
            report = await kernel.start()
        return {
            **cast(JsonObject, report),
            "drafts": retired,
            "server": await server_report(),
        }

    @server.tool(annotations=READ_ONLY)
    async def environment(
        match: str | list[str] | None = None,
    ) -> dict[str, JsonValue]:
        """What operators see: Python, PATH, allowed environment variables
        (whether set, never values), timeouts, installed packages (`match`:
        a part of the name, or a list of parts, e.g. ["torch", "diffusers"];
        not a pattern), duplicates (a package installed by several
        distributions, e.g. three OpenCV builds) and where the binaries of
        `bin:<name>` capabilities resolve."""
        request: JsonObject = {}
        if match is not None:
            request["match"] = (
                [match] if isinstance(match, str) else list[JsonValue](match)
            )
        return await call("environment", request)

    @server.tool(annotations=READ_ONLY)
    async def server_status() -> dict[str, JsonValue]:
        """The code this server process runs and the code on disk.

        `stale` is true when the server's code changed on disk since the
        process started (e.g. after a git pull): its tools are old until the
        human reconnects (restarts) the MCP server. `tools` lists the tools
        of this process.
        """
        return await server_report()

    return server


def _check_input(value: JsonValue) -> None:
    """Reject a string that is itself JSON-encoded (``"\"/data/a.mp4\""``):
    the operator would get the quotes."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        if isinstance(decoded, str):
            message = (
                "input is a JSON-encoded string; pass the string itself, "
                f"without quotes: {decoded}"
            )
            raise ToolError(json.dumps({"code": "bad_request", "message": message}))


def _edits(edits: list[ModuleEdit] | None) -> list[Edit] | None:
    return None if edits is None else [Edit(e.old, e.new) for e in edits]


def _saved(config: Config, name: str) -> JsonObject:
    """The document of a saved pipeline; ``not_found`` lists the saved ones."""
    try:
        return pipelines.load(config.pipelines_dir, name)
    except KernelError as e:
        if e.code != "not_found":
            raise
        saved: list[JsonValue] = []
        saved.extend(pipelines.names(config.pipelines_dir))
        raise KernelError(e.code, e.message, {"name": name, "saved": saved}) from None


@contextmanager
def tool_errors() -> Generator[None]:
    """Report a ``KernelError`` to the agent as a JSON tool error."""
    try:
        yield
    except KernelError as e:
        raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None


def instructions(header: str, config: Config) -> str:
    """The server instructions: the generic ones, then what the domain is
    about (``about`` in ``ico.toml``)."""
    text = f"{header}\n\n{INSTRUCTIONS}"
    if config.about:
        text += (
            f"\nThis domain ({config.name}): {config.about}\n"
            "Its pipelines, workflow and conventions: the guide tool, "
            'section "Domain notes".\n'
        )
    return text


def guide_text(config: Config) -> str:
    """The package guide followed by the domain's ``docs/domain.md``."""
    guide = resources.files("ico_mcp").joinpath(GUIDE).read_text(encoding="utf-8")
    notes = config.domain_notes
    if notes.is_file():
        return f"{guide.rstrip()}\n\n{notes.read_text(encoding='utf-8').strip()}\n"
    return f"{guide.rstrip()}\n\nThis domain has no notes yet (`docs/domain.md`).\n"


# ─── Entry point ───


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        init_main(argv[1:])
        return
    parser = argparse.ArgumentParser(
        prog="ico-mcp",
        description="MCP server over the ICO operators of a domain repository "
        "(`ico-mcp init <dir>` creates one).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="domain repository with ico.toml (default: the nearest one "
        "from the current directory up)",
    )
    args = parser.parse_args(argv)
    root = cast(Path | None, args.root) or find_root(Path.cwd())
    if root is None:
        sys.exit(
            "ico-mcp: no ico.toml in the current directory or its parents; "
            "pass --root <domain repository> or create one with "
            "`ico-mcp init <dir>`"
        )
    try:
        config = load_config(root)
    except ConfigError as e:
        sys.exit(f"ico-mcp: {e}")
    anyio.run(create_server(config).run_stdio_async)


def init_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="ico-mcp init", description="Create an empty domain repository."
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--name", default=None, help="default: the directory name")
    parser.add_argument("--package", default="domain", help="stable package name")
    args = parser.parse_args(argv)
    try:
        written = scaffold.init(
            cast(Path, args.directory),
            name=cast(str | None, args.name),
            package=cast(str, args.package),
        )
    except ValueError as e:
        sys.exit(f"ico-mcp init: {e}")
    for path in written:
        print(path)
