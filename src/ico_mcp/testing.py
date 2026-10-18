"""The examples of stable operators as tests, for domain repositories.

``examples/<name>@<version>.json`` holds ``[{"input", "output"}, ...]``,
saved by ``propose_operator`` and committed by ``crystallize``. Running the
operator on each input must give exactly that output. A domain's
``tests/test_domain_examples.py`` is one line::

    test_example = example_tests(Path(__file__).resolve().parents[1])

and the ``crystallize`` gate runs it in the pull request branch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from ico.registry import default_registry, load_operators
from ico.spec import NodeSpec, run

from ico_mcp.config import load_config


def stable_examples(root: Path) -> list[tuple[str, Any, Any]]:
    """``(ref, input, output)`` of every example of a stable operator."""
    config = load_config(root)
    load_operators(config.stable_dir, status="stable")
    stable = {e.ref for e in default_registry if e.meta.status == "stable"}
    cases: list[tuple[str, Any, Any]] = []
    for path in sorted(config.examples_dir.glob("*@*.json")):
        ref = path.name.removesuffix(".json")
        if ref in stable:
            examples = cast(list[dict[str, Any]], json.loads(path.read_text()))
            cases.extend((ref, e["input"], e["output"]) for e in examples)
    return cases


def example_tests(root: Path) -> Callable[[str, Any, Any], None]:
    """A test, parametrized by the examples of the domain at ``root``."""

    @pytest.mark.parametrize(("ref", "input", "output"), stable_examples(root))
    def test_example(ref: str, input: Any, output: Any) -> None:
        result = run(NodeSpec(kind="op", op=ref), input)
        assert list(result.outputs) == [output]

    return test_example
