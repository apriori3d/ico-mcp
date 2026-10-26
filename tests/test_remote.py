"""Remote runs: run(target=) against a fake RunPod API on 127.0.0.1 that
runs ICO's real worker handler, with file:// storage; the code goes by
value, local files are uploaded and outputs come back."""

import asyncio
import itertools
import json
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp import targets
from ico_mcp.config import Config, ConfigError, load_config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject
from ico_mcp.server import create_server
from ico_mcp.sources import absolute_imports, job_code

MakeRepo = Any
pytestmark = pytest.mark.anyio
KEY = "test-key"

FILES = '''
from pathlib import Path

from ico import IcoOperator
from ico.registry import register

from domain.text import Report


@register("upper_file", 1)
class UpperFile(IcoOperator[str, str]):
    """Write an upper-cased copy of a text file into out_dir."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        super().__init__(fn=self._apply)

    def _apply(self, path: str) -> str:
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        target = Path(self.out_dir) / Path(path).name
        target.write_text(Path(path).read_text().upper() + str(Report(1).words))
        return str(target)
'''


@dataclass
class FakeJob:
    body: dict[str, Any]
    status: str = "IN_QUEUE"
    chunks: list[dict[str, Any]] = field(default_factory=lambda: [])
    read: int = 0
    loop: asyncio.AbstractEventLoop | None = None
    task: "asyncio.Task[None] | None" = None


