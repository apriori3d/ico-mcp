"""The code a remote run carries (server side, files only: no imports of
domain code).

A remote run's job carries the sources of the operator modules its spec
uses, with the closure of their imports inside the domain: relative
imports within a package, and absolute imports of the stable package from
drafts. Both packages go by value, so the image is only the environment.
``assets.toml`` and ``assets.lock`` go along.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence

from ico_mcp import drafts
from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject, JsonValue

ASSET_FILES = ("assets.toml", "assets.lock")


def job_code(
    config: Config, modules: Sequence[str] | None
) -> tuple[JsonObject, list[JsonValue]]:
    """``(sources, packages)`` of a job for ``modules`` (qualified names,
    ``domain_draft.slug``; ``None``: every module of both packages)."""
    stable, draft = config.stable_dir, config.draft_dir

    def of(directory_name: str) -> list[str]:
        if modules is None:
            return drafts.module_names(config.root / directory_name)
        prefix = f"{directory_name}."
        return [m.removeprefix(prefix) for m in modules if m.startswith(prefix)]

    draft_modules = drafts.closure(draft, of(draft.name))
    wanted_stable = set(of(stable.name))
    for module in draft_modules:
        wanted_stable |= absolute_imports(
            (draft / f"{module}.py").read_text(), stable.name
        )
    stable_modules = drafts.closure(stable, sorted(wanted_stable))

    sources: JsonObject = {}
    packages: list[JsonValue] = []
    for directory, chosen, status in (
        (stable, stable_modules, "stable"),
        (draft, draft_modules, "draft"),
    ):
        if not chosen:
            continue
        init = directory / "__init__.py"
        sources[f"{directory.name}/__init__.py"] = (
            init.read_text() if init.is_file() else ""
        )
        for module in chosen:
            sources[f"{directory.name}/{module}.py"] = (
                directory / f"{module}.py"
            ).read_text()
        listed: list[JsonValue] = []
        listed.extend(chosen)
        packages.append({"path": directory.name, "status": status, "modules": listed})
    for name in ASSET_FILES:
        path = config.root / name
        if path.is_file():
            sources[name] = path.read_text()
    return sources, packages


def absolute_imports(code: str, package: str) -> set[str]:
    """Top-level modules of ``package`` that ``code`` imports absolutely
    (``from domain.frames import Frame``, ``import domain.frames``)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    found: set[str] = set()
    prefix = f"{package}."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith(prefix):
                found.add(node.module.removeprefix(prefix).partition(".")[0])
            elif node.module == package:
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    found.add(alias.name.removeprefix(prefix).partition(".")[0])
    return found
