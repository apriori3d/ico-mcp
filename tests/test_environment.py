"""``environment``: what operators see; ``bin:`` capabilities."""

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config
from ico_mcp.environment import bin_names, duplicates
from ico_mcp.server import create_server

pytestmark = pytest.mark.anyio

MakeRepo = Any


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def test_environment_report(
    tmp_path: Path, make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = cast(
        Config, make_repo(tmp_path, 'env = ["ICO_TEST_SET", "ICO_TEST_UNSET"]\n')
    )
    monkeypatch.setenv("ICO_TEST_SET", "secret-value")
    monkeypatch.delenv("ICO_TEST_UNSET", raising=False)
    async with Client(create_server(config)) as client:
        report = await call(client, "environment")
        filtered = await call(client, "environment", match="ANYIO")
        several = await call(client, "environment", match=["anyio", "pydantic"])

    assert report["python"]["version"].count(".") == 2
    assert report["path"] and all(isinstance(p, str) for p in report["path"])
    assert report["env"] == [
        {"name": "ICO_TEST_SET", "set": True},
        {"name": "ICO_TEST_UNSET", "set": False},
    ]
    assert "secret-value" not in json.dumps(report)
    assert report["timeouts"] == {"run": 600, "validate": 30, "load": 60}
    assert any(p.startswith("anyio==") for p in report["packages"])
    assert [p.split("==")[0].lower() for p in filtered["packages"]] == ["anyio"]
    names = {p.split("==")[0].lower() for p in several["packages"]}
    assert {"anyio", "pydantic"} <= names
    assert all("anyio" in n or "pydantic" in n for n in names)
    assert isinstance(report["duplicates"], list)
    assert report["bins"] == {
        "ico-test-missing-binary": None,
        "sh": shutil.which("sh"),
    }


async def test_bins_on_cards_and_validate_warnings(config: Config) -> None:
    spec = {
        "kind": "seq",
        "children": [{"kind": "op", "op": "shout@1"}, {"kind": "op", "op": "shell@1"}],
    }
    async with Client(create_server(config)) as client:
        card = await call(client, "get_operator", ref="shell@1")
        plain = await call(client, "get_operator", ref="shout@1")
        checked = await call(client, "validate", spec=spec)
        clean = await call(client, "validate", spec={"kind": "op", "op": "shout@1"})

    assert card["bins"] == {"ico-test-missing-binary": None, "sh": shutil.which("sh")}
    assert "bins" not in plain
    [warning] = checked["issues"]
    assert (warning["severity"], warning["kind"], warning["path"]) == (
        "warning",
        "missing_binary",
        "1",
    )
    assert "`ico-test-missing-binary`" in warning["message"]
    assert warning["refs"] == ["shell@1"]
    assert clean["issues"] == []


def test_bin_names() -> None:
    caps = {"gpu", "bin:ffmpeg", "bin:ffprobe", "bin:", "network"}
    assert bin_names(caps) == ["ffmpeg", "ffprobe"]


def test_duplicates_ignore_namespace_packages() -> None:
    owners = {
        "cv2": ["opencv-python", "opencv-contrib-python", "opencv-python"],
        "google": ["protobuf", "googleapis-common-protos"],
        "numpy": ["numpy"],
    }
    versions = {
        "opencv-python": "5.0",
        "opencv-contrib-python": "4.10",
        "protobuf": "5",
    }
    namespace = {"google"}

    found = duplicates(owners, versions, lambda dist, pkg: pkg not in namespace)

    assert found == [
        {
            "package": "cv2",
            "distributions": ["opencv-contrib-python==4.10", "opencv-python==5.0"],
        }
    ]
