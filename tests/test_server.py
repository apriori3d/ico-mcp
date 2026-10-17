import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp.client import Client
from mcp_types import CallToolResult, TextContent

from ico_mcp.config import Config
from ico_mcp.server import INSTRUCTIONS, create_server, default_root

pytestmark = pytest.mark.anyio

TOOLS = {
    "search_operators",
    "get_operator",
    "validate",
    "run",
    "save_pipeline",
    "list_pipelines",
    "get_pipeline",
    "reload",
    "propose_operator",
    "propose_types",
    "list_types",
    "get_type",
    "dashboard",
    "run_status",
    "cancel_run",
}


def op(ref: str) -> dict[str, Any]:
    return {"kind": "op", "op": ref}


def seq(*refs: str) -> dict[str, Any]:
    return {"kind": "seq", "children": [op(ref) for ref in refs]}


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, text(result)
    return cast(dict[str, Any], result.structured_content)


async def call_error(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    message = text(result)
    return cast(dict[str, Any], json.loads(message[message.index("{") :]))


def text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


def traces(config: Config) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for file in sorted(config.runs_dir.glob("*.jsonl")):
        lines.extend(json.loads(line) for line in file.read_text().splitlines())
    return lines


async def test_tools_and_instructions(config: Config) -> None:
    async with Client(create_server(config)) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions

    assert {tool.name for tool in tools} == TOOLS
    assert instructions == INSTRUCTIONS
    read_only = {
        t.name for t in tools if t.annotations and t.annotations.read_only_hint
    }
    assert read_only == {
        "search_operators",
        "get_operator",
        "validate",
        "list_pipelines",
        "get_pipeline",
        "list_types",
        "get_type",
        "dashboard",
        "run_status",
    }


async def test_search_card_validate_run(config: Config) -> None:
    async with Client(create_server(config)) as client:
        found = await call(
            client, "search_operators", input_type="str", output_type="int"
        )
        card = await call(client, "get_operator", ref="count_words@1")
        checked = await call(
            client, "validate", spec=seq("split_words@1", "count_words@1")
        )
        result = await call(client, "run", spec=op("count_words@1"), input="a b c")

    assert [c["name"] for c in found["cards"]] == ["count_words"]
    assert "def count_words" in card["source"]
    assert checked["issues"][0]["expected"] == "str"
    assert result["outputs"] == [3]


async def test_kernel_errors_are_json_tool_errors(config: Config) -> None:
    async with Client(create_server(config)) as client:
        ambiguous = await call_error(client, "search_operators", output_type="Report")
        missing = await call_error(client, "get_operator", ref="nope@1")

    assert ambiguous["code"] == "type_error"
    assert len(ambiguous["candidates"]) == 2
    assert missing["code"] == "not_found"


async def test_run_writes_traces_for_success_and_failure(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await call(
            client, "run", spec=seq("shout@1", "count_words@1"), input="x" * 2000
        )
        failed = await call_error(client, "run", spec=op("fail@1"), input="x")

    ok, bad = traces(config)
    assert ok["ok"] is True
    assert ok["refs"] == ["count_words@1", "shout@1"]
    assert ok["result"] == {"total": 1, "truncated": False, "outputs": [1], "errors": 0}
    assert len(ok["input"]) == 1001  # shortened
    assert ok["ts"].endswith("Z")
    assert bad["ok"] is False
    assert bad["error"]["code"] == failed["code"] == "run_error"
    assert bad["result"] is None
    assert ok["progress"] == bad["progress"] == []


async def test_run_reports_the_first_progress_node(
    config: Config, progress_spec_of: Callable[[int], dict[str, Any]]
) -> None:
    updates: list[tuple[float, float | None, str | None]] = []

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        updates.append((progress, total, message))

    async with Client(create_server(config)) as client:
        result = await client.call_tool(
            "run",
            {"spec": progress_spec_of(4), "input": "a b c d"},
            progress_callback=on_progress,
        )
        without_token = await call(client, "run", spec=progress_spec_of(4), input="e f")

    assert not result.is_error
    assert updates[0] == (0, 4, "words 0/4")
    assert updates[-1] == (4, 4, "words 4/4")
    values = [u[0] for u in updates]
    assert values == sorted(set(values))  # strictly increasing
    assert without_token["outputs"] == ["E", "F"]
    first, _ = traces(config)
    assert first["progress"] == [{"key": "0", "label": "words", "done": 4, "total": 4}]


async def test_save_list_get_pipeline(config: Config) -> None:
    spec = seq("shout@1", "count_words@1")
    async with Client(create_server(config)) as client:
        saved = await call(
            client, "save_pipeline", name="count", spec=spec, description="Count words."
        )
        listed = await call(client, "list_pipelines")
        loaded = await call(client, "get_pipeline", name="count")
        result = await call(client, "run", spec=loaded["spec"], input="a b")

    assert saved == {
        "name": "count",
        "path": "pipelines/count.spec.json",
        "replaced": False,
    }
    assert listed == {"pipelines": [{"name": "count", "description": "Count words."}]}
    assert loaded["spec"] == {"version": 1, "root": spec}
    assert result["outputs"] == [2]


async def test_save_pipeline_replaces_only_on_request(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await call(
            client,
            "save_pipeline",
            name="p",
            spec=op("count_words@1"),
            description="v1",
        )
        exists = await call_error(
            client, "save_pipeline", name="p", spec=op("shout@1"), description="v2"
        )
        replaced = await call(
            client,
            "save_pipeline",
            name="p",
            spec=op("shout@1"),
            description="v2",
            replace=True,
        )
        loaded = await call(client, "get_pipeline", name="p")

    assert exists["code"] == "bad_request"
    assert "replace=true" in exists["message"]
    assert replaced["replaced"] is True
    assert loaded["description"] == "v2"


async def test_invalid_pipeline_is_not_saved(config: Config) -> None:
    async with Client(create_server(config)) as client:
        invalid = await call_error(
            client,
            "save_pipeline",
            name="bad",
            spec=seq("count_words@1", "shout@1"),
            description="",
        )
        bad_name = await call_error(
            client, "save_pipeline", name="Bad Name", spec=op("shout@1"), description=""
        )
        missing = await call_error(client, "get_pipeline", name="bad")

    assert invalid["code"] == "invalid_pipeline"
    assert invalid["issues"][0]["path"] == "1"
    assert bad_name["code"] == "bad_request"
    assert missing["code"] == "not_found"
    assert not (config.pipelines_dir / "bad.spec.json").exists()


async def test_reload_picks_up_new_drafts(config: Config) -> None:
    async with Client(create_server(config)) as client:
        before = await call(client, "search_operators", text="reverse")
        (config.draft_dir / "extra.py").write_text(
            "from ico.registry import register\n\n\n"
            '@register("reverse", 1)\n'
            "def reverse(text: str) -> str:\n"
            '    """Reverse a text."""\n'
            "    return text[::-1]\n"
        )
        report = await call(client, "reload")
        after = await call(client, "search_operators", text="reverse")

    assert before["total"] == 0
    assert "reverse@1" in report["draft"]["loaded"]
    assert after["cards"][0]["status"] == "draft"


def test_server_does_not_import_operators(config: Config) -> None:
    # The server imports ICO (for the Live Board) but never domain code:
    # operators are only loaded by the kernel.
    code = (
        "import sys, ico_mcp.server\n"
        "from ico.registry import default_registry\n"
        "from ico_mcp.config import load_config\n"
        f"ico_mcp.server.create_server(load_config({str(config.root)!r}))\n"
        "print(any(m.split('.')[0] in ('domain', 'domain_draft') for m in sys.modules),"
        " len(list(default_registry)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert out.stdout.split() == ["False", "0"]


def test_default_root_is_this_repository() -> None:
    assert default_root() == Path(__file__).resolve().parents[1]
    assert (default_root() / "ico.toml").is_file()


def test_main_reports_a_config_error(tmp_path: Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "ico_mcp", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert out.returncode == 1
    assert "ico.toml not found" in out.stderr


# ─── Dashboard ───


def board_get(url: str, path: str) -> Any:
    base, _, query = url.partition("/?")
    with urllib.request.urlopen(
        f"{base}{path}{'&' if '?' in path else '?'}{query}"
    ) as r:
        return json.loads(r.read())


async def test_runs_show_on_the_dashboard(
    config: Config, progress_spec_of: Callable[[int], dict[str, Any]]
) -> None:
    async with Client(create_server(config)) as client:
        url = (await call(client, "dashboard"))["url"]
        ok = await call(client, "run", spec=progress_spec_of(2), input="a b")
        failed = await call_error(client, "run", spec=op("fail@1"), input="x")
        listed = board_get(url, "/api/runs")
        done = listed["runs"][1]
        plan = board_get(url, f"/api/plan?run={done['id']}")
        page = urllib.request.urlopen(url).read().decode()
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            urllib.request.urlopen(url.replace("token=", "token=x"))

    assert url.startswith("http://127.0.0.1:")
    assert ok["dashboard"] == failed["dashboard"] == url
    bad = listed["runs"][0]
    assert (bad["title"], bad["state"]) == ("fail@1", "failed")
    assert "boom" in bad["error"]
    assert (done["title"], done["state"]) == ("shout@1, words@1", "done")
    assert done["summary"] == "2 output(s) · 0 item error(s)"
    assert [(p["label"], p["done"], p["total"]) for p in done["progress"]] == [
        ("words", 2.0, 2.0)
    ]
    flow = ["".join(s["text"] for s in row[0]) for row in plan["rows"]]
    assert any("for each in" in line for line in flow)
    assert "ICO Live Board" in page
    assert forbidden.value.code == 403


async def test_dashboard_url_survives_a_kernel_restart(config: Config) -> None:
    async with Client(create_server(config)) as client:
        before = (await call(client, "dashboard"))["url"]
        await call(client, "run", spec=op("shout@1"), input="a")
        await call(client, "reload")
        after = (await call(client, "dashboard"))["url"]
        runs = board_get(after, "/api/runs")["runs"]

    assert before == after
    assert [r["title"] for r in runs] == ["shout@1"]


# ─── Asynchronous runs ───


async def wait_for(check: Callable[[], bool], timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while not check():
            await anyio.sleep(0.05)


async def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await anyio.sleep(0.05)
    return False


async def poll_until_finished(client: Client, run_id: str) -> dict[str, Any]:
    with anyio.fail_after(20):
        while True:
            status = await call(client, "run_status", run_id=run_id)
            if status["state"] != "running":
                return status
            await anyio.sleep(0.1)


async def test_run_without_waiting(
    config: Config, progress_spec_of: Callable[[int], dict[str, Any]]
) -> None:
    async with Client(create_server(config)) as client:
        started = await call(
            client, "run", spec=progress_spec_of(3), input="a b c", wait=False
        )
        running = await call(client, "run_status", run_id=started["run_id"])
        finished = await poll_until_finished(client, started["run_id"])
        waited = await call(client, "run", spec=op("shout@1"), input="x")

    assert started["state"] == "running"
    assert started["dashboard"].startswith("http://127.0.0.1:")
    assert running["id"] == started["run_id"]
    assert finished["state"] == "done"
    assert finished["result"]["outputs"] == ["A", "B", "C"]
    assert finished["summary"] == "3 output(s) · 0 item error(s)"
    assert [(p["label"], p["done"]) for p in finished["progress"]] == [("words", 3)]
    assert waited["outputs"] == ["X"] and waited["run_id"] != started["run_id"]
    assert len(traces(config)) == 2


async def test_main_kernel_is_free_while_a_run_hangs(
    config: Config, tmp_path: Path
) -> None:
    pid_file = tmp_path / "worker.pid"
    async with Client(create_server(config)) as client:
        started = await call(
            client, "run", spec=op("hang@1"), input=str(pid_file), wait=False
        )
        await wait_for(pid_file.exists)
        found = await call(client, "search_operators", text="shout")
        await call(client, "reload")
        still = await call(client, "run_status", run_id=started["run_id"])
        cancelled = await call(client, "cancel_run", run_id=started["run_id"])
        again = await call(client, "cancel_run", run_id=started["run_id"])

    assert [c["name"] for c in found["cards"]] == ["shout"]
    assert still["state"] == "running"
    assert (cancelled["state"], cancelled["error"]) == (
        "failed",
        "the run was cancelled",
    )
    assert cancelled["kernel_error"]["code"] == "cancelled"
    assert again == cancelled
    assert await wait_dead(int(pid_file.read_text()))
    [trace] = traces(config)
    assert trace["error"]["code"] == "cancelled"


async def test_failed_run_status_and_unknown_run(config: Config) -> None:
    async with Client(create_server(config)) as client:
        failed = await call_error(client, "run", spec=op("fail@1"), input="x")
        status = await call(client, "run_status", run_id=failed["run_id"])
        missing = await call_error(client, "run_status", run_id="nope")
        not_cancelled = await call_error(client, "cancel_run", run_id="nope")

    assert failed["code"] == "run_error"
    assert status["state"] == "failed"
    assert status["kernel_error"]["code"] == "run_error"
    assert "result" not in status
    assert missing["code"] == not_cancelled["code"] == "not_found"


async def test_shutdown_kills_active_runs(config: Config, tmp_path: Path) -> None:
    pid_file = tmp_path / "worker.pid"
    async with Client(create_server(config)) as client:
        await call(client, "run", spec=op("hang@1"), input=str(pid_file), wait=False)
        await wait_for(pid_file.exists)

    assert await wait_dead(int(pid_file.read_text()))
