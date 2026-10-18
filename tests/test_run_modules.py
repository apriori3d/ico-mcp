"""A run's kernel imports only the operator modules its spec uses."""

from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.server import create_server

pytestmark = pytest.mark.anyio

HEAVY = '''import os
from pathlib import Path

from ico.registry import register

# Stands in for a heavy framework: records every process importing it.
with open(Path(__file__).parent.parent / "heavy_imports.txt", "a") as log:
    log.write(f"{os.getpid()}\\n")


@register("heavy", 1)
def heavy(text: str) -> str:
    """The pid of the process running it."""
    return str(os.getpid())
'''

LIGHT = '''import os

from ico.registry import register


@register("light", 1)
def light(text: str) -> str:
    """The pid of the process running it."""
    return str(os.getpid())
'''


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def run(client: Client, ref: str) -> dict[str, Any]:
    result = await client.call_tool(
        "run", {"spec": {"kind": "op", "op": ref}, "input": "x"}
    )
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


def imported_by(config: Config) -> set[str]:
    log = config.root / "heavy_imports.txt"
    return set(log.read_text().split()) if log.exists() else set()


@pytest.fixture
def modules(config: Config) -> Config:
    (config.draft_dir / "heavy.py").write_text(HEAVY)
    (config.draft_dir / "light.py").write_text(LIGHT)
    return config


async def test_a_run_imports_only_the_modules_it_uses(modules: Config) -> None:
    async with Client(create_server(modules)) as client:
        light = await run(client, "light@1")
        heavy = await run(client, "heavy@1")

    light_pid, heavy_pid = light["outputs"][0], heavy["outputs"][0]
    assert light_pid != heavy_pid  # each run has a kernel of its own
    assert light_pid not in imported_by(modules)
    assert heavy_pid in imported_by(modules)


async def test_catalog_lists_modules_and_unknown_refs(modules: Config) -> None:
    async with KernelClient(modules) as kernel:
        found = await kernel.request(
            "modules", {"refs": ["light@1", "shout@1", "nope@1"]}
        )

    assert found == {
        "modules": ["domain_draft.light", "domain_draft.tools"],
        "unknown": ["nope@1"],
    }


async def test_a_kernel_loads_selected_modules(modules: Config) -> None:
    kernel = KernelClient(modules, modules=["domain_draft.light", "domain.text"])
    async with kernel:
        report = cast(dict[str, Any], await kernel.start())
        card = await kernel.request("search", {"text": "pid"})

    assert report["draft"]["loaded"] == ["light@1"]
    assert "count_words@1" in report["stable"]["loaded"]
    assert [c["name"] for c in cast(dict[str, Any], card)["cards"]] == ["light"]


async def test_unknown_refs_fall_back_to_loading_everything(modules: Config) -> None:
    async with Client(create_server(modules)) as client:
        result = await client.call_tool(
            "run", {"spec": {"kind": "op", "op": "nope@1"}, "input": "x"}
        )

    assert result.is_error
    assert "nope@1" in text(result)
