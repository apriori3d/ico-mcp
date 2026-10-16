"""Shared fixtures: a repository with stable and draft operators."""

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from ico_mcp.config import Config, load_config

MakeRepo = Callable[..., Config]

STABLE = '''
from dataclasses import dataclass

from ico.registry import register


@dataclass
class Report:
    words: int


@register("count_words", 1, tags={"text"})
def count_words(text: str) -> int:
    """Count the words of a text."""
    return len(text.split())


@register("split_words", 1, tags={"text"})
def split_words(text: str) -> list[str]:
    """Split a text into words."""
    return text.split()


@register("make_report", 1)
def make_report(words: int) -> Report:
    """Wrap a word count into a report."""
    return Report(words)
'''

DRAFT = '''
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass

from ico.registry import register


@dataclass
class Report:
    title: str


@register("shout", 1, tags={"text"})
def shout(text: str) -> str:
    """Upper-case a text, printing noise to stdout."""
    print("noise on stdout")
    return text.upper()


@register("title_report", 1)
def title_report(text: str) -> Report:
    """A report with a title."""
    return Report(text)


@register("fail", 1)
def fail(text: str) -> str:
    """Always raise."""
    raise RuntimeError("boom")


@register("hang", 1)
def hang(pid_file: str) -> str:
    """Start a worker process, record its pid and hang."""
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(pid_file, "w") as f:
        f.write(str(worker.pid))
    time.sleep(60)
    return pid_file


@register("crash", 1)
def crash(text: str) -> str:
    """Exit the process."""
    print("dying", flush=True)
    os._exit(3)


@register("words", 1)
def words(text: str) -> Iterator[str]:
    """Iterate over the words of a text."""
    return iter(text.split())


@register("getenv", 1)
def getenv(name: str) -> str:
    """Read an environment variable."""
    return os.environ.get(name, "<unset>")
'''


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_repo(root: Path, toml_extra: str = "") -> Config:
    for package, source in (("domain", STABLE), ("domain_draft", DRAFT)):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("")
        name = "text" if package == "domain" else "tools"
        (root / package / f"{name}.py").write_text(textwrap.dedent(source))
    (root / "ico.toml").write_text('name = "demo"\npackage = "domain"\n' + toml_extra)
    return load_config(root)


@pytest.fixture
def make_repo() -> MakeRepo:
    """Create a repository: ``make_repo(root, toml_extra="")``."""
    return _make_repo


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _make_repo(tmp_path)


def progress_spec(total: int) -> dict[str, object]:
    """``words@1`` then a stream of ``shout@1`` behind a progress node."""
    body = {
        "kind": "seq",
        "children": [
            {"kind": "progress", "name": "words", "params": {"total": total}},
            {"kind": "op", "op": "shout@1"},
        ],
    }
    return {
        "kind": "seq",
        "children": [
            {"kind": "op", "op": "words@1"},
            {"kind": "stream", "children": [body]},
        ],
    }


@pytest.fixture
def progress_spec_of() -> Callable[[int], dict[str, object]]:
    return progress_spec
