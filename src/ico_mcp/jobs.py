"""Runs started by the server, each in a kernel process of its own.

ICO cannot interrupt a running flow, so a run is cancelled by killing its
kernel (and the kernel's worker processes). A kernel per run also keeps the
main kernel free for search, validate and proposals while a run is going,
and a reload does not cut runs short: they finish on the code they loaded.

Every run is a run of the dashboard (its ``BoardRun``), so the run id is the
same on the board and in ``run_status``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, cast

import anyio
from anyio.abc import TaskGroup
from ico import PlanView
from ico.tools.board import BoardRun, BoardRuns
from ico.tools.board.runs import ProgressSnapshot
from ico.tools.profiler import ProfileSnapshot

from ico_mcp import profiles, traces
from ico_mcp.config import Config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError

OnProgress = Callable[[JsonObject], Awaitable[None]]
"""Receives every progress snapshot of a run (e.g. for MCP progress)."""

CANCELLED: JsonObject = {"code": "cancelled", "message": "the run was cancelled"}
TITLE_MAX = 120


class Job:
    """One run: its kernel, its dashboard run and, once finished, its outcome."""

    def __init__(self, kernel: KernelClient, board: BoardRun) -> None:
        self.kernel = kernel
        self.board = board
        self.result: JsonObject | None = None
        self.error: JsonObject | None = None
        self.cancelled = False
        self.finished = anyio.Event()
        self.listeners: list[OnProgress] = []
        """Receive the run's progress snapshots (``run(wait=true)``,
        ``wait_run``); added and removed while the run goes."""

    @property
    def id(self) -> str:
        return self.board.id

    def status(self) -> JsonObject:
        """The dashboard snapshot of the run; when finished, also its
        ``result`` or its structured ``kernel_error``."""
        status = cast(JsonObject, dict(self.board.view()))
        if self.result is not None:
            status["result"] = self.result
        if self.error is not None:
            status["kernel_error"] = self.error
        return status


class Jobs:
    """Active and recent runs; use as an async context manager (the server
    lifespan), which kills the kernels of active runs on exit."""

    def __init__(
        self, config: Config, runs: BoardRuns, catalog: KernelClient | None = None
    ) -> None:
        """``catalog`` is the main kernel (all operators loaded): it tells
        which modules a run needs, so the run's kernel loads only those."""
        self.config = config
        self.runs = runs
        self.catalog = catalog
        self._jobs: dict[str, Job] = {}
        self._tasks: TaskGroup | None = None

    async def __aenter__(self) -> Self:
        self._tasks = anyio.create_task_group()
        await self._tasks.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for job in self._jobs.values():
            if not job.finished.is_set():
                job.cancelled = True
                job.kernel.kill()
        tasks, self._tasks = self._tasks, None
        if tasks is not None:
            await tasks.__aexit__(exc_type, exc, tb)

    def start(
        self,
        spec: JsonObject,
        input: JsonValue,
        max_items: int | None = None,
        on_progress: OnProgress | None = None,
        *,
        pipeline: str | None = None,
        values: JsonObject | None = None,
        profile: bool = False,
    ) -> Job:
        """Start a run in a new kernel; it finishes in the background.

        ``pipeline`` names a saved pipeline run by name; ``values`` are the
        values of the pipeline parameters; ``profile`` measures every node
        (the dashboard shows it on the plan, the result has a summary).
        """
        if self._tasks is None:
            raise RuntimeError("Jobs is not entered")
        job = Job(
            KernelClient(self.config),
            self.runs.start(_title(spec, pipeline, values)),
        )
        self._forget_evicted()
        self._jobs[job.id] = job
        params: JsonObject = {"spec": spec, "input": input}
        if max_items is not None:
            params["max_items"] = max_items
        if values is not None:
            params["params"] = values
        if profile:
            params["profile"] = True
        if on_progress is not None:
            job.listeners.append(on_progress)
        self._tasks.start_soon(self._run, job, params, pipeline)
        return job

    def get(self, run_id: str) -> Job:
        """A run by id.

        Raises:
            KernelError: ``not_found`` for an unknown or evicted run.
        """
        job = self._jobs.get(run_id)
        if job is None:
            raise KernelError(
                "not_found",
                f"no run {run_id!r}; only active and recent runs are kept",
                {"run_id": run_id},
            )
        return job

    def cancel(self, run_id: str) -> Job:
        """Kill the kernel of a running run; a finished run is left as is."""
        job = self.get(run_id)
        if not job.finished.is_set():
            job.cancelled = True
            job.kernel.kill()
        return job

    async def _run(
        self,
        job: Job,
        params: JsonObject,
        pipeline: str | None,
    ) -> None:
        started, clock = datetime.now(UTC), time.monotonic()

        async def on_notification(message: JsonObject) -> None:
            body = message.get("params")
            if not isinstance(body, dict):
                return
            plan, snapshot = body.get("plan"), body.get("snapshot")
            method = message.get("method")
            if method == "plan" and isinstance(plan, dict):
                job.board.plan = cast(PlanView, plan)
            elif method == "progress" and isinstance(snapshot, dict):
                job.board.progress(cast(ProgressSnapshot, snapshot))
                for listener in list(job.listeners):
                    await listener(snapshot)
            elif method == "profile" and isinstance(snapshot, dict):
                job.board.profile(cast(ProfileSnapshot, snapshot))

        drafts: JsonValue = None
        profile: ProfileSnapshot | None = None
        job.kernel.modules = await self._modules(cast(JsonObject, params["spec"]))
        async with job.kernel:
            try:
                await job.kernel.start()
                if not job.cancelled:
                    job.result = cast(
                        JsonObject,
                        await job.kernel.request(
                            "run", params, on_notification=on_notification
                        ),
                    )
                    # Which draft code the run used: for the trace only.
                    drafts = job.result.pop("drafts", None)
                    final = job.result.pop("profile", None)
                    if isinstance(final, dict):
                        profile = cast(ProfileSnapshot, final)
                        job.board.profile(profile)
                        job.result["profile"] = profiles.summary(profile)
            except KernelError as e:
                job.error = e.to_dict()
        if job.cancelled and job.result is None:
            job.error = CANCELLED
        job.board.finish(
            str(job.error.get("message")) if job.error is not None else None,
            summary=_summary(job.result) if job.result is not None else None,
        )
        traces.write(
            self.config.runs_dir,
            started=started,
            duration=time.monotonic() - clock,
            spec=cast(JsonObject, params["spec"]),
            input=params.get("input"),
            result=job.result,
            error=job.error,
            progress=_final_progress(job.board),
            pipeline=pipeline,
            params=cast(JsonObject | None, params.get("params")),
            drafts=cast(JsonObject | None, drafts),
            profile=profiles.trace(profile) if profile is not None else None,
        )
        job.finished.set()

    async def _modules(self, spec: JsonObject) -> list[str] | None:
        """The operator modules a spec uses, or ``None`` (load everything)
        when the catalog cannot tell (an unknown ref, a kernel error)."""
        if self.catalog is None:
            return None
        refs: list[JsonValue] = []
        refs.extend(traces.op_refs(spec))
        try:
            found = cast(
                JsonObject, await self.catalog.request("modules", {"refs": refs})
            )
        except KernelError:
            return None
        if found["unknown"]:
            return None
        return cast(list[str], found["modules"])

    def _forget_evicted(self) -> None:
        """Drop finished runs the dashboard no longer keeps."""
        kept = {view["id"] for view in self.runs.runs()}
        gone = [
            i
            for i, job in self._jobs.items()
            if i not in kept and job.finished.is_set()
        ]
        for run_id in gone:
            del self._jobs[run_id]


def _title(spec: JsonObject, pipeline: str | None, values: JsonObject | None) -> str:
    """The pipeline name, else the root node name, else the operator refs;
    then the parameter values."""
    root = spec.get("root", spec)
    name = root.get("name") if isinstance(root, dict) else None
    title = pipeline or (name if isinstance(name, str) else None)
    title = title or ", ".join(traces.op_refs(spec)) or "run"
    if values:
        shown = (
            f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
            for k, v in values.items()
        )
        title = f"{title} · {', '.join(shown)}"
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


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
