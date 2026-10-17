"""Pipeline parameters: ``{"$param": ...}`` references, values per run."""

import json
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, KernelError
from ico_mcp.server import create_server

pytestmark = pytest.mark.anyio


def param(name: str, **declaration: Any) -> dict[str, Any]:
    return {"$param": name, **declaration}


def repeat(times: Any, sep: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {"times": times}
    if sep is not None:
        params["sep"] = sep
    return {"kind": "op", "op": "repeat@1", "params": params}


SPEC: dict[str, Any] = {
    "version": 1,
    "root": {
        "kind": "seq",
        "name": "echo",
        "children": [
            {"kind": "op", "op": "shout@1"},
            repeat(param("times"), param("sep", default="-")),
        ],
    },
}


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def call_error(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    message = text(result)
    return cast(dict[str, Any], json.loads(message[message.index("{") :]))


# ─── Kernel ───


async def test_kernel_run_substitutes_values_and_defaults(config: Config) -> None:
    plans: list[JsonObject] = []

    async def collect(message: JsonObject) -> None:
        if message.get("method") == "plan":
            plans.append(message)

    async with KernelClient(config) as kernel:
        result = await kernel.request(
            "run",
            {"spec": SPEC, "input": "ab", "params": {"times": 3}},
            on_notification=collect,
        )
        missing = None
        try:
            await kernel.request("run", {"spec": SPEC, "input": "ab"})
        except KernelError as e:
            missing = e

    assert cast(dict[str, Any], result)["outputs"] == ["AB-AB-AB"]
    assert missing is not None and missing.code == "spec_error"
    assert "missing value for parameter(s) times" in missing.message
    rows = cast(dict[str, Any], plans[0]["params"])["plan"]["rows"]
    flow = ["".join(s["text"] for s in row[0]) for row in rows]
    assert any("Repeat" in line for line in flow)


async def test_kernel_validate_declares_params(config: Config) -> None:
    async with KernelClient(config) as kernel:
        missing = await kernel.request("validate", {"spec": SPEC})
        given = await kernel.request("validate", {"spec": SPEC, "params": {"times": 2}})
        bad = await kernel.request(
            "validate", {"spec": repeat({"$param": "t", "extra": 1})}
        )

    declared = cast(dict[str, Any], missing)["params"]
    assert declared == [
        {"name": "times", "required": True, "type": "int", "paths": ["1"]},
        {
            "name": "sep",
            "required": False,
            "type": "str",
            "default": "-",
            "paths": ["1"],
        },
    ]
    [issue] = cast(dict[str, Any], missing)["issues"]
    assert issue["severity"] == "error"
    assert "missing value for parameter(s) times" in issue["message"]
    assert cast(dict[str, Any], given)["issues"] == []
    assert cast(dict[str, Any], given)["params"] == declared
    [malformed] = cast(dict[str, Any], bad)["issues"]
    assert "parameter reference" in malformed["message"]
    assert cast(dict[str, Any], bad)["params"] is None


async def test_kernel_rejects_params_that_are_not_an_object(config: Config) -> None:
    async with KernelClient(config) as kernel:
        with pytest.raises(KernelError) as info:
            await kernel.request("run", {"spec": SPEC, "params": [1]})

    assert info.value.code == "bad_request"


# ─── Server ───


async def test_run_a_saved_pipeline_by_name(config: Config) -> None:
    async with Client(create_server(config)) as client:
        rejected = await call_error(
            client, "save_pipeline", name="echo", spec=SPEC, description="Echo."
        )
        saved = await call(
            client,
            "save_pipeline",
            name="echo",
            spec=SPEC,
            description="Echo.",
            params={"times": 1},
        )
        by_name = await call(
            client, "run", pipeline="echo", input="x", params={"times": 2, "sep": "+"}
        )
        by_spec = await call(
            client, "run", spec=SPEC, input="x", params={"times": 2, "sep": "+"}
        )
        status = await call(client, "run_status", run_id=by_name["run_id"])
        listed = await call(client, "list_pipelines")
        loaded = await call(client, "get_pipeline", name="echo")

    assert rejected["code"] == "invalid_pipeline"
    assert saved["params"][0]["name"] == "times"
    assert by_name["outputs"] == by_spec["outputs"] == ["X+X"]
    assert status["title"] == "echo · times=2, sep=+"
    assert listed["pipelines"][0]["params"] == [
        {"name": "times", "required": True},
        {"name": "sep", "required": False, "default": "-"},
    ]
    assert loaded["check_params"] == {"times": 1}
    assert loaded["spec"]["root"]["children"][1]["params"]["times"] == param("times")
    stored = json.loads((config.pipelines_dir / "echo.spec.json").read_text())
    assert stored["check_params"] == {"times": 1}
    traces = [
        json.loads(line)
        for path in sorted(config.runs_dir.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert [t["pipeline"] for t in traces] == ["echo", None]


async def test_run_needs_exactly_one_of_spec_and_pipeline(config: Config) -> None:
    async with Client(create_server(config)) as client:
        both = await call_error(client, "run", spec=SPEC, pipeline="echo")
        neither = await call_error(client, "run", input="x")
        await call(
            client, "save_pipeline", name="other", spec=SPEC, description="",
            params={"times": 1},
        )  # fmt: skip
        unknown = await call_error(client, "run", pipeline="nope", input="x")

    assert both["code"] == neither["code"] == "bad_request"
    assert unknown["code"] == "not_found"
    assert unknown["saved"] == ["other"]


async def test_string_input_has_a_schema_and_arrives_unquoted(config: Config) -> None:
    async with Client(create_server(config)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await call(
            client, "run", spec={"kind": "op", "op": "shout@1"}, input="/data/a.mp4"
        )

    schema = tools["run"].input_schema["properties"]["input"]
    assert {"type": "string"} in schema["anyOf"] and {"type": "null"} in schema["anyOf"]
    assert result["outputs"] == ["/DATA/A.MP4"]


async def test_titles_and_traces(config: Config) -> None:
    unnamed = {"kind": "op", "op": "shout@1"}
    async with Client(create_server(config)) as client:
        first = await call(client, "run", spec=SPEC, input="a", params={"times": 1})
        second = await call(client, "run", spec=unnamed, input="a")
        titles = [
            (await call(client, "run_status", run_id=r["run_id"]))["title"]
            for r in (first, second)
        ]

    assert titles == ["echo · times=1", "shout@1"]
    lines = [
        json.loads(line)
        for path in sorted(config.runs_dir.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert [(t["pipeline"], t["params"]) for t in lines] == [
        (None, {"times": 1}),
        (None, None),
    ]
