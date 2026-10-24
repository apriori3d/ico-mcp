"""``server_status``: is the running server older than its code on disk?"""

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp import __version__
from ico_mcp.config import Config
from ico_mcp.server import create_server
from ico_mcp.version import STALE_HINT, fingerprint, git_state

pytestmark = pytest.mark.anyio


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "Test")
        monkeypatch.setenv(f"{key}_EMAIL", "test@example.com")


@pytest.fixture
def code(tmp_path: Path) -> Path:
    """A stand-in for the server's packages."""
    package = tmp_path / "code" / "serverpkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text("VERSION = 1\n")
    return package


def committed(config: Config) -> Config:
    git(config.root, "init", "-q", "-b", "main")
    git(config.root, "add", "-A")
    git(config.root, "commit", "-q", "-m", "init")
    return config


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def status(client: Client) -> dict[str, Any]:
    result = await client.call_tool("server_status", {})
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def test_status_of_a_fresh_server(
    config: Config, code: Path, git_env: None
) -> None:
    committed(config)
    commit = git(config.root, "rev-parse", "--short", "HEAD").strip()
    async with Client(create_server(config, code=[code])) as client:
        report = await status(client)
        instructions = client.instructions

    assert report["version"] == __version__
    assert report["running"]["commit"] == report["on_disk"]["commit"] == commit
    assert report["running"]["dirty"] is False
    assert report["running"]["started"].endswith("Z")
    assert report["stale"] is False and "hint" not in report
    assert "server_status" in report["tools"] and "run" in report["tools"]
    assert instructions is not None
    assert instructions.startswith(f"ico-mcp {__version__} ({commit})")


async def test_changed_code_makes_the_server_stale(
    config: Config, code: Path, git_env: None
) -> None:
    committed(config)
    async with Client(create_server(config, code=[code])) as client:
        (config.root / "README.md").write_text("docs only\n")
        git(config.root, "add", "-A")
        git(config.root, "commit", "-q", "-m", "docs")
        after_docs = await status(client)
        (code / "tools.py").write_text("VERSION = 2\n")
        after_code = await status(client)
        reloaded = await client.call_tool("reload", {})

    assert after_docs["stale"] is False
    assert after_docs["on_disk"]["commit"] != after_docs["running"]["commit"]
    assert after_code["stale"] is True
    assert after_code["hint"] == STALE_HINT
    server = cast(dict[str, Any], reloaded.structured_content)["server"]
    assert server["stale"] is True


async def test_without_git(config: Config, code: Path) -> None:
    async with Client(create_server(config, code=[code])) as client:
        report = await status(client)
        instructions = client.instructions

    assert report["running"]["commit"] is None
    assert report["running"]["fingerprint"] == fingerprint([code])
    assert instructions is not None and instructions.startswith(
        f"ico-mcp {__version__} (no git)"
    )


def test_dirty_tracked_files(config: Config, git_env: None) -> None:
    committed(config)
    (config.root / "ico.toml").write_text((config.root / "ico.toml").read_text() + "\n")

    assert git_state(config.root)[1] is True


def test_fingerprint_follows_the_contents(code: Path) -> None:
    before = fingerprint([code])
    (code / "tools.py").write_text("VERSION = 3\n")

    assert fingerprint([code]) != before
    assert len(before) == 12
