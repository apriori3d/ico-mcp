"""Which code the server process runs, and whether the code on disk is newer.

A stdio server process keeps the code it imported at start: after a
``git pull`` the kernel (a new process per run) runs the new operators, but
the server process, its tools and the client's tool list stay old until the
client restarts it. :class:`ServerStatus` makes that visible: it compares a
fingerprint of the server's own packages (``ico_mcp`` and ``ico``) taken at
start with the one on disk now. A fingerprint, not the commit id: it changes
with uncommitted code edits and not with commits that touch only documents
or pipelines.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import ico

import ico_mcp
from ico_mcp.protocol import JsonObject, JsonValue

DEFAULT_PACKAGES = (Path(ico_mcp.__file__).parent, Path(ico.__file__).parent)
"""The packages imported by the server process."""

STALE_HINT = (
    "The server process runs older code than the repository: ask the human "
    "to reconnect (restart) the MCP server to load the new code and tools."
)


@dataclass(frozen=True, slots=True)
class CodeState:
    """The server code at one moment."""

    commit: str | None
    dirty: bool
    fingerprint: str

    def to_dict(self) -> JsonObject:
        return {
            "commit": self.commit,
            "dirty": self.dirty,
            "fingerprint": self.fingerprint,
        }


def fingerprint(packages: Sequence[Path]) -> str:
    """A short hash of the ``.py`` files of ``packages`` (paths and contents)."""
    digest = hashlib.sha256()
    for package in packages:
        for path in sorted(package.rglob("*.py")):
            digest.update(str(path.relative_to(package.parent)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def git_state(root: Path) -> tuple[str | None, bool]:
    """The short commit id of ``root`` and whether its tracked files changed;
    ``(None, False)`` outside a git repository or without git."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, False
    return commit or None, bool(changes.strip())


def code_state(root: Path, packages: Sequence[Path]) -> CodeState:
    commit, dirty = git_state(root)
    return CodeState(commit, dirty, fingerprint(packages))


class ServerStatus:
    """The code a server process started with, compared with the disk."""

    def __init__(self, root: Path, packages: Sequence[Path] = DEFAULT_PACKAGES) -> None:
        self.root = root
        self.packages = tuple(packages)
        self.started = datetime.now(UTC)
        self.running = code_state(root, self.packages)

    @property
    def header(self) -> str:
        """``ico-mcp 0.1.0 (8690327)``, for the server instructions."""
        commit = self.running.commit or "no git"
        dirty = ", modified" if self.running.dirty else ""
        return f"ico-mcp {ico_mcp.__version__} ({commit}{dirty})"

    def report(self, tools: Sequence[str]) -> JsonObject:
        """The running and the on-disk code, ``stale`` and the tool names."""
        on_disk = code_state(self.root, self.packages)
        stale = on_disk.fingerprint != self.running.fingerprint
        running = self.running.to_dict()
        running["started"] = self.started.isoformat().replace("+00:00", "Z")
        names: list[JsonValue] = []
        names.extend(tools)
        report: JsonObject = {
            "version": ico_mcp.__version__,
            "running": running,
            "on_disk": on_disk.to_dict(),
            "stale": stale,
            "tools": names,
        }
        if stale:
            report["hint"] = STALE_HINT
        return report
