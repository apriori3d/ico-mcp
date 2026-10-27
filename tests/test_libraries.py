"""Libraries: installed packages whose operators the domain uses."""

import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from ico_mcp.config import Config
from ico_mcp.kernel_client import KernelClient

MakeRepo = Callable[..., Config]

pytestmark = pytest.mark.anyio

SHAPES = '''
from dataclasses import dataclass

from ico.registry import register


@dataclass(frozen=True)
class Square:
    """A square by its side."""

    side: float


@register("geom.square", 1)
def square(side: float) -> Square:
    """A square with this side."""
    return Square(side)


@register("geom.area", 1)
def area(shape: Square) -> float:
    """The area of a square."""
    return shape.side**2
'''

EXTRA = '''
from ico.registry import register


@register("geom.double", 1)
def double(x: float) -> float:
    """Twice a number."""
    return 2 * x
'''

# A draft that imports the library: its operators are still stable.
USES_GEOM = '''
from geom.shapes import Square

from ico.registry import register


@register("half_side", 1)
def half_side(shape: Square) -> Square:
    """A square with half the side."""
    return Square(shape.side / 2)
'''


@pytest.fixture
def library(
    tmp_path: Path, make_repo: MakeRepo, monkeypatch: pytest.MonkeyPatch
) -> Config:
    """A repository with the library ``geom`` installed on ``PYTHONPATH``."""
    site = tmp_path / "site"
    (site / "geom").mkdir(parents=True)
    (site / "geom" / "__init__.py").write_text("")
    (site / "geom" / "shapes.py").write_text(textwrap.dedent(SHAPES))
    (site / "geom" / "extra.py").write_text(textwrap.dedent(EXTRA))
    monkeypatch.setenv("PYTHONPATH", str(site))
    config = make_repo(
        tmp_path / "repo", 'libraries = ["geom"]\nenv = ["PYTHONPATH"]\n'
    )
    (config.draft_dir / "uses_geom.py").write_text(textwrap.dedent(USES_GEOM))
    return config


async def request(
    kernel: KernelClient, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    return cast(dict[str, Any], await kernel.request(method, params))


def statuses(result: dict[str, Any]) -> dict[str, str]:
    return {card["name"]: card["status"] for card in result["cards"]}


async def test_library_operators_load_as_stable(library: Config) -> None:
    async with KernelClient(library) as kernel:
        report = cast(dict[str, Any], await kernel.start())
        found = await request(kernel, "search", {"text": "square"})

    assert report["libraries"]["geom"]["loaded"] == [
        "geom.double@1",
        "geom.area@1",
        "geom.square@1",
    ]
    assert report["libraries"]["geom"]["errors"] == []
    assert "half_side@1" in report["draft"]["loaded"]
    # Loaded before the draft that imports it: not registered as a draft.
    assert statuses(found) == {
        "geom.area": "stable",
        "geom.square": "stable",
        "half_side": "draft",
    }


async def test_library_types_are_listed(library: Config) -> None:
    async with KernelClient(library) as kernel:
        types = await request(kernel, "types", {})

    cards = {card["name"]: card for card in types["types"]}
    assert cards["Square"]["status"] == "stable"


async def test_a_draft_runs_with_library_operators(library: Config) -> None:
    spec = {
        "kind": "seq",
        "children": [
            {"kind": "op", "op": "geom.square@1"},
            {"kind": "op", "op": "half_side@1"},
            {"kind": "op", "op": "geom.area@1"},
        ],
    }
    async with KernelClient(library) as kernel:
        result = await request(kernel, "run", {"spec": spec, "input": 4.0})

    assert result["outputs"] == [4.0]


async def test_a_kernel_loads_selected_library_modules(library: Config) -> None:
    kernel = KernelClient(library, modules=["geom.extra"])
    async with kernel:
        report = cast(dict[str, Any], await kernel.start())

    assert report["libraries"]["geom"]["loaded"] == ["geom.double@1"]
    assert report["draft"]["loaded"] == []


async def test_a_missing_library_is_a_load_error(
    tmp_path: Path, make_repo: MakeRepo
) -> None:
    config = make_repo(tmp_path, 'libraries = ["no_such_library_xyz"]\n')
    async with KernelClient(config) as kernel:
        report = cast(dict[str, Any], await kernel.start())
        found = await request(kernel, "search", {"text": "count"})

    (error,) = report["libraries"]["no_such_library_xyz"]["errors"]
    assert error["file"] == "no_such_library_xyz"
    assert "is not installed in the domain's environment" in error["error"]
    # The domain's own packages load all the same.
    assert "count_words@1" in report["stable"]["loaded"]
    assert "count_words" in [card["name"] for card in found["cards"]]
