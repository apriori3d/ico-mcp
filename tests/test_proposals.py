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


async def rejected(
    client: Client, tool_name: str = "propose_operator", **arguments: Any
) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments)
    assert result.is_error
    message = text(result)
    error = cast(dict[str, Any], json.loads(message[message.index("{") :]))
    assert error["code"] == "proposal_failed"
    return error


async def tool(client: Client, tool_name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments)
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


# ─── Types ───

FRAMES = '''from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    """A video frame on disk."""

    path: str
    index: int
'''

FRAME_OPS = '''from ico.registry import register

from .frames import Frame


@register("first_frame", 1)
def first_frame(path: str) -> Frame:
    """The first frame of a video."""
    return Frame(path, 0)
'''


async def test_proposed_types_are_used_by_a_draft_operator(config: Config) -> None:
    async with Client(create_server(config)) as client:
        accepted = await tool(client, "propose_types", module="frames", code=FRAMES)
        operator = await propose(client, module="video", code=FRAME_OPS)
        listed = await tool(client, "list_types")
        frame = await tool(client, "get_type", name="Frame")
        found = await tool(client, "search_operators", output_type="Frame")

    assert accepted["module"] == "domain_draft.frames"
    assert accepted["path"] == "domain_draft/frames.py"
    assert [t["name"] for t in accepted["types"]] == ["Frame"]
    assert accepted["types"][0]["fields"] == [
        {"name": "path", "type": "str"},
        {"name": "index", "type": "int"},
    ]
    assert operator["cards"][0]["sig"] == "str → Frame"
    assert "domain_draft.frames:Frame" in [t["type"] for t in listed["types"]]
    assert (frame["status"], frame["doc"]) == ("draft", "A video frame on disk.")
    assert [c["name"] for c in found["cards"]] == ["first_frame"]


async def test_types_module_needs_a_class_and_no_operators(config: Config) -> None:
    async with Client(create_server(config)) as client:
        empty = await rejected(
            client, "propose_types", module="frames", code="LIMIT = 3\n"
        )
        mixed = await rejected(
            client, "propose_types", module="slug", code=SLUGIFY + FRAMES
        )

    assert empty["stage"] == "types"
    assert "no public class" in empty["message"]
    assert mixed["stage"] == "types"
    assert mixed["diagnostics"] == [{"ref": "slugify@1"}]


async def test_write_rejects_absolute_draft_imports(config: Config) -> None:
    code = (
        "import os\n"
        "from domain_draft.frames import Frame, Video as V\n"
        "from domain_draft import frames\n"
        "import domain_draft.frames as f, domain_draft.a.b\n"
        "from domain.text import Report\n"
    )
    async with Client(create_server(config)) as client:
        operator = await rejected(client, module="video", code=code)
        types = await rejected(client, "propose_types", module="video", code=code)

    assert operator["stage"] == types["stage"] == "write"
    assert [(d["line"], d["fix"]) for d in operator["diagnostics"]] == [
        (2, "from .frames import Frame, Video as V"),
        (3, "from . import frames"),
        (4, "from . import frames as f"),
        (4, "from .a import b"),
    ]
    assert types["diagnostics"] == operator["diagnostics"]
    assert not (config.draft_dir / "video.py").exists()


# ─── Edits ───


async def test_edits_change_a_draft_module(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await propose(client, module="slug", code=SLUGIFY)
        card = await tool(client, "get_operator", ref="slugify@1")
        edited = await propose(
            client,
            module="slug",
            edits=[
                {"old": '"-".join', "new": '"_".join'},
                {"old": "Lower-case words", "new": "Lower-case words, underscores"},
            ],
        )
        ran = await tool(
            client, "run", spec={"kind": "op", "op": "slugify@1"}, input="A B"
        )
        stable = await tool(client, "get_operator", ref="count_words@1")

    assert card["module_source"] == SLUGIFY
    assert edited["edits"] == 2
    assert ran["outputs"] == ["a_b"]
    text = (config.draft_dir / "slug.py").read_text()
    assert '"_".join' in text and "underscores" in text
    assert "module_source" not in stable


async def test_an_edit_can_delete_text(config: Config) -> None:
    code = SLUGIFY + "\n\nUNUSED = 1\n"
    async with Client(create_server(config)) as client:
        await propose(client, module="slug", code=code)
        await propose(
            client, module="slug", edits=[{"old": "\n\nUNUSED = 1\n", "new": ""}]
        )

    assert (config.draft_dir / "slug.py").read_text() == SLUGIFY


async def test_failed_edits_change_nothing(config: Config) -> None:
    doubled = SLUGIFY + '\n# "-".join twice\n'
    async with Client(create_server(config)) as client:
        await propose(client, module="slug", code=doubled)
        missing = await rejected(
            client,
            module="slug",
            edits=[
                {"old": "Lower-case", "new": "Upper"},
                {"old": "nowhere", "new": "x"},
            ],
        )
        twice = await rejected(
            client, module="slug", edits=[{"old": '"-".join', "new": "x"}]
        )
        empty = await rejected(client, module="slug", edits=[{"old": "", "new": "x"}])

    assert (missing["stage"], missing["diagnostics"][0]["edit"]) == ("write", 1)
    assert "not found" in missing["message"]
    assert twice["diagnostics"][0]["found"] == 2
    assert "add surrounding lines" in twice["message"]
    assert "empty old text" in empty["message"]
    assert (config.draft_dir / "slug.py").read_text() == doubled


async def test_edits_need_a_draft_and_exactly_one_source(config: Config) -> None:
    async with Client(create_server(config)) as client:
        no_draft = await rejected(
            client, module="nothing", edits=[{"old": "a", "new": "b"}]
        )
        both = await rejected(
            client, module="slug", code=SLUGIFY, edits=[{"old": "a", "new": "b"}]
        )
        neither = await rejected(client, module="slug")
        stable = await rejected(client, module="text", edits=[{"old": "a", "new": "b"}])

    assert "no draft module 'nothing'" in no_draft["message"]
    assert "exactly one of code and edits" in both["message"]
    assert "exactly one of code and edits" in neither["message"]
    assert "stable module" in stable["message"]


async def test_edits_of_a_types_module(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await tool(client, "propose_types", module="frames", code=FRAMES)
        edited = await tool(
            client,
            "propose_types",
            module="frames",
            edits=[
                {"old": "    index: int\n", "new": "    index: int\n    time: float\n"}
            ],
        )

    assert edited["edits"] == 1
    assert [f["name"] for f in edited["types"][0]["fields"]] == [
        "path",
        "index",
        "time",
    ]
