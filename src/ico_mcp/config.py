"""Repository configuration: ``ico.toml`` and the domain layout.

The repository root is the domain root (see ``docs/design/ico-mcp.md``,
"Repository layout")::

    <root>/
      ico.toml
      <package>/            stable operators and types
      <package>_draft/      drafts written by the agent
      pipelines/            saved specs
      examples/             examples for JSON-io operators
      runs/                 run traces (gitignored)
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

CONFIG_FILE = "ico.toml"

TypecheckMode = Literal["basic", "standard", "strict"]

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class ConfigError(ValueError):
    """``ico.toml`` is missing or invalid, or the layout does not match it."""


@dataclass(frozen=True, slots=True)
class GithubConfig:
    """Target of crystallization pull requests.

    Attributes:
        repo: ``owner/name``.
        base: Branch the pull requests are based on.
    """

    repo: str
    base: str = "main"


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Timeouts in seconds: kernel requests (``run``, ``validate``,
    ``load``) and the longest a tool call waits for a run (``wait``)."""

    run: float = 600
    validate: float = 30
    load: float = 60
    wait: float = 45
    """The longest ``run(wait=true)`` and ``wait_run`` block before they
    return a running run; keep it below the client's tool-call timeout
    (about 60 s in the Claude app), which restarts the server and loses the
    run."""


@dataclass(frozen=True, slots=True)
class Config:
    """Loaded ``ico.toml`` of a repository.

    Attributes:
        root: The repository root (the directory of ``ico.toml``).
        name: Domain name.
        package: Stable operator package; drafts are in ``<package>_draft``.
        github: Pull request target; ``None`` until set (``crystallize``
            requires it).
        timeouts: Kernel request timeouts.
        env: Environment variables passed to the kernel (an allowlist).
        typecheck: Pyright mode for drafts.
    """

    root: Path
    name: str
    package: str
    github: GithubConfig | None = None
    timeouts: Timeouts = Timeouts()
    env: tuple[str, ...] = ()
    typecheck: TypecheckMode = "strict"

    @property
    def stable_dir(self) -> Path:
        return self.root / self.package

    @property
    def draft_dir(self) -> Path:
        return self.root / f"{self.package}_draft"

    @property
    def pipelines_dir(self) -> Path:
        return self.root / "pipelines"

    @property
    def examples_dir(self) -> Path:
        return self.root / "examples"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"


def load_config(root: str | Path) -> Config:
    """Load and validate ``<root>/ico.toml`` and check the package layout.

    Raises:
        ConfigError: With the offending field, e.g.
            ``ico.toml: timeouts.run: expected a positive number, got 'x'``.
    """
    root = Path(root).resolve()
    file = root / CONFIG_FILE
    try:
        with file.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{file} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE}: invalid TOML: {e}") from None

    table = _Table(data, "")
    name = table.string("name")
    package = table.string("package")
    if not package.isidentifier():
        raise _error("package", "a Python identifier", package)
    github = _github(table.table("github")) if "github" in data else None
    timeouts = _timeouts(table.table("timeouts")) if "timeouts" in data else Timeouts()
    env = _env(table.get("env", []))
    typecheck = table.choice("typecheck", get_args(TypecheckMode), "strict")
    table.reject_unknown()

    config = Config(
        root=root,
        name=name,
        package=package,
        github=github,
        timeouts=timeouts,
        env=env,
        typecheck=cast(TypecheckMode, typecheck),
    )
    for directory in (config.stable_dir, config.draft_dir):
        if not (directory / "__init__.py").is_file():
            raise ConfigError(
                f"{CONFIG_FILE}: package: {directory} is not a package "
                "(no __init__.py); rename the package directories or set "
                "'package'"
            )
    return config


def _github(table: _Table) -> GithubConfig:
    repo = table.string("repo")
    if not _GITHUB_REPO.fullmatch(repo):
        raise _error(table.key("repo"), "'owner/name'", repo)
    base = table.string("base", "main")
    table.reject_unknown()
    return GithubConfig(repo=repo, base=base)


def _timeouts(table: _Table) -> Timeouts:
    defaults = Timeouts()
    timeouts = Timeouts(
        run=table.positive("run", defaults.run),
        validate=table.positive("validate", defaults.validate),
        load=table.positive("load", defaults.load),
        wait=table.positive("wait", defaults.wait),
    )
    table.reject_unknown()
    return timeouts


def _env(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error("env", "a list of variable names", value)
    names: list[str] = []
    for i, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or not _ENV_NAME.fullmatch(item):
            raise _error(f"env[{i}]", "an environment variable name", item)
        names.append(item)
    return tuple(names)


class _Table:
    """A TOML table read field by field; unread fields are unknown keys."""

    def __init__(self, data: dict[str, object], prefix: str) -> None:
        self.data = data
        self.prefix = prefix
        self.read: set[str] = set()

    def key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str, default: object) -> object:
        self.read.add(key)
        return self.data.get(key, default)

    def string(self, key: str, default: str | None = None) -> str:
        self.read.add(key)
        if key not in self.data:
            if default is None:
                raise ConfigError(f"{CONFIG_FILE}: {self.key(key)}: required")
            return default
        value = self.data[key]
        if not isinstance(value, str) or not value:
            raise _error(self.key(key), "a non-empty string", value)
        return value

    def positive(self, key: str, default: float) -> float:
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise _error(self.key(key), "a positive number", value)
        return float(value)

    def choice(self, key: str, choices: tuple[str, ...], default: str) -> str:
        value = self.get(key, default)
        if value not in choices:
            raise _error(self.key(key), f"one of {list(choices)}", value)
        return cast(str, value)

    def table(self, key: str) -> _Table:
        value = self.get(key, None)
        if not isinstance(value, dict):
            raise _error(self.key(key), "a table", value)
        return _Table(cast(dict[str, object], value), f"{self.key(key)}.")

    def reject_unknown(self) -> None:
        unknown = sorted(set(self.data) - self.read)
        if unknown:
            keys = ", ".join(self.key(k) for k in unknown)
            raise ConfigError(f"{CONFIG_FILE}: unknown keys: {keys}")


def _error(key: str, expected: str, got: object) -> ConfigError:
    return ConfigError(f"{CONFIG_FILE}: {key}: expected {expected}, got {got!r}")
