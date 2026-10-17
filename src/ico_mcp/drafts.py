"""Draft modules and their relative imports, around crystallization.

Draft modules import each other relatively (``from .frames import Frame``)
and the stable package absolutely, so crystallization moves files without
rewriting them: a module moves together with the draft modules it imports
relatively (:func:`closure`).

Moved drafts stay in the draft package until the pull request is merged and
pulled, so the agent keeps using them. Then a stable module of the same name
exists and :func:`retire` removes the draft (if it is identical) and
rewrites the imports of the remaining drafts to the stable module.
"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject, JsonValue


def module_names(directory: Path) -> list[str]:
    """Top-level modules of a package directory (``__init__`` excluded)."""
    return sorted(p.stem for p in directory.glob("*.py") if p.stem != "__init__")


def relative_imports(code: str) -> set[str]:
    """Sibling modules a module imports relatively (``from .x import``,
    ``from . import x``); code that does not parse imports nothing."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.partition(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return found


def draft_imports(code: str, package: str) -> list[JsonObject]:
    """Absolute imports of the draft ``package``, with the relative form.

    Code that does not parse is left to the typecheck and load stages.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    def inner(name: str | None) -> str | None:
        """``name`` inside ``package`` ("" for the package), else ``None``."""
        if name == package:
            return ""
        if name is not None and name.startswith(f"{package}."):
            return name.removeprefix(f"{package}.")
        return None

    found: list[JsonObject] = []
    for node in ast.walk(tree):
        fixes: list[str | None] = []
        line = 0
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            line = node.lineno
            rest = inner(node.module)
            if rest is not None:
                relative = ast.ImportFrom(
                    module=rest or None, names=node.names, level=1
                )
                fixes.append(ast.unparse(relative))
        elif isinstance(node, ast.Import):
            line = node.lineno
            for alias in node.names:
                rest = inner(alias.name)
                if rest is None:
                    continue
                parent, _, leaf = rest.rpartition(".")
                relative = ast.ImportFrom(
                    module=parent or None,
                    names=[ast.alias(leaf, alias.asname)],
                    level=1,
                )
                fixes.append(ast.unparse(relative) if leaf else None)
        for fix in fixes:
            found.append(
                {
                    "line": line,
                    "message": f"absolute import of the draft package {package!r}",
                    "fix": fix,
                }
            )
    found.sort(key=lambda d: cast(int, d["line"]))
    return found


def closure(directory: Path, modules: Iterable[str]) -> list[str]:
    """``modules`` and the modules of ``directory`` they import relatively,
    transitively; sorted."""
    known = set(module_names(directory))
    todo = [m for m in modules if m in known]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        code = (directory / f"{name}.py").read_text()
        todo.extend(m for m in relative_imports(code) if m in known)
    return sorted(seen)


def retire(config: Config) -> JsonObject:
    """Retire drafts that have been crystallized (a stable module of the same
    name exists).

    An identical draft is removed and the remaining drafts import the stable
    module instead. A draft that differs from the stable module is kept on
    disk and reported in ``conflicts``: it fails to load because its
    operators are registered by the stable module.

    Returns:
        ``{"retired": [...], "rewritten": [...], "conflicts": [...]}``.
    """
    stable = set(module_names(config.stable_dir))
    retired: list[str] = []
    conflicts: list[JsonValue] = []
    for name in module_names(config.draft_dir):
        if name not in stable:
            continue
        draft = config.draft_dir / f"{name}.py"
        if draft.read_bytes() == (config.stable_dir / f"{name}.py").read_bytes():
            draft.unlink()
            Path(importlib.util.cache_from_source(str(draft))).unlink(missing_ok=True)
            retired.append(name)
        else:
            conflicts.append(
                {
                    "module": name,
                    "message": f"{config.draft_dir.name}.{name} was crystallized into "
                    f"{config.package}.{name} but differs from it; save the "
                    "changes as a new module or a new operator version, then "
                    "delete the draft",
                }
            )
    rewritten: list[str] = []
    if retired:
        for name in module_names(config.draft_dir):
            path = config.draft_dir / f"{name}.py"
            code = path.read_text()
            new = _absolute_imports(code, set(retired), config.package)
            if new != code:
                path.write_text(new)
                rewritten.append(name)
    out: JsonObject = {}
    out["retired"] = list[JsonValue](retired)
    out["rewritten"] = list[JsonValue](rewritten)
    out["conflicts"] = conflicts
    return out


def _absolute_imports(code: str, modules: set[str], package: str) -> str:
    """Rewrite relative imports of ``modules`` to ``from <package>.<m>``."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    lines = code.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module and node.module.partition(".")[0] in modules:
            new = ast.ImportFrom(
                module=f"{package}.{node.module}", names=node.names, level=0
            )
        elif not node.module and all(a.name in modules for a in node.names):
            new = ast.ImportFrom(module=package, names=node.names, level=0)
        else:
            continue
        end = node.end_lineno or node.lineno
        indent = lines[node.lineno - 1][: node.col_offset]
        edits.append((node.lineno, end, f"{indent}{ast.unparse(new)}\n"))
    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1 : end] = [text]
    return "".join(lines)
