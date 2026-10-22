"""``ico-mcp init`` creates a domain repository; the server finds its root."""

import subprocess
import sys
from pathlib import Path

import pytest

from ico_mcp import scaffold
from ico_mcp.config import find_root, load_config

REPO = Path(__file__).resolve().parents[1]


def test_init_creates_a_valid_domain(tmp_path: Path) -> None:
    root = tmp_path / "ico-mcp-video"
    written = scaffold.init(root, package="video")
    config = load_config(root)

    assert config.name == "ico-mcp-video" and config.package == "video"
    assert config.draft_dir.is_dir() and config.pipelines_dir.is_dir()
    assert config.domain_notes.is_file()
    assert (root / "tests" / "test_domain_examples.py").is_file()
    assert (
        "video_draft" not in (root / ".gitignore").read_text()
    )  # drafts are committed
    pyproject = (root / "pyproject.toml").read_text()
    assert "ico-mcp = {" in pyproject and "apriori-ico = {" in pyproject
    assert f'"{root}"' in (root / ".mcp.json").read_text()
    assert len(written) == 10


def test_init_keeps_existing_files(tmp_path: Path) -> None:
    (tmp_path / "ico.toml").write_text('name = "mine"\npackage = "domain"\n')
    written = scaffold.init(tmp_path)

    assert tmp_path / "ico.toml" not in written
    assert load_config(tmp_path).name == "mine"
    assert scaffold.init(tmp_path) == []


def test_init_rejects_a_bad_package(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="identifier"):
        scaffold.init(tmp_path, package="my-domain")


def test_editable_sources_point_at_the_checkouts(tmp_path: Path) -> None:
    root = tmp_path / "domain-repo"
    scaffold.init(root)
    sources = (root / "pyproject.toml").read_text().split("[tool.uv.sources]")[1]

    for line in sources.strip().splitlines():
        if "path =" in line:
            path = line.split('path = "')[1].split('"')[0]
            assert (root / path / "pyproject.toml").is_file()


def test_find_root_walks_up(tmp_path: Path) -> None:
    scaffold.init(tmp_path)
    nested = tmp_path / "pipelines" / "deep"
    nested.mkdir(parents=True)

    assert find_root(nested) == tmp_path.resolve()
    assert find_root(tmp_path.parent) != tmp_path.resolve()


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ico_mcp", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_cli_init_and_missing_root(tmp_path: Path) -> None:
    created = run_cli("init", "video", "--package", "video", cwd=tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = run_cli(cwd=empty)

    assert created.returncode == 0, created.stderr
    assert str(tmp_path / "video" / "ico.toml") in created.stdout
    assert missing.returncode == 1
    assert "no ico.toml in the current directory or its parents" in missing.stderr
