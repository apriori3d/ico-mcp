"""Runs started by the server, each in a kernel process of its own.

A run is cancelled by SIGTERM to its kernel: ICO stops the run between
items and returns what it produced; a run that does not stop in time is
killed with its workers. A kernel per run also keeps the main kernel free
for search, validate and proposals while a run is going, and a reload does
not cut runs short: they finish on the code they loaded.

Every run is a run of the dashboard (its ``BoardRun``), so the run id is the
same on the board and in ``run_status``.
"""

from __future__ import annotations

import collections
import itertools
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
from ico_mcp.config import Config, TargetConfig
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError
from ico_mcp.sources import job_code

OnProgress = Callable[[JsonObject], Awaitable[None]]
"""Receives every progress snapshot of a run (e.g. for MCP progress)."""

CANCELLED: JsonObject = {"code": "cancelled", "message": "the run was cancelled"}
CANCEL_GRACE = 10.0
"""Seconds a cancelled run has to stop between items before it is killed."""
TITLE_MAX = 120
RECENT = 3
"""Recent previews kept per node (``run(recent=true)``)."""


class SampleWait:
    """A ``sample_run`` waiting for the samples of one node."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.node: str | None = None
        """The plan key, once the kernel resolved the request."""
        self.label: str | None = None
        self.error: JsonObject | None = None
        self.samples: list[JsonObject] = []
        self.started = anyio.Event()
        """Set when the kernel resolved the request (or refused it)."""
        self.done = anyio.Event()

    def on_started(self, params: JsonObject) -> None:
        self.started.set()
        node, label = params.get("node"), params.get("label")
        self.node = node if isinstance(node, str) else None
        self.label = label if isinstance(label, str) else None

    def on_error(self, params: JsonObject) -> None:
        self.started.set()
        error = params.get("error")
        self.error = cast(JsonObject, error) if isinstance(error, dict) else {}
        self.done.set()

    def on_sample(self, sample: JsonObject) -> None:
        if sample.get("node") == self.node and len(self.samples) < self.count:
            self.samples.append(sample)
            if len(self.samples) == self.count:
                self.done.set()


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
        self.samples: dict[int, SampleWait] = {}
        """``sample_run`` calls waiting, by their token."""
        self.recent: dict[str, collections.deque[JsonObject]] = {}
        """The last recent previews of each node (``run(recent=true)``)."""

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
        self._sample_tokens = itertools.count(1)

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
        recent: bool = False,
        target: TargetConfig | None = None,
    ) -> Job:
        """Start a run in a new kernel; it finishes in the background.

        ``pipeline`` names a saved pipeline run by name; ``values`` are the
        values of the pipeline parameters; ``profile`` measures every node
        (the dashboard shows it on the plan, the result has a summary);
        ``target`` sends the run to a remote target: its kernel loads no
        operators, carries the code of the modules the spec uses (and the
        assets manifest) in the job, and bridges the job's events.
        """
        if self._tasks is None:
            raise RuntimeError("Jobs is not entered")
        title = _title(spec, pipeline, values)
        job = Job(
            KernelClient(self.config, remote=target is not None),
            self.runs.start(
                f"{title} @ {target.name}" if target is not None else title
            ),
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
        if recent:
            params["recent"] = True
        if on_progress is not None:
            job.listeners.append(on_progress)
        self._tasks.start_soon(self._run, job, params, pipeline, target)
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
        """Stop a running run; a finished run is left as is.

        The run's kernel gets SIGTERM: the run stops between items, its
        workers stop cleanly, and it returns the outputs produced so far
        (with its trace and profile). A run that has not stopped after
        ``CANCEL_GRACE`` seconds (an item that runs long) is killed with its
        workers.
        """
        job = self.get(run_id)
        if not job.finished.is_set() and not job.cancelled:
            job.cancelled = True
            job.kernel.terminate()
            assert self._tasks is not None
            self._tasks.start_soon(self._kill_after_grace, job)
        return job

    async def sample(
        self, run_id: str, node: str, count: int, wait: bool = True
    ) -> JsonObject:
        """The next ``count`` results of ``node`` (a plan key or a label) in
        a running run, as previews; waits at most ``timeouts.wait``, or
        until the run ends, and returns the samples that came by then, with
        the node's recent previews (``run(recent=true)``). Without
        ``wait`` it returns once the node is resolved.

        Raises:
            KernelError: ``not_found`` (run or node), ``not_running`` (the
                run ended or its kernel is not up yet), ``bad_request``.
        """
        job = self.get(run_id)
        if job.finished.is_set():
            raise KernelError("not_running", f"run {run_id} has ended")
        token = next(self._sample_tokens)
        waiting = SampleWait(count)
        job.samples[token] = waiting
        try:
            params: JsonObject = {"token": token, "node": node, "count": count}
            await job.kernel.send("sample", params)
            with anyio.move_on_after(self.config.timeouts.wait):
                await (waiting.done if wait else waiting.started).wait()
        finally:
            del job.samples[token]
        if waiting.error is not None:
            raise KernelError.from_dict(waiting.error)
        recent: list[JsonValue] = []
        if waiting.node is not None:
            recent.extend(job.recent.get(waiting.node, ()))
        return {
            "node": waiting.node,
            "label": waiting.label,
            "recent": recent,
            "samples": list[JsonValue](waiting.samples),
            "complete": len(waiting.samples) == count,
            "state": job.status()["state"],
        }

    async def _kill_after_grace(self, job: Job) -> None:
        with anyio.move_on_after(CANCEL_GRACE):
            await job.finished.wait()
        if not job.finished.is_set():
            job.kernel.kill()

    async def _run(
        self,
        job: Job,
        params: JsonObject,
        pipeline: str | None,
        target: TargetConfig | None = None,
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
            elif method in ("sample_started", "sample_error"):
                wait = job.samples.get(cast(int, body.get("token")))
                if wait is not None and method == "sample_started":
                    wait.on_started(body)
                elif wait is not None:
                    wait.on_error(body)
            elif method == "sample" and isinstance(body.get("sample"), dict):
                sample = cast(JsonObject, body["sample"])
                if sample.get("recent"):
                    node = str(sample.get("node"))
                    kept = job.recent.setdefault(node, collections.deque(maxlen=RECENT))
                    kept.append(sample)
                else:
                    for wait in list(job.samples.values()):
                        wait.on_sample(sample)

        drafts: JsonValue = None
        profile: ProfileSnapshot | None = None
        modules = await self._modules(cast(JsonObject, params["spec"]))
        if target is None:
            job.kernel.modules = modules
        else:
            sources, packages = job_code(self.config, modules)
            params = {
                **params,
                "run_id": job.id,
                "sources": sources,
                "packages": packages,
                "target": {
                    "name": target.name,
                    "provider": target.provider,
                    "endpoint": target.endpoint,
                    "storage": target.storage,
                    "execution_timeout": target.execution_timeout,
                },
            }
            job.kernel.modules = []  # the code goes to the target
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
        # Cancelled: the kernel stopped the run between items and returned
        # what it produced (`cancelled` in the result), or it was killed (no
        # result). Either way the run did not succeed.
        if (job.cancelled and job.result is None) or (
            job.result is not None and "cancelled" in job.result
        ):
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
        for wait in job.samples.values():
            wait.done.set()

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
    if "cancelled" in result:
        parts.append("cancelled")
    return " · ".join(parts)


def _final_progress(run: BoardRun) -> list[JsonValue]:
    """The final state of every progress node, for the run trace."""
    out: list[JsonValue] = []
    for node in run.view()["progress"]:
        out.append({k: node[k] for k in ("key", "label", "done", "total")})
    return out
