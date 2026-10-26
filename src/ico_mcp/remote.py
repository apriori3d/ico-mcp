"""Remote runs in the kernel of the run (kernel side; it loads no domain
operators): stage local files to the target's storage, send the job to
the target, bridge its events into the kernel's notifications, and bring
the outputs back into ``runs/<run_id>/out/``.

A run's files in the storage are ``<storage>/runs/<run_id>/``: ``in/``
(uploaded inputs), ``out/`` (outputs) and ``result.json`` (the job's final
event, written by the runner). ``runs/<run_id>/remote.json`` here records
where the job went (target, RunPod's id, those URIs) right after it is
submitted, so :func:`fetch_run` can finish a run whose stream was lost
(a network outage, a restart of the server).

The job carries the code (``sources``, ``packages``) the server collected;
the keys come from the environment of this kernel only.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ico.core.runtime.cancel import IcoCancelToken
from ico.remote import FetchJob, Job, Package, RunPod, RunPodJob
from ico.remote.runpod import API, DONE
from ico.remote.runpod_api import RunPodApi, RunPodApiError, Volume
from ico.remote.storage import storage_for

from ico_mcp.fetching import fetch_snapshot
from ico_mcp.protocol import ERROR_CODES, JsonObject, JsonValue, KernelError

UPLOAD_LIMIT = 5 * 10**9
"""Bytes of local inputs a remote run uploads at most."""
API_ENV = "ICO_RUNPOD_API"
BRIDGED = ("queue", "plan", "progress", "profile", "sample")
RECORD = "remote.json"
"""Where a run went, under ``runs/<run_id>/``."""

Notify = Callable[[str, JsonObject], None]


def run_remote(
    params: JsonObject, runs_dir: Path, notify: Notify, cancel: IcoCancelToken
) -> JsonObject:
    """Run ``params`` (a kernel ``run`` request with ``target``,
    ``run_id``, ``sources``, ``packages``) on its target; returns the
    result as a local run does, with local output paths and ``target``.

    Raises:
        KernelError: ``storage_error`` (staging), ``remote_error`` (the
            target), or the job's own error (``spec_error``,
            ``run_error``, ``cancelled``, ...).
    """
    target = cast(JsonObject, params["target"])
    run_id = str(params["run_id"])
    volume, storage = _storage(target)
    try:
        staged: dict[str, str] = {}
        budget = [UPLOAD_LIMIT]
        input = _upload(
            params.get("input"), f"{storage}/runs/{run_id}/in", staged, budget
        )
        values = _upload(
            params.get("params"), f"{storage}/runs/{run_id}/in", staged, budget
        )
    except Exception as e:  # botocore raises its own types, not OSError
        raise KernelError(
            "storage_error", f"uploading the inputs: {type(e).__name__}: {e}"
        ) from None
    out_uri, status_uri = _uris(storage, run_id)
    job = Job(
        spec=cast(JsonObject, params["spec"]),
        input=input,
        params=cast(JsonObject | None, values),
        packages=tuple(
            Package.from_dict(cast(JsonObject, p))
            for p in cast(list[JsonValue], params.get("packages") or [])
        ),
        sources={
            k: str(v) for k, v in cast(JsonObject, params.get("sources") or {}).items()
        },
        out=out_uri,
        profile=bool(params.get("profile")),
        recent=bool(params.get("recent")),
        max_items=cast(int | None, params.get("max_items")),
        id=run_id,
        volume=volume.id if volume is not None else None,
        cleanup=bool(target.get("cleanup")),
        status=status_uri,
    )
    try:
        handle = _executor(target).submit(job)
    except RuntimeError as e:
        raise KernelError("remote_error", str(e)) from None
    _record(runs_dir, run_id, target, handle.id, storage)
    result = _follow(handle, run_id, notify, cancel)
    result = _finish(result, out_uri, runs_dir, run_id, target)
    if staged:
        result["uploaded"] = cast(JsonValue, dict(staged))
    return result


def fetch_run(
    params: JsonObject, runs_dir: Path, notify: Notify, cancel: IcoCancelToken
) -> JsonObject:
    """Finish the remote run ``params["input"]`` (a run id) whose client
    lost its stream: its result from ``result.json`` in the storage, or,
    while RunPod still runs the job, its events again from RunPod's stream.
    The outputs come into ``runs/<run_id>/out/`` as for the run itself, and
    the result says ``fetched``: the run id.

    A run from before ``remote.json`` existed is looked up in the target's
    storage by its id alone (no RunPod id: only what the storage has).

    Raises:
        KernelError: the run's own error (from ``result.json`` or the
            stream), ``not_found`` (nothing of the run in the storage, and
            RunPod does not know the job), ``remote_error``.
    """
    target = cast(JsonObject, params["target"])
    run_id = str(params["input"])
    _, storage = _storage(target)  # also the volume's S3 keys, if it is one
    record = read_record(runs_dir, run_id)
    runpod_id = None
    if record is not None:
        storage, runpod_id = str(record["storage"]), record.get("runpod_id")
    out_uri, status_uri = _uris(storage, run_id)
    event = _read_status(status_uri)
    if event is None and runpod_id is not None:
        handle = _executor(target).job(str(runpod_id), run_id)
        try:
            status = str(handle.status().get("status", ""))
        except RuntimeError:
            status = ""  # RunPod no longer keeps the job
        if status and status not in DONE:
            result = _follow(handle, run_id, notify, cancel)
            result = _finish(result, out_uri, runs_dir, run_id, target)
            result["fetched"] = run_id
            return result
        event = _read_status(status_uri)  # it may have ended meanwhile
    if event is None:
        local = runs_dir / run_id / "out"
        found = _download_all(out_uri, local)
        if not found:
            raise KernelError(
                "not_found",
                f"run {run_id}: no result and no outputs in {storage}/runs/{run_id}/",
                {"run_id": run_id},
            )
        raise KernelError(
            "remote_error",
            f"run {run_id} left no result.json (it ended before writing one, "
            f"or ran before ico wrote one); its outputs are in {local}",
            {"run_id": run_id, "outputs": list[JsonValue](found)},
        )
    if event.get("event") == "error":
        raise _job_error(event)
    result = _finish(
        cast(JsonObject, event["result"]), out_uri, runs_dir, run_id, target
    )
    result["fetched"] = run_id
    return result


def read_record(runs_dir: Path, run_id: str) -> JsonObject | None:
    """``runs/<run_id>/remote.json``: where a remote run went, if recorded."""
    path = runs_dir / run_id / RECORD
    if not path.is_file():
        return None
    return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))


def _record(
    runs_dir: Path, run_id: str, target: JsonObject, runpod_id: str, storage: str
) -> None:
    """Write ``runs/<run_id>/remote.json`` right after the job is sent."""
    out_uri, status_uri = _uris(storage, run_id)
    record: JsonObject = {
        "run_id": run_id,
        "target": str(target["name"]),
        "endpoint": str(target["endpoint"]),
        "runpod_id": runpod_id,
        "storage": storage,
        "out": out_uri,
        "status": status_uri,
        "submitted": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    path = runs_dir / run_id / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")


def _follow(
    handle: RunPodJob, run_id: str, notify: Notify, cancel: IcoCancelToken
) -> JsonObject:
    """The job's events bridged into notifications, up to its result."""
    # A cancel (SIGTERM, in the main thread) must not wait for HTTP there.
    cancel.add_listener(
        lambda reason: threading.Thread(target=handle.cancel, daemon=True).start()
    )
    try:
        for event in handle.events():
            kind = event.get("event")
            if kind in BRIDGED:
                body: JsonObject = {k: v for k, v in event.items() if k != "event"}
                if kind == "sample":
                    body = {"sample": event.get("snapshot")}
                notify(str(kind), body)
            elif kind == "result":
                return cast(JsonObject, event["result"])
            elif kind == "error":
                raise _job_error(event)
    except RuntimeError as e:
        # The job may go on without us (a network outage outlasting the
        # retries): its outcome lands in the storage.
        raise KernelError(
            "remote_error",
            f"{e}; the job may still run: fetch_run(run_id) gets its outcome",
            {"run_id": run_id, "runpod_id": handle.id},
        ) from None
    raise KernelError("remote_error", "the remote job ended without a result")


