import dataclasses
import json
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp import drafts
from ico_mcp.config import Config, Timeouts
from ico_mcp.server import create_server

pytestmark = pytest.mark.anyio

MakeRepo = Any

SLUGIFY = '''from ico.registry import register


@register("slugify", 1, tags={"text"})
def slugify(text: str) -> str:
    """Lower-case words joined by dashes."""
    return "-".join(text.lower().split())
'''

FRAMES = '''from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    """A video frame on disk."""

    path: str
    index: int
'''

VIDEO = '''from ico.registry import register

from .frames import Frame


@register("first_frame", 1)
def first_frame(path: str) -> Frame:
    """The first frame of a video."""
    return Frame(path, 0)
'''

LOOSE = '''from ico.registry import register


def helper(text):
    return text


@register("loose", 1)
def loose(text: str) -> str:
    """Typed operator with an untyped helper: fails pyright strict."""
    return str(helper(text))
'''

FAKE_GH = """#!/bin/sh
echo "$@" >> "$GH_LOG"
if [ "$1" = auth ]; then exit "${GH_AUTH_EXIT:-0}"; fi
if [ "$1" = pr ]; then
  if [ -n "$GH_HOLD" ]; then
    while [ ! -e "$GH_HOLD" ]; do sleep 0.05; done
  fi
  while [ $# -gt 0 ]; do
    if [ "$1" = --body-file ]; then cp "$2" "$GH_BODY"; fi
    shift
  done
  if [ -n "$GH_PR_EXIT" ]; then echo "no permission" >&2; exit "$GH_PR_EXIT"; fi
  echo "https://github.com/acme/demo/pull/7"
fi
"""


# ─── Fixtures ───


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def repo(
    tmp_path: Path, make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch
) -> Config:
    """A clone with a GitHub target, a bare ``origin`` and a fake ``gh``."""
    for key, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    config = cast(
        Config,
        make_repo(root, 'github = { repo = "acme/demo", base = "main" }\n'),
    )
    (root / "tests").mkdir()
    shutil.copy(
        Path(__file__).with_name("test_domain_examples.py"),
        root / "tests" / "test_domain_examples.py",
    )
    (root / ".gitignore").write_text(
        "__pycache__/\nruns/\ndomain_draft/*\n!domain_draft/__init__.py\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Template")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(root), str(bare))
    git(root, "remote", "add", "origin", str(bare))
    git(root, "fetch", "-q", "origin")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("GH_BODY", str(tmp_path / "pr-body.md"))
    return config


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


def branch_file(repo: Config, branch: str, path: str) -> str:
    bare = repo.root.parent / "origin.git"
    return git(bare, "show", f"{branch}:{path}")


# ─── Pull request ───


async def test_crystallize_opens_a_pull_request(repo: Config) -> None:
    examples = [{"ref": "slugify@1", "input": "A B", "output": "a-b"}]
    spec = {"kind": "op", "op": "slugify@1"}
    async with Client(create_server(repo)) as client:
        await call(
            client, "propose_operator", module="slug", code=SLUGIFY, examples=examples
        )
        await call(client, "propose_types", module="frames", code=FRAMES)
        await call(client, "propose_operator", module="video", code=VIDEO)
        await call(
            client, "save_pipeline", name="slug", spec=spec, description="Slugs."
        )
        await call(client, "run", spec=spec, input="X Y")
        await call(
            client, "propose_operator", module="slug",
            edits=[{"old": "Lower-case words", "new": "Lower-case words,"}],
        )  # fmt: skip
        await call(client, "run", spec=spec, input="Z")
        opened = await call(
            client,
            "crystallize",
            refs=["first_frame@1", "slugify@1"],
            title="Stable video ops",
            summary="Frames and slugs.",
            pipelines=["slug"],
        )
        still = await call(client, "search_operators", text="slugify")

    assert opened["pr_url"] == "https://github.com/acme/demo/pull/7"
    assert opened["branch"] == "crystallize/stable-video-ops"
    assert opened["modules"] == ["frames", "slug", "video"]
    assert opened["added_modules"] == ["frames"]
    assert all(step["ok"] for step in opened["gate"])
    branch = opened["branch"]
    assert branch_file(repo, branch, "domain/video.py") == VIDEO
    assert branch_file(repo, branch, "domain/frames.py") == FRAMES
    assert json.loads(branch_file(repo, branch, "examples/slugify@1.json"))
    assert json.loads(branch_file(repo, branch, "pipelines/slug.spec.json"))
    log = (repo.root.parent / "gh.log").read_text()
    assert (
        "pr create --repo acme/demo --base main --head crystallize/stable-video-ops"
        in log
    )
    body = (repo.root.parent / "pr-body.md").read_text()
    assert "Frames and slugs." in body and "`first_frame@1`" in body
    assert "| `slugify@1` | 2 | 2 |" in body
    assert "Added as relative imports of the moved modules: frames" in body
    assert "## Recent successful runs" in body
    recent = body.split("## Recent successful runs")[1]
    assert "input `Z` · 1 output(s) · 0 item error(s)" in recent
    [earlier] = [line for line in recent.splitlines() if "input `X Y`" in line]
    assert earlier.endswith("(earlier draft code)")
    assert "input `Z`" in recent and "`Z` · 1 output(s) · 0 item error(s) ·" in recent
    assert "`first_frame@1`:\n- none recorded" in recent
    # The working tree is untouched: drafts stay and keep loading.
    assert not (repo.stable_dir / "video.py").exists()
    assert (repo.draft_dir / "video.py").exists()
    assert still["cards"][0]["status"] == "draft"
    assert git(repo.root, "worktree", "list").count("\n") == 1


