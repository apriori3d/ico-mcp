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
            tmp_path / "a", '[assets]\nroot = "~/models"\n[resources]\ngpu = "8GB"\n'
        ),
    )
    assert config.assets.root == Path("~/models").expanduser()
    (config.root / "assets.toml").write_text("")
    env = asset_env(config)
    assert env["ICO_ASSETS_MANIFEST"] == str(config.root / "assets.toml")
    assert env["ICO_ASSETS_ROOT"] == str(Path("~/models").expanduser())
    assert env["ICO_RESOURCES_GPU"] == "8GB" and "ICO_RESOURCES_RAM" not in env
    with pytest.raises(ConfigError, match="resources.ram: expected a size"):
        make_repo(tmp_path / "b", '[resources]\nram = "lots"\n')
    with pytest.raises(ConfigError, match="unknown keys: assets.path"):
        make_repo(tmp_path / "c", '[assets]\npath = "x"\n')