class FakeRunPod:
    """/run, /stream, /status, /cancel; each job runs ICO's handler."""

    def __init__(self) -> None:
        from ico.remote.runpod_handler import handler

        self.handler = handler
        self.jobs: dict[str, FakeJob] = {}
        self.gpu = threading.Event()
        """Cleared: jobs wait in the queue (no GPU free)."""
        self.gpu.set()
        self.broken = False
        """Set: stream reads fail (a network outage on the client)."""
        self.ids = itertools.count(1)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.serve(self)

            def do_POST(self) -> None:
                fake.serve(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve(self, request: BaseHTTPRequestHandler) -> None:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        _, _endpoint, action, *rest = request.path.split("/")
        if action == "run":
            body = json.loads(
                request.rfile.read(int(request.headers["content-length"]))
            )
            id = f"rp-{next(self.ids)}"
            job = self.jobs[id] = FakeJob(body)
            threading.Thread(target=self.work, args=(id, job), daemon=True).start()
            return self.reply(request, {"id": id, "status": job.status})
        job = self.jobs[rest[0]]
        if action == "stream" and self.broken:
            return self.reply(request, {"error": "unavailable"}, 503)
        if action == "stream":
            new = job.chunks[job.read :]
            job.read += len(new)
            return self.reply(
                request, {"status": job.status, "stream": [{"output": c} for c in new]}
            )
        if action == "status":
            return self.reply(request, {"id": rest[0], "status": job.status})
        job.status = "CANCELLED"
        if job.loop is not None and job.task is not None:
            job.loop.call_soon_threadsafe(job.task.cancel)
        self.reply(request, {"id": rest[0], "status": job.status})

    def reply(
        self, request: BaseHTTPRequestHandler, body: object, code: int = 200
    ) -> None:
        data = json.dumps(body).encode()
        request.send_response(code)
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    def work(self, id: str, job: FakeJob) -> None:
        self.gpu.wait()

        async def main() -> None:
            job.loop = asyncio.get_running_loop()
            job.task = asyncio.current_task()
            job.status = "IN_PROGRESS"
            try:
                async for chunk in self.handler({"id": id, "input": job.body["input"]}):
                    job.chunks.append(chunk)
                job.status = "COMPLETED"
            except asyncio.CancelledError:
                pass

        asyncio.run(main())


@pytest.fixture
def runpod() -> Iterator[FakeRunPod]:
    fake = FakeRunPod()
    yield fake
    fake.gpu.set()
    fake.server.shutdown()


@pytest.fixture
def remote(
    make_repo: MakeRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runpod: FakeRunPod,
) -> Iterator[Config]:
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    monkeypatch.setenv("ICO_RUNPOD_API", runpod.url)
    monkeypatch.setenv("ICO_WORKDIRS", str(tmp_path / "volume" / "jobs"))
    storage = f"file://{tmp_path / 'bucket'}"
    repo = tmp_path / "repo"
    make_repo(repo, f'[targets.gpu]\nendpoint = "ep"\nstorage = "{storage}"\n')
    (repo / "domain_draft" / "files.py").write_text(textwrap.dedent(FILES))
    yield load_config(repo)


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def call_error(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    message = text(result)
    return cast(dict[str, Any], json.loads(message[message.index("{") :]))


async def finish(client: Client, run_id: str) -> dict[str, Any]:
    while True:
        status = await call(client, "wait_run", run_id=run_id, timeout=5)
        if status["state"] != "running":
            return status


def seq(*refs: str) -> dict[str, Any]:
    return {"kind": "seq", "children": [{"kind": "op", "op": r} for r in refs]}


async def test_a_run_records_where_it_went(remote: Config, tmp_path: Path) -> None:
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="a b c",
            target="gpu", wait=False,
        )  # fmt: skip
        done = await finish(client, started["run_id"])

    run_id = started["run_id"]
    record = json.loads((remote.runs_dir / run_id / "remote.json").read_text())
    bucket = f"file://{tmp_path / 'bucket'}"
    assert record["runpod_id"] == "rp-1" and record["target"] == "gpu"
    assert record["status"] == f"{bucket}/runs/{run_id}/result.json"
    written = json.loads(
        (tmp_path / "bucket" / "runs" / run_id / "result.json").read_text()
    )
    assert written["event"] == "result" and written["result"]["outputs"] == [3]
    assert done["result"]["outputs"] == [3]
    [trace] = [
        json.loads(line)
        for path in remote.runs_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert (trace["run_id"], trace["target"]) == (run_id, "gpu")


async def test_a_lost_stream_is_fetched_from_the_storage(
    remote: Config, runpod: FakeRunPod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ICO_REMOTE_RETRY", "0.5")
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    spec = {
        "kind": "op",
        "op": "upper_file@1",
        "params": {"out_dir": {"$param": "out"}},
    }
    runpod.gpu.clear()  # the job waits for a worker while the network fails
    runpod.broken = True
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=spec, input=str(source), params={"out": "$JOB/out"},
            target="gpu", wait=False,
        )  # fmt: skip
        lost = await finish(client, started["run_id"])
        runpod.gpu.set()
        [job] = runpod.jobs.values()
        while job.status != "COMPLETED":  # the job goes on without the client
            await asyncio.sleep(0.05)
        runpod.broken = False
        fetching = await call(client, "fetch_run", run_id=started["run_id"])
        fetched = await finish(client, fetching["run_id"])

    run_id = started["run_id"]
    assert lost["state"] == "failed"
    assert lost["kernel_error"]["code"] == "remote_error"
    assert lost["kernel_error"]["run_id"] == run_id
    assert "fetch_run" in lost["kernel_error"]["message"]
    assert fetching["fetches"] == run_id and fetching["target"] == "gpu"
    assert fetched["state"] == "done", fetched.get("kernel_error")
    local = remote.runs_dir / run_id / "out" / "notes.txt"
    assert fetched["result"]["outputs"] == [str(local)]
    assert fetched["result"]["fetched"] == run_id
    assert local.read_text() == "HELLO1"


async def test_fetch_run_follows_a_job_that_still_runs(
    remote: Config, runpod: FakeRunPod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ICO_REMOTE_RETRY", "0.5")
    runpod.gpu.clear()
    runpod.broken = True
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="a b",
            target="gpu", wait=False,
        )  # fmt: skip
        lost = await finish(client, started["run_id"])
        runpod.broken = False
        fetching = await call(client, "fetch_run", run_id=started["run_id"])
        runpod.gpu.set()  # a worker takes the job only now
        fetched = await finish(client, fetching["run_id"])

    assert lost["state"] == "failed"
    assert fetched["state"] == "done", fetched.get("kernel_error")
    assert fetched["result"]["outputs"] == [2]
    assert fetched["result"]["fetched"] == started["run_id"]


async def test_an_older_run_is_looked_up_in_the_storage(
    remote: Config, tmp_path: Path
) -> None:
    out = tmp_path / "bucket" / "runs" / "0ld" / "out"
    out.mkdir(parents=True)
    (out / "video.mp4").write_text("frames")
    async with Client(create_server(remote)) as client:
        unnamed = await call_error(client, "fetch_run", run_id="0ld")
        older = await call(client, "fetch_run", run_id="0ld", target="gpu")
        found = await finish(client, older["run_id"])
        none = await call(client, "fetch_run", run_id="nothing", target="gpu")
        missing = await finish(client, none["run_id"])

    local = remote.runs_dir / "0ld" / "out" / "video.mp4"
    assert unnamed["code"] == "bad_request"
    assert found["state"] == "failed"
    assert found["kernel_error"]["code"] == "remote_error"
    assert found["kernel_error"]["outputs"] == [str(local)]
    assert local.read_text() == "frames"
    assert missing["kernel_error"]["code"] == "not_found"


async def test_a_run_goes_to_the_target(remote: Config) -> None:
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="a b c",
            target="gpu", wait=False,
        )  # fmt: skip
        done = await finish(client, started["run_id"])
        board = await call(client, "dashboard")

    assert done["state"] == "done", done.get("kernel_error")
    assert done["result"]["outputs"] == [3]
    assert done["result"]["target"] == "gpu"
    assert done["title"] == "count_words@1, shout@1 @ gpu"
    assert board["url"]


async def test_the_plan_shows_while_the_job_waits_for_a_gpu(
    remote: Config, runpod: FakeRunPod
) -> None:
    runpod.gpu.clear()
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="a b",
            target="gpu", wait=False,
        )  # fmt: skip
        waiting = await call(client, "wait_run", run_id=started["run_id"], timeout=2)
        runpod.gpu.set()
        done = await finish(client, started["run_id"])

    # The plan comes from the main kernel, before any event of the target,
    # and the card says that the job waits for a worker.
    assert waiting["state"] == "running" and waiting["has_plan"]
    assert waiting["summary"].startswith("waiting for a worker on gpu since ")
    assert [j.status for j in runpod.jobs.values()] == ["COMPLETED"]
    assert done["state"] == "done" and done["result"]["outputs"] == [2]


