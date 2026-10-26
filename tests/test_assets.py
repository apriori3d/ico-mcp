"""Assets and resources in the kernel: validate warnings, operator cards,
environment, and the ico.toml blocks that reach the kernel."""

import hashlib
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

from ico_mcp.config import Config, ConfigError, load_config
from ico_mcp.kernel_client import KernelClient, asset_env
from ico_mcp.protocol import JsonObject

MakeRepo = Any

pytestmark = pytest.mark.anyio

REV = "ee6f4a40737a995bf5818954cfce6d59443b0f04"
DATA = b"weights!"
SHA = hashlib.sha256(DATA).hexdigest()

MODELS = '''
from dataclasses import dataclass

from ico import IcoOperator
from ico.assets import AssetId
from ico.registry import register
from ico.resources import Resource, resource


@resource("demo.encoder", 1)
@dataclass(frozen=True)
class Encoder(Resource[str]):
    """A text encoder."""

    weights: AssetId = "encoder"

    def load(self) -> str:
        return self.weights


ENCODER = Encoder()


@register("encode", 1, tags={"gpu"})
class Encode(IcoOperator[str, str]):
    """Encode a text with the encoder and a table."""

    def __init__(self, encoder: Encoder = ENCODER, table: AssetId = "table") -> None:
        self.encoder = encoder
        self.table = table
        super().__init__(fn=self._apply)

    def _apply(self, text: str) -> str:
        with self.encoder.use() as model:
            return f"{model}:{text}"
'''

MANIFEST = f"""
[assets.encoder]
hf = {{ repo = "org/enc", revision = "{REV}", file = "enc.bin" }}

[assets.table]
url = "https://example.com/table.json"
sha256 = "{SHA}"

[assets.big]
url = "https://example.com/big.bin"
sha256 = "{SHA}"
"""

LOCK = f"""
version = 1

[[asset]]
id = "encoder"
kind = "file"
files = [ {{ name = "enc.bin", size = {len(DATA)}, sha256 = "{SHA}" }} ]

[[asset]]
id = "table"
kind = "file"
files = [ {{ name = "table.json", size = {len(DATA)}, sha256 = "{SHA}" }} ]
"""


@pytest.fixture
def domain(make_repo: MakeRepo, tmp_path: Path) -> Config:
    repo = tmp_path / "repo"
    make_repo(repo, '[assets]\nroot = "store"\n[resources]\nram = "2GB"\n')
    (repo / "domain" / "models.py").write_text(textwrap.dedent(MODELS))
    (repo / "assets.toml").write_text(MANIFEST)
    (repo / "assets.lock").write_text(LOCK)
    (repo / "store" / "encoder").mkdir(parents=True)
    (repo / "store" / "encoder" / "enc.bin").write_bytes(DATA)  # the table is missing
    return load_config(repo)


def encode(**params: Any) -> JsonObject:
    return {"kind": "op", "op": "encode@1", "params": params}


async def test_validate_reports_missing_and_unknown_assets(domain: Config) -> None:
    async with KernelClient(domain) as kernel:
        await kernel.start()
        result = cast(
            dict[str, Any], await kernel.request("validate", {"spec": encode()})
        )
        wrong = cast(
            dict[str, Any],
            await kernel.request("validate", {"spec": encode(table="tabel")}),
        )

    kinds = {i["kind"]: i for i in result["issues"]}
    assert set(kinds) == {"missing_asset"}
    assert "asset 'table' is missing" in kinds["missing_asset"]["message"]
    assert "ask the human" in kinds["missing_asset"]["message"]
    unknown = [i for i in wrong["issues"] if i["kind"] == "unknown_asset"]
    assert unknown and unknown[0]["severity"] == "error"
    assert "known: big, encoder, table" in unknown[0]["message"]


