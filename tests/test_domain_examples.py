"""The examples of stable operators are their tests.

``examples/<name>@<version>.json`` holds ``[{"input", "output"}, ...]``,
saved by ``propose_operator`` and committed by ``crystallize``. Running the
operator on each input must give exactly that output. The ``crystallize``
gate runs this file in the pull request branch.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from ico.registry import default_registry, load_operators
from ico.spec import NodeSpec, run

from ico_mcp.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def stable_examples() -> list[tuple[str, Any, Any]]:
    config = load_config(ROOT)
    load_operators(config.stable_dir, status="stable")
    stable = {e.ref for e in default_registry if e.meta.status == "stable"}
    cases: list[tuple[str, Any, Any]] = []
    for path in sorted(config.examples_dir.glob("*@*.json")):
        ref = path.name.removesuffix(".json")
        if ref in stable:
            examples = cast(list[dict[str, Any]], json.loads(path.read_text()))
            cases.extend((ref, e["input"], e["output"]) for e in examples)
    return cases


@pytest.mark.parametrize(("ref", "input", "output"), stable_examples())
def test_example(ref: str, input: Any, output: Any) -> None:
    result = run(NodeSpec(kind="op", op=ref), input)
    assert list(result.outputs) == [output]
