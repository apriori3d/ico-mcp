"""Pipelines that use other pipelines (pipeline_ref) and tap, on the server."""

from typing import Any

import pytest
from mcp.client import Client

from ico_mcp.config import Config
from ico_mcp.server import create_server
from test_server import call, call_error, op

pytestmark = pytest.mark.anyio


def ref(pipeline: str, **bind: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"pipeline": pipeline}
    if bind:
        params["bind"] = bind
    return {"kind": "pipeline_ref", "params": params}


ECHO = {"kind": "op", "op": "repeat@1", "params": {"times": {"$param": "times"}}}
LOUD = {"kind": "seq", "children": [ref("upper"), ref("echo", times={"$param": "n"})]}


async def save(
    client: Client, name: str, spec: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return await call(
        client, "save_pipeline", name=name, spec=spec, description=name, **extra
    )


async def test_a_pipeline_of_pipelines(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await save(client, "upper", op("shout@1"))
        await save(client, "echo", ECHO, params={"times": 2})
        loud = await save(client, "loud", LOUD, params={"n": 3})
        lifted = await save(client, "echoed", ref("echo"), params={"times": 2})
        ran = await call(client, "run", pipeline="loud", input="hi", params={"n": 2})
        listed = await call(client, "list_pipelines")
        upper = await call(client, "get_pipeline", name="upper")
        inlined = await call(client, "get_pipeline", name="loud", inlined=True)

    assert loud["uses"] == ["upper", "echo"]
    assert [p["name"] for p in loud["params"]] == ["n"]  # times is bound to n
    assert [p["name"] for p in lifted["params"]] == ["times"]  # lifted
    assert ran["outputs"] == ["HI HI"]
    assert {p["name"]: p["uses"] for p in listed["pipelines"]}["loud"] == [
        "upper",
        "echo",
    ]
    assert upper["used_by"] == ["loud"]
    assert "pipeline_ref" not in str(inlined["spec"])
    assert inlined["spec"]["root"]["children"][0]["name"] == "upper"


async def test_replacing_a_used_pipeline_checks_its_users(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await save(client, "upper", op("shout@1"))
        await save(client, "echo", ECHO, params={"times": 2})
        await save(client, "loud", LOUD, params={"n": 3})
        # words@1 gives an iterator, which repeat@1 cannot take.
        broken = await save(client, "upper", op("words@1"), replace=True)
        refused = await call_error(client, "delete_pipeline", name="upper")
        unused = await call(client, "delete_pipeline", name="loud")

    [dependent] = broken["dependents"]
    assert (dependent["name"], dependent["ok"]) == ("loud", False)
    assert refused["code"] == "bad_request" and refused["used_by"] == ["loud"]
    assert unused["name"] == "loud"


async def test_cycles_and_unknown_pipelines_are_errors(config: Config) -> None:
    async with Client(create_server(config)) as client:
        cycle = await call_error(
            client, "save_pipeline", name="loop", spec=ref("loop"), description=""
        )
        unknown = await call_error(
            client,
            "validate",
            spec={"kind": "seq", "children": [op("shout@1"), ref("nope")]},
        )

    assert cycle["code"] == "spec_error" and "loop → loop" in cycle["message"]
    assert unknown["code"] == "spec_error" and unknown["path"] == "1"
    assert "unknown pipeline 'nope'" in unknown["message"]


async def test_tap_passes_its_input_on(config: Config) -> None:
    spec = {
        "kind": "seq",
        "children": [
            {"kind": "tap", "children": [op("shout@1")]},
            {"kind": "op", "op": "repeat@1", "params": {"times": 2}},
        ],
    }
    async with Client(create_server(config)) as client:
        ran = await call(client, "run", spec=spec, input="a")

    assert ran["outputs"] == ["a a"]  # the input, not SHOUTED