def _finish(
    result: JsonObject, out_uri: str, runs_dir: Path, run_id: str, target: JsonObject
) -> JsonObject:
    """``result`` with its outputs downloaded into ``runs/<run_id>/out/``."""
    try:
        result["outputs"] = _download(
            result.get("outputs"), out_uri, runs_dir / run_id / "out"
        )
    except Exception as e:  # botocore raises its own types, not OSError
        raise KernelError(
            "storage_error",
            f"downloading the outputs: {type(e).__name__}: {e}",
            {"result": result},
        ) from None
    result["target"] = str(target["name"])
    return result


def _download_all(prefix: str, local: Path) -> list[str]:
    """Everything under ``prefix`` downloaded into ``local``; the files."""
    storage = storage_for(prefix + "/")
    if storage is None:
        return []
    try:
        storage.get(prefix + "/", local)
    except Exception:  # missing, or botocore's own errors
        return []
    return sorted(str(p) for p in local.rglob("*") if p.is_file())


def _read_status(uri: str) -> JsonObject | None:
    """The final event a runner wrote to ``uri``, or ``None`` if there is
    none (yet)."""
    storage = storage_for(uri)
    if storage is None:
        return None
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "result.json"
        try:
            storage.get(uri, path)
        except Exception:  # missing: FileNotFoundError, botocore's 404
            return None
        return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))


