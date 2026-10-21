"""Remote runs in the kernel of the run (kernel side; it loads no domain
operators): stage local files to the target's storage, send the job to
the target, bridge its events into the kernel's notifications, and bring
the outputs back into ``runs/<run_id>/out/``.

The job carries the code (``sources``, ``packages``) the server collected;
the keys come from the environment of this kernel only.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from ico.core.runtime.cancel import IcoCancelToken
from ico.remote import FetchJob, Job, Package, RunPod
from ico.remote.runpod import API
from ico.remote.runpod_api import REST_API, RunPodApi, RunPodApiError, Volume
from ico.remote.storage import storage_for

from ico_mcp.protocol import ERROR_CODES, JsonObject, JsonValue, KernelError

UPLOAD_LIMIT = 5 * 10**9
"""Bytes of local inputs a remote run uploads at most."""
API_ENV = "ICO_RUNPOD_API"
REST_ENV = "ICO_RUNPOD_REST"
BRIDGED = ("plan", "progress", "profile", "sample")

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
    volume = _volume(target)
    if volume is not None:
        # The order's volume is the storage: its S3 API from here, its
        # mount on the worker.
        os.environ.update(volume.s3_env())
        storage = volume.uri("ico").rstrip("/")
    else:
        storage = str(target["storage"])
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
    out_uri = f"{storage}/runs/{run_id}/out"
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
    )
    try:
        handle = _executor(target).submit(job)
    except RuntimeError as e:
        raise KernelError("remote_error", str(e)) from None
    # A cancel (SIGTERM, in the main thread) must not wait for HTTP there.
    cancel.add_listener(
        lambda reason: threading.Thread(target=handle.cancel, daemon=True).start()
    )
    result: JsonObject | None = None
    try:
        for event in handle.events():
            kind = event.get("event")
            if kind in BRIDGED:
                body: JsonObject = {k: v for k, v in event.items() if k != "event"}
                if kind == "sample":
                    body = {"sample": event.get("snapshot")}
                notify(str(kind), body)
            elif kind == "result":
                result = cast(JsonObject, event["result"])
            elif kind == "error":
                raise _job_error(event)
    except RuntimeError as e:
        raise KernelError("remote_error", str(e)) from None
    if result is None:
        raise KernelError("remote_error", "the remote job ended without a result")
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
    if staged:
        result["uploaded"] = cast(JsonValue, dict(staged))
    return result


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
            if kind == "fetch":
                id = str(event.get("asset"))
                snapshot: JsonObject = {
                    "key": f"fetch.{id}",
                    "label": f"fetch {id} (MB)",
                    "node": None,
                    "done": round(float(cast(float, event.get("done") or 0)) / 1e6, 1),
                    "total": round(
                        float(cast(float, event.get("total") or 0)) / 1e6, 1
                    ),
                    "rate": None,
                    "eta": None,
                }
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
    return RunPodApi(base_url=os.environ.get(REST_ENV) or REST_API)


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