async def test_files_go_up_and_outputs_come_back(
    remote: Config, tmp_path: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    spec = {
        "kind": "op",
        "op": "upper_file@1",
        "params": {"out_dir": {"$param": "out"}},
    }
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=spec, input=str(source), params={"out": "$JOB/out"},
            target="gpu", wait=False,
        )  # fmt: skip
        done = await finish(client, started["run_id"])

    assert done["state"] == "done", done.get("kernel_error")
    [output] = done["result"]["outputs"]
    local = remote.runs_dir / started["run_id"] / "out" / "notes.txt"
    assert output == str(local) and local.read_text() == "HELLO1"
    uploaded = done["result"]["uploaded"][str(source)]
    assert uploaded.endswith(f"/runs/{started['run_id']}/in/notes.txt")


async def test_a_storage_wins_over_the_volume_of_an_order(
    remote: Config, tmp_path: Path, runpod: FakeRunPod
) -> None:
    # The volume's S3 API would need RunPod keys: with a storage, inputs
    # and outputs never go there.
    volume: JsonObject = {
        "id": "vol1",
        "name": "ico-gpu",
        "size": 30,
        "datacenter": "EU-RO-1",
    }
    targets.set_volume(remote, "gpu", volume)
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    spec = {
        "kind": "op",
        "op": "upper_file@1",
        "params": {"out_dir": {"$param": "out"}},
    }
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=spec, input=str(source), params={"out": "$JOB/out"},
            target="gpu", wait=False,
        )  # fmt: skip
        done = await finish(client, started["run_id"])

    assert done["state"] == "done", done.get("kernel_error")
    storage = f"file://{tmp_path / 'bucket'}/runs/{started['run_id']}"
    assert done["result"]["uploaded"][str(source)] == f"{storage}/in/notes.txt"
    [job] = runpod.jobs.values()
    assert job.body["input"]["job"]["out"] == f"{storage}/out"
    assert "volume" not in job.body["input"]["job"]
    local = remote.runs_dir / started["run_id"] / "out" / "notes.txt"
    assert local.read_text() == "HELLO1"


async def test_a_remote_run_can_be_cancelled(remote: Config) -> None:
    words = " ".join(f"w{i}" for i in range(400))
    spec = {"kind": "seq", "children": [
        {"kind": "op", "op": "words@1"},
        {"kind": "stream", "children": [{"kind": "op", "op": "slow@1"}]},
    ]}  # fmt: skip
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=spec, input=words, target="gpu", wait=False
        )
        await asyncio.sleep(3)
        await call(client, "cancel_run", run_id=started["run_id"])
        done = await finish(client, started["run_id"])

    assert done["state"] == "failed"