async def test_cards_and_environment_show_assets(domain: Config) -> None:
    async with KernelClient(domain) as kernel:
        await kernel.start()
        card = cast(dict[str, Any], await kernel.request("card", {"ref": "encode@1"}))
        env = cast(dict[str, Any], await kernel.request("environment", {}))
        ran = cast(
            dict[str, Any],
            await kernel.request("run", {"spec": encode(), "input": "hi"}),
        )

    params = {p["name"]: p for p in card["params"]}
    assert params["encoder"]["resource"] == "demo.encoder@1"
    assert params["table"]["asset"] is True
    assert [(a["id"], a["state"]) for a in card["assets"]] == [
        ("encoder", "present"),
        ("table", "missing"),
    ]
    assets = env["assets"]
    assert assets["root"] == str(domain.root / "store")
    states = {a["id"]: a["state"] for a in assets["items"]}
    assert states == {"big": "unlocked", "encoder": "present", "table": "missing"}
    assert env["resources"]["ram"] == 2 * 10**9
    assert ran["outputs"] == ["encoder:hi"]


async def test_no_manifest_is_a_warning(make_repo: MakeRepo, tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    make_repo(repo)
    (repo / "domain" / "models.py").write_text(textwrap.dedent(MODELS))
    config = load_config(repo)
    async with KernelClient(config) as kernel:
        await kernel.start()
        result = cast(
            dict[str, Any], await kernel.request("validate", {"spec": encode()})
        )
        env = cast(dict[str, Any], await kernel.request("environment", {}))
    assert [i["kind"] for i in result["issues"]] == ["no_assets_manifest"]
    assert env["assets"] == {"manifest": None}


def test_ico_toml_blocks(make_repo: MakeRepo, tmp_path: Path) -> None:
    config = cast(
        Config,
        make_repo(
            tmp_path / "a",
            '[assets]\nroot = "~/models"\n[resources]\ngpu_free = "30%"\n',
        ),
    )
    assert config.assets.root == Path("~/models").expanduser()
    (config.root / "assets.toml").write_text("")
    env = asset_env(config)
    assert env["ICO_ASSETS_MANIFEST"] == str(config.root / "assets.toml")
    assert env["ICO_ASSETS_ROOT"] == str(Path("~/models").expanduser())
    assert env["ICO_RESOURCES_GPU_FREE"] == "30%" and "ICO_RESOURCES_RAM" not in env
    with pytest.raises(ConfigError, match="resources.ram: expected a size"):
        make_repo(tmp_path / "b", '[resources]\nram = "lots"\n')
    with pytest.raises(ConfigError, match="resources.gpu_free: expected a share"):
        make_repo(tmp_path / "d", '[resources]\ngpu_free = "8GB"\n')
    with pytest.raises(ConfigError, match="unknown keys: assets.path"):
        make_repo(tmp_path / "c", '[assets]\npath = "x"\n')


# ─── Tools: assets_status, fetch_assets ───


class Files:
    """Plain files over HTTP on 127.0.0.1 (the source of url assets)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.files = files

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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


async def call(client: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return cast(dict[str, Any], result.structured_content)


async def call_error(client: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    import json

    result = await client.call_tool(tool, arguments)
    assert result.is_error
    message = "".join(getattr(c, "text", "") for c in result.content)
    return cast(dict[str, Any], json.loads(message[message.index("{") :]))


async def wait_done(client: Any, run_id: str) -> dict[str, Any]:
    while True:
        status = await call(client, "wait_run", run_id=run_id, timeout=5)
        if status["state"] != "running":
            return status


async def test_fetch_assets_runs_as_a_job(domain: Config) -> None:
    from mcp.client import Client

    from ico_mcp.server import create_server

    files = Files({"table.json": DATA})
    manifest = domain.assets_manifest
    manifest.write_text(manifest.read_text().replace("https://example.com", files.url))
    async with Client(create_server(domain)) as client:
        before = await call(client, "assets_status", spec=encode())
        locked_only = await call_error(client, "fetch_assets")
        started = await call(client, "fetch_assets", spec=encode())
        finished = await wait_done(client, started["run_id"])
        after = await call(client, "assets_status", ids=["table"])
        again = await call(client, "fetch_assets", ids=["table"])
    files.server.shutdown()

    assert [(i["id"], i["state"]) for i in before["items"]] == [
        ("encoder", "present"),
        ("table", "missing"),
    ]
    assert before["to_fetch"] == len(DATA)
    assert (
        locked_only["code"] == "not_locked"
        and "big (unlocked)" in locked_only["message"]
    )
    assert started["assets"] == ["table"] and started["state"] == "running"
    assert finished["state"] == "done"
    assert finished["title"] == "fetch assets"
    outputs = finished["result"]["outputs"]
    assert outputs[-2]["percent"] == 100
    assert outputs[-1]["fetched"][0]["state"] == "present"
    assert after["items"][0]["state"] == "present"
    assert (domain.root / "store" / "table" / "table.json").read_bytes() == DATA
    assert again["state"] == "present"


async def test_a_large_fetch_needs_the_humans_word(domain: Config) -> None:
    from mcp.client import Client

    from ico_mcp.server import create_server

    lock = domain.root / "assets.lock"
    big = LOCK.replace(
        f'id = "table"\nkind = "file"\nfiles = [ {{ name = "table.json", size = {len(DATA)}',
        'id = "table"\nkind = "file"\nfiles = [ { name = "table.json", size = 3000000000',
    )
    lock.write_text(big)
    async with Client(create_server(domain)) as client:
        asked = await call(client, "fetch_assets", ids=["table"])
        searched = await call(client, "search_operators", text="fetch")

    assert asked["state"] == "needs_confirmation" and asked["to_fetch"] == 3 * 10**9
    assert "ask the human" in asked["message"]
    assert searched["cards"] == []  # the built-in fetch is not a domain operator


async def test_a_profile_shows_the_resources(domain: Config) -> None:
    import json

    from mcp.client import Client

    from ico_mcp.server import create_server

    async with Client(create_server(domain)) as client:
        ran = await call(client, "run", spec=encode(), input="hi", profile=True)

    [row] = ran["profile"]["resources"]
    assert (row["resource"], row["process"], row["uses"], row["loads"], row["reloads"]) == (
        "demo.encoder@1", "main", "1", "1", "0",
    )  # fmt: skip
    [trace] = [
        json.loads(line)
        for file in sorted(domain.runs_dir.glob("*.jsonl"))
        for line in file.read_text().splitlines()
    ]
    assert trace["resources"][0]["ref"] == "demo.encoder@1"
    assert trace["resources"][0]["loads"] == 1


async def test_reload_reads_ico_toml_again(domain: Config) -> None:
    from mcp.client import Client

    from ico_mcp.server import create_server

    toml = domain.root / "ico.toml"
    async with Client(create_server(domain)) as client:
        toml.write_text(toml.read_text().replace('ram = "2GB"', 'ram = "3GB"'))
        first = await call(client, "reload")
        env = await call(client, "environment")
        toml.write_text("board = { port = 4321 }\n" + toml.read_text())  # top level
        second = await call(client, "reload")

    assert first["config"] == {"changed": ["resources"], "reconnect": []}
    assert env["resources"]["ram"] == 3 * 10**9
    assert second["config"] == {"changed": ["board"], "reconnect": ["board"]}


async def test_run_status_of_a_run_whose_resources_have_fields(domain: Config) -> None:
    """Resource fields reach clients as JSON: a tuple in a result made MCP
    refuse run_status and wait_run of such runs (the Wan2.2 experts)."""
    from mcp.client import Client

    from ico_mcp.server import create_server

    spec = encode(encoder={"ref": "demo.encoder@1", "params": {"weights": "table"}})
    async with Client(create_server(domain)) as client:
        ran = await call(client, "run", spec=spec, input="hi", profile=True)
        status = await call(client, "run_status", run_id=ran["run_id"])
        waited = await call(client, "wait_run", run_id=ran["run_id"])

    for result in (ran, status["result"], waited["result"]):
        [row] = result["profile"]["resources"]
        assert row["resource"] == "demo.encoder@1 (weights=table)"
        assert (row["ref"], row["args"]) == ("demo.encoder@1", [["weights", "table"]])