def _storage(target: JsonObject) -> tuple[Volume | None, str]:
    """The target's volume (when it is the storage) and storage prefix."""
    volume = None if target.get("storage") else _volume(target)
    if volume is None:
        return None, str(target["storage"]).rstrip("/")
    # Without a storage the order's volume is the storage: its S3 API from
    # here, its mount on the worker. With one, the volume keeps only what
    # the worker reads at its mount (assets, work dirs) and its S3 API (and
    # keys) is not used at all.
    os.environ.update(volume.s3_env())
    return volume, volume.uri("ico").rstrip("/")


def _uris(storage: str, run_id: str) -> tuple[str, str]:
    """The outputs prefix and the ``result.json`` of a run."""
    return f"{storage}/runs/{run_id}/out", f"{storage}/runs/{run_id}/result.json"


def fetch_remote(
    params: JsonObject, notify: Notify, cancel: IcoCancelToken
) -> JsonObject:
    """A prefetch on the target: the manifest and lock of ``sources``, the
    asset ids as ``input``; ``fetch`` events become progress snapshots
    (one node per asset, in MB). The result is shaped as a fetch run's:
    one output ``{"fetched": [states]}``."""
    target = cast(JsonObject, params["target"])
    sources = cast(JsonObject, params.get("sources") or {})
    if "assets.toml" not in sources or "assets.lock" not in sources:
        raise KernelError(
            "bad_request", "the domain has no assets.toml and assets.lock"
        )
    job = FetchJob(
        manifest=str(sources["assets.toml"]),
        lock=str(sources["assets.lock"]),
        ids=tuple(str(i) for i in cast(list[JsonValue], params.get("input") or [])),
        id=str(params["run_id"]),
    )
    handle = _executor(target).submit(job)
    cancel.add_listener(
        lambda reason: threading.Thread(target=handle.cancel, daemon=True).start()
    )
    try:
        for event in handle.events():
            kind = event.get("event")
            if kind == "queue":
                notify("queue", {"status": event.get("status")})
            elif kind == "fetch":
                snapshot = fetch_snapshot(
                    str(event.get("asset")),
                    float(cast(float, event.get("done") or 0)),
                    float(cast(float, event.get("total") or 0)),
                )
                notify("progress", {"snapshot": snapshot})
            elif kind == "result":
                result = cast(JsonObject, event["result"])
                return {
                    "outputs": [{"fetched": result.get("assets")}],
                    "total": 1,
                    "errors": [],
                    "root": result.get("root"),
                    "target": str(target["name"]),
                }
            elif kind == "error":
                raise _job_error(event)
    except RuntimeError as e:
        raise KernelError("remote_error", str(e)) from None
    raise KernelError("remote_error", "the prefetch ended without a result")