def gone(path: Path, timeout: float = 10.0) -> bool:
    """Whether ``path`` disappears within ``timeout`` seconds (the runner
    removes its job directory after its last event)."""
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not path.exists()


async def test_job_directories_are_removed_unless_kept(
    remote: Config, tmp_path: Path, runpod: FakeRunPod
) -> None:
    jobs = tmp_path / "volume" / "jobs"
    async with Client(create_server(remote)) as client:
        cleaned = await call(
            client, "run", spec={"kind": "op", "op": "getenv@1"}, input="ICO_RUN_ID",
            target="gpu",
        )  # fmt: skip
        started = await call(
            client, "run", spec={"kind": "op", "op": "fail@1"}, input="x",
            target="gpu", wait=False,
        )  # fmt: skip
        failed = await finish(client, started["run_id"])
        kept = await call(
            client, "run", spec={"kind": "op", "op": "shout@1"}, input="$RUN",
            target="gpu", cleanup=False,
        )  # fmt: skip

    assert cleaned["outputs"] == [cleaned["run_id"]]
    assert failed["state"] == "failed"
    assert gone(jobs / cleaned["run_id"]) and gone(jobs / started["run_id"])
    assert kept["outputs"] == [kept["run_id"].upper()]
    assert (jobs / kept["run_id"] / "code").is_dir()
    options = [j.body["input"]["job"]["options"] for j in runpod.jobs.values()]
    assert [o.get("cleanup", False) for o in options] == [True, True, False]


async def test_a_target_can_keep_its_job_directories(
    remote: Config, tmp_path: Path
) -> None:
    (remote.root / "ico.toml").write_text(
        (remote.root / "ico.toml")
        .read_text()
        .replace('endpoint = "ep"', 'endpoint = "ep"\ncleanup = false')
    )
    config = load_config(remote.root)
    async with Client(create_server(config)) as client:
        kept = await call(
            client, "run", spec={"kind": "op", "op": "shout@1"}, input="x",
            target="gpu",
        )  # fmt: skip

    assert [t.cleanup for t in config.targets] == [False]
    assert (tmp_path / "volume" / "jobs" / kept["run_id"] / "code").is_dir()


async def test_an_unknown_target(remote: Config) -> None:
    async with Client(create_server(remote)) as client:
        error = await call_error(
            client, "run", spec=seq("shout@1"), input="x", target="cpu"
        )
    assert error["code"] == "bad_request" and "targets: gpu" in error["message"]


def test_the_job_carries_the_code_it_uses(remote: Config) -> None:
    (remote.root / "assets.toml").write_text("")
    sources, packages = job_code(remote, ["domain_draft.files"])
    assert set(sources) == {
        "domain/__init__.py", "domain/text.py", "domain_draft/__init__.py",
        "domain_draft/files.py", "assets.toml",
    }  # fmt: skip
    assert packages == [
        {"path": "domain", "status": "stable", "modules": ["text"]},
        {"path": "domain_draft", "status": "draft", "modules": ["files"]},
    ]
    everything, _ = job_code(remote, None)
    assert "domain_draft/tools.py" in everything
    code = "from domain.text import Report\nimport domain.frames\nfrom domain import video\n"
    assert absolute_imports(code, "domain") == {"text", "frames", "video"}


