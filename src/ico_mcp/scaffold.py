"""``ico-mcp init``: create an empty domain repository.

A domain repository holds what the agent builds (operators, types,
pipelines, examples) and what the domain depends on; the server comes from
the ``ico-mcp`` package installed in the domain's environment, so server
fixes reach a domain by a version bump.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from string import Template

import ico

import ico_mcp
from ico_mcp.config import CONFIG_FILE

ICO_TOML = """\
# ico-mcp domain configuration; see the ico-mcp README.

name = "$name"
package = "$package"             # stable package; drafts in ${package}_draft
# libraries = []               # installed packages whose operators the domain uses
# about = "What this domain does, in a few lines (shown to agents)."
# github = { repo = "owner/$name", base = "main" }  # needed by crystallize
timeouts = { run = 600, validate = 30, load = 60, wait = 45 }
env = []                       # variables passed to the kernel, e.g. ["HF_HOME"]
typecheck = "strict"           # pyright mode for drafts
"""

PYPROJECT = """\
[project]
name = "$name"
version = "0.1.0"
requires-python = ">=3.11,<3.13"
# The server and the ICO framework, then what the operators need.
dependencies = ["ico-mcp", "apriori-ico"]

[dependency-groups]
dev = ["pytest"]                # the crystallize gate runs the examples

[tool.uv]
package = false                 # the domain packages load from this directory
$sources"""

GITIGNORE = """\
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.DS_Store

# Run traces
runs/

# Machine-specific MCP client config
.mcp.json
"""

EXAMPLES_TEST = """\
\"\"\"The examples of stable operators are their tests (run by the
crystallize gate).\"\"\"

from pathlib import Path

from ico_mcp.testing import example_tests

test_example = example_tests(Path(__file__).resolve().parents[1])
"""

DOMAIN_NOTES = """\
## 8. Domain notes

What agents need to know about this domain beyond each pipeline's
description: the workflow (which pipeline comes after which), how to choose
between alternatives, data locations and formats, naming rules, hardware.
"""

MCP_JSON = """\
{
  "mcpServers": {
    "$name": {
      "command": "uv",
      "args": ["run", "--project", "$root", "ico-mcp", "--root", "$root"]
    }
  }
}
"""


def init(
    directory: Path, *, name: str | None = None, package: str = "domain"
) -> list[Path]:
    """Create an empty domain repository in ``directory``; existing files
    are kept. Returns the files written.

    Raises:
        ValueError: ``package`` is not a Python identifier.
    """
    if not package.isidentifier():
        raise ValueError(f"package must be a Python identifier, got {package!r}")
    root = directory.resolve()
    values = {
        "name": name or root.name,
        "package": package,
        "root": str(root),
        "sources": _sources(root),
    }
    files = {
        CONFIG_FILE: ICO_TOML,
        "pyproject.toml": PYPROJECT,
        ".gitignore": GITIGNORE,
        ".mcp.json": MCP_JSON,
        f"{package}/__init__.py": "",
        f"{package}_draft/__init__.py": "",
        "pipelines/.gitkeep": "",
        "examples/.gitkeep": "",
        "tests/test_domain_examples.py": EXAMPLES_TEST,
        "docs/domain.md": DOMAIN_NOTES,
    }
    written: list[Path] = []
    for relative, template in files.items():
        path = root / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(Template(template).substitute(values), encoding="utf-8")
        written.append(path)
    return written


def _sources(root: Path) -> str:
    """Where ``ico-mcp`` and ``apriori-ico`` (ICO) come from: the checkouts
    this server runs from when it is an editable install; nothing (PyPI)
    otherwise."""
    checkouts = {
        "ico-mcp": _checkout(Path(ico_mcp.__file__), "ico-mcp"),
        "apriori-ico": _checkout(Path(ico.__file__), "apriori-ico"),
    }
    lines: list[str] = []
    for package, checkout in checkouts.items():
        if checkout is None:
            continue
        # Relative between neighbours (../ico-mcp), else absolute.
        near = os.path.commonpath([checkout, root]) != os.sep
        path = os.path.relpath(checkout, root) if near else str(checkout)
        lines.append(f'{package} = {{ path = "{path}", editable = true }}')
    if not lines:
        return ""
    return "\n[tool.uv.sources]\n" + "\n".join(lines) + "\n"


def _checkout(module_file: Path, project: str) -> Path | None:
    """The source checkout of an editable install (``<checkout>/src/<pkg>``)."""
    try:
        distribution(project)
    except PackageNotFoundError:
        return None
    checkout = module_file.resolve().parents[2]
    pyproject = checkout / "pyproject.toml"
    if pyproject.is_file() and f'name = "{project}"' in pyproject.read_text():
        return checkout
    return None