def volume_up(params: JsonObject) -> JsonObject:
    """Create a volume for an order and attach it to the target's endpoint."""
    target = cast(JsonObject, params["target"])
    try:
        created = _api().volume_up(
            str(target["endpoint"]),
            int(cast(int, params["size"])),
            str(params["datacenter"]),
            str(params.get("name") or f"ico-{target['name']}"),
        )
    except RunPodApiError as e:
        raise KernelError("remote_error", str(e)) from None
    return {
        "id": created.id,
        "name": created.name,
        "size": created.size,
        "datacenter": created.datacenter,
    }


def volume_down(params: JsonObject) -> JsonObject:
    """Detach the order's volume from the endpoint and delete it."""
    target = cast(JsonObject, params["target"])
    volume = cast(JsonObject, params["volume"])
    try:
        _api().volume_down(str(target["endpoint"]), str(volume["id"]))
    except RunPodApiError as e:
        raise KernelError("remote_error", str(e)) from None
    return {"deleted": volume["id"]}


def _api() -> RunPodApi:
    return RunPodApi.from_env()


def _volume(target: JsonObject) -> Volume | None:
    found = target.get("volume")
    if not isinstance(found, dict):
        return None
    return Volume(
        id=str(found["id"]),
        name=str(found.get("name") or ""),
        size=int(cast(int, found.get("size") or 0)),
        datacenter=str(found["datacenter"]),
    )


def _executor(target: JsonObject) -> RunPod:
    return RunPod(
        str(target["endpoint"]),
        execution_timeout=float(cast(float, target.get("execution_timeout", 10800))),
        base_url=os.environ.get(API_ENV) or API,
    )


def _job_error(event: JsonObject) -> KernelError:
    """The kernel error of a job's ``error`` event; codes the server does
    not know become ``remote_error`` with ``remote_code``."""
    code = str(event.get("code"))
    data: JsonObject = {
        k: v for k, v in event.items() if k not in ("event", "code", "message")
    }
    message = str(event.get("message"))
    if code in ERROR_CODES:
        return KernelError(code, message, data)
    return KernelError("remote_error", message, {"remote_code": code, **data})


def _upload(
    value: JsonValue, prefix: str, staged: dict[str, str], budget: list[int]
) -> JsonValue:
    """``value`` with existing local absolute paths uploaded under
    ``prefix`` and replaced by their URIs."""
    if isinstance(value, str):
        path = Path(value)
        if not (path.is_absolute() and path.exists()):
            return value
        if value not in staged:
            size = _size(path)
            if size > budget[0]:
                raise ValueError(
                    f"{value}: inputs over {UPLOAD_LIMIT / 1e9:.0f} GB are not uploaded; "
                    "put them in the target's storage and pass the URI"
                )
            budget[0] -= size
            name = path.name or "input"
            used = {uri.rsplit("/", 1)[-1] for uri in staged.values()}
            n = 1
            while name in used:
                name = f"{path.stem}-{n}{path.suffix}"
                n += 1
            uri = f"{prefix}/{name}"
            storage = storage_for(uri)
            if storage is None:
                raise ValueError(f"not a storage URI: {uri}")
            storage.put(path, uri)
            staged[value] = uri
        return staged[value]
    if isinstance(value, list):
        return [_upload(v, prefix, staged, budget) for v in value]
    if isinstance(value, dict):
        return {k: _upload(v, prefix, staged, budget) for k, v in value.items()}
    return value


def _download(value: JsonValue, prefix: str, local: Path) -> JsonValue:
    """``value`` with URIs under ``prefix`` downloaded below ``local`` and
    replaced by local paths."""
    if isinstance(value, str):
        if value != prefix and not value.startswith(prefix + "/"):
            return value
        relative = value[len(prefix) :].lstrip("/")
        target = local / relative if relative else local
        storage = storage_for(value)
        if storage is None:
            return value
        target.parent.mkdir(parents=True, exist_ok=True)
        storage.get(value, target)
        return str(target)
    if isinstance(value, list):
        return [_download(v, prefix, local) for v in value]
    if isinstance(value, dict):
        return {k: _download(v, prefix, local) for k, v in value.items()}
    return value


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