async def test_failing_gate_pushes_nothing(repo: Config) -> None:
    async with Client(create_server(repo)) as client:
        await call(
            client, "propose_operator", module="loose", code=LOOSE, typecheck=False
        )
        failed = await call_error(
            client, "crystallize", refs=["loose@1"], title="Loose", summary="No."
        )

    assert (failed["code"], failed["stage"]) == ("crystallize_failed", "gate")
    steps = {step["step"]: step for step in failed["gate"]}
    assert not steps["pyright strict"]["ok"]
    assert steps["examples"]["ok"] and steps["no draft imports"]["ok"]
    bare = repo.root.parent / "origin.git"
    assert "crystallize/loose" not in git(bare, "branch", "--list")
    assert "crystallize/loose" not in git(repo.root, "branch", "--list")
    assert git(repo.root, "worktree", "list").count("\n") == 1


async def test_checks(repo: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    async with Client(create_server(repo)) as client:
        stable = await call_error(
            client, "crystallize", refs=["count_words@1"], title="t", summary="s"
        )
        missing = await call_error(
            client, "crystallize", refs=["shout@1"], title="t", summary="s",
            pipelines=["nope"],
        )  # fmt: skip
        monkeypatch.setenv("GH_AUTH_EXIT", "1")
        unauthenticated = await call_error(
            client, "crystallize", refs=["shout@1"], title="t", summary="s"
        )

    assert stable["stage"] == missing["stage"] == unauthenticated["stage"] == "check"
    assert "not a draft" in stable["message"]
    assert "no saved pipeline" in missing["message"]
    assert "gh auth login" in unauthenticated["message"]


async def test_github_must_be_configured(config: Config) -> None:
    async with Client(create_server(config)) as client:
        error = await call_error(
            client, "crystallize", refs=["shout@1"], title="t", summary="s"
        )

    assert error["stage"] == "check"
    assert "github" in error["message"]


# ─── Background ───


async def test_a_long_crystallization_goes_on_in_the_background(
    repo: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = dataclasses.replace(repo, timeouts=Timeouts(wait=1))
    release = tmp_path / "release"
    monkeypatch.setenv("GH_HOLD", str(release))  # gh pr create waits for it
    async with Client(create_server(config)) as client:
        await call(client, "propose_operator", module="slug", code=SLUGIFY)
        clock = time.monotonic()
        started = await call(
            client, "crystallize", refs=["slugify@1"], title="Slug", summary="s"
        )
        elapsed = time.monotonic() - clock
        crystallize_id = started["crystallize_id"]
        still = await call(
            client, "wait_crystallize", crystallize_id=crystallize_id, timeout=600
        )
        # The drafts are free while the gate and the push go on.
        clock = time.monotonic()
        await call(client, "propose_operator", module="pad", code=PAD)
        proposed_in = time.monotonic() - clock
        release.touch()
        done = await call(client, "wait_crystallize", crystallize_id=crystallize_id)
        again = await call(client, "wait_crystallize", crystallize_id=crystallize_id)
        missing = await call_error(client, "wait_crystallize", crystallize_id="c-0")
        too_short = await call_error(
            client, "wait_crystallize", crystallize_id=crystallize_id, timeout=0
        )

    assert started["state"] == "running" and started["waited"] == 1
    assert started["stage"] in {"git", "gate", "publish"}
    assert 0.9 <= elapsed < 10
    assert still["state"] == "running" and still["waited"] == 1  # cut to 1 s
    assert proposed_in < 10
    assert done["state"] == "done" and done["crystallize_id"] == crystallize_id
    assert done["pr_url"] == "https://github.com/acme/demo/pull/7"
    assert done["modules"] == ["slug"]
    assert again == done  # the result stays reachable
    assert missing["code"] == "not_found"
    assert too_short["code"] == "bad_request"
    assert git(repo.root, "worktree", "list").count("\n") == 1


async def test_a_background_failure_is_reported_by_wait_crystallize(
    repo: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = dataclasses.replace(repo, timeouts=Timeouts(wait=1))
    release = tmp_path / "release"
    monkeypatch.setenv("GH_HOLD", str(release))
    monkeypatch.setenv("GH_PR_EXIT", "1")
    async with Client(create_server(config)) as client:
        await call(client, "propose_operator", module="slug", code=SLUGIFY)
        started = await call(
            client, "crystallize", refs=["slugify@1"], title="Slug", summary="s"
        )
        release.touch()
        failed = await call_error(
            client, "wait_crystallize", crystallize_id=started["crystallize_id"]
        )

    assert started["state"] == "running"
    assert (failed["code"], failed["stage"]) == ("crystallize_failed", "publish")
    assert failed["crystallize_id"] == started["crystallize_id"]
    assert "no permission" in failed["output"]


async def test_server_exit_stops_a_crystallization_and_removes_its_worktree(
    repo: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = dataclasses.replace(repo, timeouts=Timeouts(wait=1))
    monkeypatch.setenv("GH_HOLD", str(tmp_path / "never"))
    async with Client(create_server(config)) as client:
        await call(client, "propose_operator", module="slug", code=SLUGIFY)
        started = await call(
            client, "crystallize", refs=["slugify@1"], title="Slug", summary="s"
        )

    assert started["state"] == "running"
    assert git(repo.root, "worktree", "list").count("\n") == 1


# ─── Drafts after the merge ───


def test_closure_follows_relative_imports(config: Config) -> None:
    (config.draft_dir / "frames.py").write_text(FRAMES)
    (config.draft_dir / "video.py").write_text(VIDEO)
    (config.draft_dir / "clip.py").write_text("from . import video\n")

    assert drafts.closure(config.draft_dir, ["clip"]) == ["clip", "frames", "video"]
    assert drafts.closure(config.draft_dir, ["frames"]) == ["frames"]


def test_retire_removes_identical_drafts_and_rewrites_imports(config: Config) -> None:
    (config.draft_dir / "frames.py").write_text(FRAMES)
    (config.stable_dir / "frames.py").write_text(FRAMES)
    (config.draft_dir / "video.py").write_text(VIDEO)
    (config.draft_dir / "clip.py").write_text(
        textwrap.dedent("""\
            from . import frames, video
            from .frames import Frame as F
        """)
    )
    (config.draft_dir / "edited.py").write_text("X = 1\n")
    (config.stable_dir / "edited.py").write_text("X = 0\n")

    report = drafts.retire(config)

    assert report["retired"] == ["frames"]
    assert sorted(cast(list[str], report["rewritten"])) == ["clip", "video"]
    assert [c["module"] for c in cast(list[dict[str, Any]], report["conflicts"])] == [
        "edited"
    ]
    assert not (config.draft_dir / "frames.py").exists()
    assert (config.draft_dir / "edited.py").exists()
    assert (
        "from domain.frames import Frame" in (config.draft_dir / "video.py").read_text()
    )
    clip = (config.draft_dir / "clip.py").read_text()
    assert "from domain.frames import Frame as F" in clip
    assert "from . import frames, video" in clip  # mixed: video stays a draft


async def test_reload_retires_merged_drafts(config: Config) -> None:
    (config.draft_dir / "frames.py").write_text(FRAMES)
    (config.draft_dir / "video.py").write_text(VIDEO)
    async with Client(create_server(config)) as client:
        (config.stable_dir / "frames.py").write_text(FRAMES)  # merged and pulled
        report = await call(client, "reload")
        frame = await call(client, "get_type", name="Frame")

    assert report["drafts"]["retired"] == ["frames"]
    assert report["drafts"]["rewritten"] == ["video"]
    assert report["draft"]["errors"] == []
    assert frame["status"] == "stable"


PAD = '''from ico import IcoOperator
from ico.registry import register


@register("pad", 1)
class Pad(IcoOperator[str, str]):
    """Pad a text on the left."""

    def __init__(self, width: int) -> None:
        self.width = width
        super().__init__(fn=self._apply)

    def _apply(self, text: str) -> str:
        return text.rjust(self.width)
'''


async def test_gate_validates_pipelines_with_their_check_params(repo: Config) -> None:
    spec = {"kind": "op", "op": "pad@1", "params": {"width": {"$param": "width"}}}
    async with Client(create_server(repo)) as client:
        await call(client, "propose_operator", module="pad", code=PAD)
        await call(
            client, "save_pipeline", name="padded", spec=spec, description="",
            params={"width": 8},
        )  # fmt: skip
        opened = await call(
            client, "crystallize", refs=["pad@1"], title="Pad", summary="s",
            pipelines=["padded"],
        )  # fmt: skip

    steps = {step["step"]: step for step in opened["gate"]}
    assert steps["load and validate pipelines"]["ok"]
    saved = json.loads(
        branch_file(repo, opened["branch"], "pipelines/padded.spec.json")
    )
    assert saved["check_params"] == {"width": 8}
    assert saved["root"]["params"] == {"width": {"$param": "width"}}
