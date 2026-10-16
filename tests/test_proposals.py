import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config
from ico_mcp.server import create_server

pytestmark = pytest.mark.anyio

MakeRepo = Callable[..., Config]

SLUGIFY = '''from ico.registry import register


@register("slugify", 1, tags={"text"})
def slugify(text: str) -> str:
    """Lower-case words joined by dashes."""
    return "-".join(text.lower().split())
'''


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def propose(client: Client, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool("propose_operator", arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def rejected(client: Client, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool("propose_operator", arguments)
    assert result.is_error
    message = text(result)
    error = cast(dict[str, Any], json.loads(message[message.index("{") :]))
    assert error["code"] == "proposal_failed"
    return error


async def tool(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


# ─── Success ───


async def test_proposed_operator_is_loaded_as_draft(config: Config) -> None:
    examples = [
        {"ref": "slugify@1", "input": "Hello World", "output": "hello-world"},
        {"ref": "slugify@1", "input": " a  b ", "output": "a-b"},
    ]
    async with Client(create_server(config)) as client:
        accepted = await propose(client, module="slug", code=SLUGIFY, examples=examples)
        found = await tool(client, "search_operators", text="slugify")
        ran = await tool(
            client, "run", spec={"kind": "op", "op": "slugify@1"}, input="X Y"
        )

    assert accepted["module"] == "domain_draft.slug"
    assert accepted["path"] == "domain_draft/slug.py"
    assert [c["name"] for c in accepted["cards"]] == ["slugify"]
    assert accepted["examples"] == {"slugify@1": 2}
    assert found["cards"][0]["status"] == "draft"
    assert ran["outputs"] == ["x-y"]
    saved = json.loads((config.examples_dir / "slugify@1.json").read_text())
    assert saved == [{"input": e["input"], "output": e["output"]} for e in examples]


async def test_replacing_a_draft(config: Config) -> None:
    replaced = SLUGIFY.replace('"-".join', '"_".join')
    async with Client(create_server(config)) as client:
        await propose(client, module="slug", code=SLUGIFY)
        await propose(client, module="slug", code=replaced)
        ran = await tool(
            client, "run", spec={"kind": "op", "op": "slugify@1"}, input="a b"
        )

    assert ran["outputs"] == ["a_b"]


async def test_warnings_do_not_fail(tmp_path: Path, make_repo: MakeRepo) -> None:
    config = make_repo(tmp_path, 'typecheck = "standard"\n')
    code = SLUGIFY + "\n\n1 + 1\n"  # reportUnusedExpression: a warning in standard mode
    async with Client(create_server(config)) as client:
        accepted = await propose(client, module="slug", code=code)

    [warning] = accepted["warnings"]
    assert warning["rule"] == "reportUnusedExpression"
    assert (warning["file"], warning["line"]) == ("domain_draft/slug.py", 10)


# ─── Failing stages ───


async def test_write_rejects_bad_and_stable_names(config: Config) -> None:
    async with Client(create_server(config)) as client:
        bad = await rejected(client, module="my-module", code=SLUGIFY)
        private = await rejected(client, module="_slug", code=SLUGIFY)
        stable = await rejected(client, module="text", code=SLUGIFY)

    assert bad["stage"] == private["stage"] == stable["stage"] == "write"
    assert "stable module" in stable["message"]
    assert not (config.draft_dir / "text.py").exists()


async def test_typecheck_errors_have_locations(config: Config) -> None:
    code = "def broken(x: int) -> str:\n    return x\n"
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="broken", code=code)

    assert error["stage"] == "typecheck"
    [diagnostic] = error["diagnostics"]
    assert diagnostic["rule"] == "reportReturnType"
    assert (diagnostic["file"], diagnostic["line"]) == ("domain_draft/broken.py", 2)
    assert (config.draft_dir / "broken.py").exists()  # left for fixing


async def test_load_errors(config: Config) -> None:
    code = "import does_not_exist  # noqa\n"
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="broken", code=code, typecheck=False)
        still = await tool(client, "search_operators", text="shout")

    assert error["stage"] == "load"
    assert "ModuleNotFoundError" in error["diagnostics"][0]["error"]
    assert still["total"] == 1  # other drafts are still loaded


async def test_register_needs_an_operator(config: Config) -> None:
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="empty", code="X = 1\n")

    assert error["stage"] == "register"
    assert "registers no operator" in error["message"]


async def test_register_rejects_any_signatures(config: Config) -> None:
    code = (
        "from ico.registry import register\n\n\n"
        '@register("ident", 1)\n'
        "def ident(x):\n"
        '    """Return the input."""\n'
        "    return x\n"
    )
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="ident", code=code, typecheck=False)

    assert error["stage"] == "register"
    assert error["diagnostics"] == [{"ref": "ident@1"}]


async def test_failing_examples(config: Config) -> None:
    code = SLUGIFY + (
        '\n\n@register("explode", 1)\n'
        "def explode(text: str) -> str:\n"
        '    """Always fail."""\n'
        '    raise ValueError("no")\n'
    )
    examples = [
        {"ref": "slugify@1", "input": "A B", "output": "a_b"},
        {"ref": "explode@1", "input": "x", "output": "x"},
        {"ref": "slugify@1", "input": "C", "output": "c"},
    ]
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="slug", code=code, examples=examples)

    assert error["stage"] == "examples"
    wrong, failed = error["diagnostics"]
    assert (wrong["ref"], wrong["expected"], wrong["got"]) == (
        "slugify@1",
        "a_b",
        "a-b",
    )
    assert failed["ref"] == "explode@1"
    assert failed["error"]["code"] == "run_error"
    assert not (config.examples_dir / "slugify@1.json").exists()


async def test_examples_must_belong_to_the_module(config: Config) -> None:
    examples = [{"ref": "shout@1", "input": "a", "output": "A"}]
    async with Client(create_server(config)) as client:
        error = await rejected(client, module="slug", code=SLUGIFY, examples=examples)

    assert error["stage"] == "examples"
    assert error["diagnostics"] == [{"ref": "shout@1"}]