def test_keys_reach_only_remote_kernels(
    remote: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
    local = KernelClient(remote)._env()  # pyright: ignore[reportPrivateUsage]
    far = KernelClient(remote, remote=True)._env()  # pyright: ignore[reportPrivateUsage]
    assert "RUNPOD_API_KEY" not in local and far["RUNPOD_API_KEY"] == KEY
    assert "HTTPS_PROXY" not in local and far["HTTPS_PROXY"] == "http://127.0.0.1:8899"


def test_targets_in_ico_toml(make_repo: MakeRepo, tmp_path: Path) -> None:
    config = cast(
        Config,
        make_repo(tmp_path / "a", '[targets.hard]\nendpoint = "x"\nstorage = "s3://b/p/"\n'
                  "execution_timeout = 600\n"),
    )  # fmt: skip
    [target] = config.targets
    assert (target.name, target.storage, target.execution_timeout) == (
        "hard",
        "s3://b/p",
        600,
    )
    assert target.cleanup
    with pytest.raises(ConfigError, match="targets.y.cleanup: expected true or false"):
        make_repo(tmp_path / "c", '[targets.y]\nendpoint = "e"\ncleanup = 1\n')
    with pytest.raises(ConfigError, match="targets.x.storage: expected an s3://"):
        make_repo(tmp_path / "b", '[targets.x]\nendpoint = "e"\nstorage = "/tmp"\n')


# ─── Assets on the target ───

NOTES = b"hello"
NOTES_OPS = '''
from ico import IcoOperator
from ico.assets import AssetId, asset
from ico.registry import register


@register("asset_text", 1)
class AssetText(IcoOperator[str, str]):
    """The text of a file asset after a prefix."""

    def __init__(self, notes: AssetId = "notes") -> None:
        self.notes = notes
        super().__init__(fn=self._apply)

    def _apply(self, prefix: str) -> str:
        return prefix + asset(self.notes).read_text()
'''


def serve_files(files: dict[str, bytes]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_HEAD(self) -> None:
            self.reply(head=True)

        def do_GET(self) -> None:
            self.reply(head=False)

        def reply(self, head: bool) -> None:
            data = files[self.path.lstrip("/")]
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def test_missing_assets_then_a_prefetch_on_the_target(
    remote: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runpod: FakeRunPod
) -> None:
    import hashlib

    files = serve_files({"notes.txt": NOTES})
    url = f"http://127.0.0.1:{files.server_address[1]}/notes.txt"
    digest = hashlib.sha256(NOTES).hexdigest()
    (remote.root / "assets.toml").write_text(
        f'[assets.notes]\nurl = "{url}"\nsha256 = "{digest}"\n'
    )
    (remote.root / "assets.lock").write_text(
        'version = 1\n\n[[asset]]\nid = "notes"\nkind = "file"\n'
        f'files = [ {{ name = "notes.txt", size = {len(NOTES)}, sha256 = "{digest}" }} ]\n'
    )
    (remote.root / "domain_draft" / "notes.py").write_text(textwrap.dedent(NOTES_OPS))
    volume = tmp_path / "volume" / "assets"
    monkeypatch.setenv("ICO_ASSETS_ROOT", str(volume))  # the worker's root
    spec = {"kind": "op", "op": "asset_text@1"}
    async with Client(create_server(remote)) as client:
        first = await call(
            client, "run", spec=spec, input="> ", target="gpu", wait=False
        )
        failed = await finish(client, first["run_id"])
        runpod.gpu.clear()
        fetching = await call(client, "fetch_assets", spec=spec, target="gpu")
        queued = await call(client, "run_status", run_id=fetching["run_id"])
        runpod.gpu.set()
        fetched = await finish(client, fetching["run_id"])
        again = await call(
            client, "run", spec=spec, input="> ", target="gpu", wait=False
        )
        done = await finish(client, again["run_id"])
    files.shutdown()

    assert failed["state"] == "failed"
    assert failed["kernel_error"]["code"] == "missing_assets"
    assert failed["kernel_error"]["assets"] == [
        {"id": "notes", "reason": "missing", "size": 5}
    ]
    assert (fetching["target"], fetching["assets"], fetching["up_to"]) == (
        "gpu",
        ["notes"],
        5,
    )
    # Every asset at zero while the prefetch waits for a worker.
    assert [(p["key"], p["done"]) for p in queued["progress"]] == [("fetch.notes", 0)]
    assert fetched["state"] == "done" and fetched["title"] == "fetch assets @ gpu"
    assert fetched["result"]["outputs"][0]["fetched"][0]["state"] == "present"
    assert (volume / "notes" / "notes.txt").read_bytes() == NOTES
    assert done["state"] == "done" and done["result"]["outputs"] == ["> hello"]


# ─── Volumes of an order ───


class FakeRest:
    """/networkvolumes and /endpoints/<id> of RunPod's REST API (v1), and
    /serverless/<id> of v2 (where a volume is detached)."""

    def __init__(self) -> None:
        self.volumes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.serve(self, "GET")

            def do_POST(self) -> None:
                fake.serve(self, "POST")

            def do_PATCH(self) -> None:
                fake.serve(self, "PATCH")

            def do_DELETE(self) -> None:
                fake.serve(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        root = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url, self.url_v2 = f"{root}/v1", f"{root}/v2"

    def serve(self, request: BaseHTTPRequestHandler, method: str) -> None:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        length = int(request.headers.get("content-length") or 0)
        body: Any = json.loads(request.rfile.read(length)) if length else None
        path = request.path.removeprefix("/v1").removeprefix("/v2")
        self.calls.append((method, path, body))
        reply: Any = {}
        if path == "/networkvolumes" and method == "POST":
            reply = {"id": f"vol{len(self.volumes) + 1}", **body}
            self.volumes[reply["id"]] = reply
        elif path.startswith("/networkvolumes/") and method == "DELETE":
            self.volumes.pop(path.rsplit("/", 1)[1])
        data = json.dumps(reply).encode()
        request.send_response(200)
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)


@pytest.fixture
def ordered(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Config, FakeRest, FakeRunPod]]:
    """A target without storage: the order's volume is its storage."""
    runpod, rest = FakeRunPod(), FakeRest()
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    monkeypatch.setenv("ICO_RUNPOD_API", runpod.url)
    monkeypatch.setenv("ICO_RUNPOD_REST", rest.url)
    monkeypatch.setenv("ICO_RUNPOD_REST_V2", rest.url_v2)
    monkeypatch.setenv("ICO_WORKDIRS", str(tmp_path / "volume" / "jobs"))
    monkeypatch.setenv(
        "ICO_VOLUME_ROOT", str(tmp_path / "volume")
    )  # the worker's mount
    repo = tmp_path / "repo"
    make_repo(repo, '[targets.gpu]\nendpoint = "ep"\n')
    yield load_config(repo), rest, runpod
    runpod.server.shutdown()
    rest.server.shutdown()


async def test_an_order_brings_its_volume_up_and_down(
    ordered: tuple[Config, FakeRest, FakeRunPod],
) -> None:
    config, rest, runpod = ordered
    async with Client(create_server(config)) as client:
        without = await call_error(
            client, "run", spec=seq("shout@1"), input="x", target="gpu"
        )
        asked = await call(client, "volume_up", target="gpu", datacenter="EU-RO-1")
        up = await call(
            client,
            "volume_up",
            target="gpu",
            datacenter="EU-RO-1",
            size_gb=100,
            confirm=True,
        )
        again = await call(client, "volume_up", target="gpu", datacenter="EU-RO-1")
        started = await call(
            client,
            "run",
            spec={"kind": "op", "op": "shout@1"},
            input="hi",
            target="gpu",
            wait=False,
        )
        done = await finish(client, started["run_id"])
        kept = await call(client, "volume_down", target="gpu")
        down = await call(client, "volume_down", target="gpu", confirm=True)
        gone = await call_error(
            client, "run", spec=seq("shout@1"), input="x", target="gpu"
        )

    assert without["code"] == "no_volume" and gone["code"] == "no_volume"
    assert (
        asked["state"] == "needs_confirmation" and "$10.50 a month" in asked["message"]
    )
    volume = {"id": "vol1", "name": "ico-gpu", "size": 100, "datacenter": "EU-RO-1"}
    assert up == {"state": "up", "volume": volume} and again["state"] == "exists"
    assert rest.calls[:2] == [
        (
            "POST",
            "/networkvolumes",
            {"name": "ico-gpu", "size": 100, "dataCenterId": "EU-RO-1"},
        ),
        (
            "PATCH",
            "/endpoints/ep",
            {"networkVolumeId": "vol1", "dataCenterIds": ["EU-RO-1"]},
        ),
    ]
    assert done["state"] == "done", done.get("kernel_error")
    assert done["result"]["outputs"] == ["HI"]
    [job] = runpod.jobs.values()
    assert job.body["input"]["job"]["volume"] == "vol1"
    assert (
        job.body["input"]["job"]["out"] == f"s3://vol1/ico/runs/{started['run_id']}/out"
    )
    assert kept["state"] == "needs_confirmation"
    assert down["state"] == "down" and rest.volumes == {}
    assert ("PATCH", "/serverless/ep", {"mounts": {"network": []}}) in rest.calls


async def test_a_failed_upload_is_a_storage_error(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the storage needs a directory")
    repo = tmp_path / "repo"
    make_repo(repo, f'[targets.gpu]\nendpoint = "ep"\nstorage = "file://{blocked}"\n')
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    async with Client(create_server(load_config(repo))) as client:
        started = await call(
            client, "run", spec={"kind": "op", "op": "shout@1"}, input=str(source),
            target="gpu", wait=False,
        )  # fmt: skip
        failed = await finish(client, started["run_id"])

    error = failed["kernel_error"]
    assert error["code"] == "storage_error"
    assert error["message"].startswith("uploading the inputs: ")
