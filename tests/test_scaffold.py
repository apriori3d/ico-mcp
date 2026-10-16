"""The project is wired correctly: ico comes from the sibling checkout."""

from pathlib import Path

import ico
import mcp

import ico_mcp


def test_package_imports() -> None:
    assert ico_mcp.__version__ == "0.1.0"
    assert mcp.__name__ == "mcp"


def test_ico_is_the_sibling_checkout() -> None:
    sibling = (Path(__file__).parents[2] / "ico" / "src" / "ico").resolve()
    assert Path(ico.__file__).parent.resolve() == sibling
