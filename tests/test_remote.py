"""Remote runs: run(target=) against a fake RunPod API on 127.0.0.1 that
runs ICO's real worker handler, with file:// storage; the code goes by
value, local files are uploaded and outputs come back."""

import asyncio
import itertools
import json
import textwrap
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config, ConfigError, load_config
from ico_mcp.kernel_client import KernelClient
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

    def reply(self, request: BaseHTTPRequestHandler, body: object) -> None:
        data = json.dumps(body).encode()
        request.send_response(200)
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    def work(self, id: str, job: FakeJob) -> None:
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
def remote(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Config]:
    fake = FakeRunPod()
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    monkeypatch.setenv("ICO_RUNPOD_API", fake.url)
    monkeypatch.setenv("ICO_WORKDIRS", str(tmp_path / "volume" / "jobs"))
    storage = f"file://{tmp_path / 'bucket'}"
    repo = tmp_path / "repo"
    make_repo(repo, f'[targets.gpu]\nendpoint = "ep"\nstorage = "{storage}"\n')
    (repo / "domain_draft" / "files.py").write_text(textwrap.dedent(FILES))
    yield load_config(repo)
    fake.server.shutdown()


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


async def test_a_run_goes_to_the_target(remote: Config) -> None:
    async with Client(create_server(remote)) as client:
        started = await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="a b c",
            target="gpu", wait=False,
        )  # fmt: skip
        done = await finish(client, started["run_id"])
        board = await call(client, "dashboard")

    assert done["state"] == "done", done
    assert done["result"]["outputs"] == [3]
    assert done["result"]["target"] == "gpu"
    assert done["title"] == "count_words@1, shout@1 @ gpu"
    assert board["url"]


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

    assert done["state"] == "done", done
    [output] = done["result"]["outputs"]
    local = remote.runs_dir / started["run_id"] / "out" / "notes.txt"
    assert output == str(local) and local.read_text() == "HELLO1"
    uploaded = done["result"]["uploaded"][str(source)]
    assert uploaded.endswith(f"/runs/{started['run_id']}/in/notes.txt")


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


def test_keys_reach_only_remote_kernels(remote: Config) -> None:
    local = KernelClient(remote)._env()  # pyright: ignore[reportPrivateUsage]
    far = KernelClient(remote, remote=True)._env()  # pyright: ignore[reportPrivateUsage]
    assert "RUNPOD_API_KEY" not in local and far["RUNPOD_API_KEY"] == KEY


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
    with pytest.raises(ConfigError, match="targets.x.storage: expected an s3://"):
        make_repo(tmp_path / "b", '[targets.x]\nendpoint = "e"\nstorage = "/tmp"\n')
