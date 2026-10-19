"""What operators see in the kernel process: Python, PATH, packages, binaries.

Computed in the kernel, which runs the operators with its own environment
(an allowlist of variables, see ``ico.toml`` ``env``).

Binaries: an operator that runs an external program declares it as a
capability ``bin:<name>`` (``caps={"bin:ffmpeg"}``); :func:`bins` resolves
such names on the kernel's ``PATH``.

Duplicates: several distributions may install the same top-level package
(``cv2`` from ``opencv-python``, ``opencv-python-headless`` and
``opencv-contrib-python``); the last installed silently wins, so they are
reported.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from importlib import metadata

from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject, JsonValue

BIN_PREFIX = "bin:"


def bin_names(caps: Iterable[str]) -> list[str]:
    """The binaries named by ``bin:<name>`` capabilities, sorted."""
    return sorted(
        {c[len(BIN_PREFIX) :] for c in caps if c.startswith(BIN_PREFIX)} - {""}
    )


def bins(names: Iterable[str]) -> dict[str, str | None]:
    """Where each binary resolves on the current ``PATH`` (``None``: missing)."""
    return {name: shutil.which(name) for name in sorted(set(names))}


def duplicates(
    owners: Mapping[str, Collection[str]],
    versions: Mapping[str, str],
    ships_init: Callable[[str, str], bool],
) -> list[JsonValue]:
    """Top-level packages installed by more than one distribution.

    A namespace package (``google``) is legitimately shared, so a package
    counts only when at least two distributions ship its ``__init__``.

    Args:
        owners: Top-level package → distribution names
            (``importlib.metadata.packages_distributions()``).
        versions: Distribution name → version.
        ships_init: Whether a distribution ships the package's ``__init__``
            (or the module itself).
    """
    out: list[JsonValue] = []
    for package in sorted(owners):
        distributions = sorted(set(owners[package]), key=str.lower)
        distributions = [d for d in distributions if ships_init(d, package)]
        if len(distributions) > 1:
            listed: list[JsonValue] = []
            listed.extend(f"{d}=={versions.get(d, '?')}" for d in distributions)
            out.append({"package": package, "distributions": listed})
    return out


def report(
    config: Config, needed_bins: Iterable[str], match: Sequence[str] | None
) -> JsonObject:
    """The kernel's environment as reported by the ``environment`` tool.

    Variable values are never reported (they may be secrets), only whether
    the allowed ones are set.
    """
    versions: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            versions.setdefault(name, dist.version)
    packages: list[JsonValue] = []
    packages.extend(
        f"{name}=={version}"
        for name, version in sorted(versions.items(), key=lambda kv: kv[0].lower())
        if match is None or any(part.lower() in name.lower() for part in match)
    )
    env: list[JsonValue] = []
    env.extend({"name": name, "set": name in os.environ} for name in config.env)
    path: list[JsonValue] = []
    path.extend(p for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    resolved: JsonObject = {}
    resolved.update(bins(needed_bins))
    timeouts = config.timeouts
    return {
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "path": path,
        "env": env,
        "timeouts": {
            "run": timeouts.run,
            "validate": timeouts.validate,
            "load": timeouts.load,
        },
        "packages": packages,
        "duplicates": duplicates(
            metadata.packages_distributions(), versions, _ships_init
        ),
        "bins": resolved,
    }


def _ships_init(distribution: str, package: str) -> bool:
    try:
        files = metadata.distribution(distribution).files or []
    except metadata.PackageNotFoundError:
        return False
    for file in files:
        parts = file.parts
        if len(parts) == 2 and parts[0] == package and parts[1].startswith("__init__."):
            return True
        if len(parts) == 1 and parts[0].split(".")[0] == package:
            return True  # a single-module distribution or an extension module
    return False
