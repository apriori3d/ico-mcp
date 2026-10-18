import dataclasses
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

from ico_mcp import jobs as jobs_module
from ico_mcp.config import Config, Timeouts
from ico_mcp.server import INSTRUCTIONS, create_server

REPO = Path(__file__).resolve().parents[1]
MakeRepo = Callable[..., Config]

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
    "crystallize",
    "guide",
    "server_status",
    "delete_pipeline",
    "environment",
    "wait_run",
    "wait_crystallize",
    "sample_run",
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
    assert instructions is not None
    assert instructions.startswith("ico-mcp 0.1.0 (")
    assert instructions.endswith(INSTRUCTIONS)
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
        "guide",
        "server_status",
        "environment",
        "wait_run",
        "wait_crystallize",
        "sample_run",
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
        "params": [],
    }
    assert listed == {
        "pipelines": [{"name": "count", "description": "Count words.", "params": []}]
    }
    assert loaded["spec"] == {"version": 1, "root": spec}
    assert (loaded["params"], loaded["check_params"]) == ([], None)
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
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One long item does not stop between items: it is killed after the grace.
    monkeypatch.setattr(jobs_module, "CANCEL_GRACE", 0.5)
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
    assert "result" not in cancelled


def slow_spec(total: int) -> dict[str, Any]:
    """``words@1`` then a stream of ``slow@1`` behind a progress node."""
    body = {
        "kind": "seq",
        "children": [
            {"kind": "progress", "name": "words", "params": {"total": total}},
            {"kind": "op", "op": "slow@1"},
        ],
    }
    return {
        "kind": "seq",
        "children": [op("words@1"), {"kind": "stream", "children": [body]}],
    }


async def test_cancel_returns_the_outputs_so_far(config: Config) -> None:
    words = [f"w{i}" for i in range(200)]
    async with Client(create_server(config)) as client:
        started = await call(
            client, "run", spec=slow_spec(200), input=" ".join(words), wait=False
        )

        async def advanced() -> bool:
            status = await call(client, "run_status", run_id=started["run_id"])
            return any(p["done"] >= 3 for p in status["progress"])

        with anyio.fail_after(20):
            while not await advanced():
                await anyio.sleep(0.05)
        clock = time.monotonic()
        cancelled = await call(client, "cancel_run", run_id=started["run_id"])
        elapsed = time.monotonic() - clock

    assert elapsed < 5  # stopped between items, not killed after the grace
    assert cancelled["state"] == "failed"
    assert cancelled["kernel_error"]["code"] == "cancelled"
    outputs = cancelled["result"]["outputs"]
    assert 3 <= len(outputs) < len(words)
    assert outputs == words[: len(outputs)]
    assert cancelled["result"]["cancelled"] == "cancel_run"
    assert "cancelled" in cancelled["summary"]
    [trace] = traces(config)
    assert trace["ok"] is False and trace["error"]["code"] == "cancelled"
    assert trace["result"]["outputs"] == outputs[:10]  # traces keep ten


async def start_slow_run(client: Client, spec: dict[str, Any], words: list[str]) -> str:
    """Start a run of ``spec`` over ``words`` and wait until it is going."""
    started = await call(client, "run", spec=spec, input=" ".join(words), wait=False)
    run_id: str = started["run_id"]
    with anyio.fail_after(20):
        while True:
            status = await call(client, "run_status", run_id=run_id)
            if any(p["done"] >= 2 for p in status["progress"]):
                return run_id
            await anyio.sleep(0.05)


async def test_sample_run_shows_what_flows_out_of_a_node(config: Config) -> None:
    words = [f"w{i}" for i in range(200)]
    async with Client(create_server(config)) as client:
        run_id = await start_slow_run(client, slow_spec(200), words)
        by_label = await call(client, "sample_run", run_id=run_id, node="slow@1")
        by_name = await call(client, "sample_run", run_id=run_id, node="slow", count=1)
        by_key = await call(
            client, "sample_run", run_id=run_id, node=by_label["node"], count=2
        )
        await call(client, "cancel_run", run_id=run_id)

    assert by_label["label"] == "slow@1" and by_label["complete"] is True
    assert by_label["state"] == "running"
    samples = by_label["samples"]
    assert [s["index"] for s in samples] == [0, 1, 2]
    assert all(s["type"] == "str" and s["node"] == by_label["node"] for s in samples)
    first = words.index(samples[0]["preview"].strip("'"))
    assert [s["preview"] for s in samples] == [
        repr(w) for w in words[first : first + 3]
    ]
    assert by_name["node"] == by_label["node"] and len(by_name["samples"]) == 1
    assert by_key["label"] == "slow@1" and len(by_key["samples"]) == 2


async def test_sample_run_errors(config: Config) -> None:
    body = {
        "kind": "seq",
        "children": [
            {"kind": "progress", "name": "words", "params": {"total": 200}},
            op("slow@1"),
            op("slow@1"),
        ],
    }
    spec = {
        "kind": "seq",
        "children": [op("words@1"), {"kind": "stream", "children": [body]}],
    }
    async with Client(create_server(config)) as client:
        run_id = await start_slow_run(client, spec, [f"w{i}" for i in range(200)])
        ambiguous = await call_error(client, "sample_run", run_id=run_id, node="slow")
        unknown = await call_error(client, "sample_run", run_id=run_id, node="nope")
        too_many = await call_error(
            client, "sample_run", run_id=run_id, node="slow", count=21
        )
        await call(client, "cancel_run", run_id=run_id)
        ended = await call_error(client, "sample_run", run_id=run_id, node="0")
        missing = await call_error(client, "sample_run", run_id="nope", node="0")

    assert ambiguous["code"] == "not_found"
    assert [c["label"] for c in ambiguous["candidates"]] == ["slow@1", "slow@1"]
    assert unknown["code"] == "not_found"
    assert "words@1" in [n["label"] for n in unknown["nodes"]]
    assert too_many["code"] == "bad_request"
    assert ended["code"] == "not_running"
    assert missing["code"] == "not_found"


async def test_cancel_while_the_kernel_starts(config: Config) -> None:
    async with Client(create_server(config)) as client:
        started = await call(client, "run", spec=op("shout@1"), input="x", wait=False)
        cancelled = await call(client, "cancel_run", run_id=started["run_id"])

    assert cancelled["state"] == "failed"
    assert cancelled["kernel_error"]["code"] == "cancelled"


async def test_cancel_of_a_finished_run_changes_nothing(config: Config) -> None:
    async with Client(create_server(config)) as client:
        started = await call(client, "run", spec=op("shout@1"), input="x", wait=False)
        finished = await poll_until_finished(client, started["run_id"])
        cancelled = await call(client, "cancel_run", run_id=started["run_id"])

    assert finished["state"] == "done"
    assert cancelled == finished


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


# ─── Guide ───


async def test_guide_is_the_package_guide_with_the_domain_notes(
    config: Config,
) -> None:
    async with Client(create_server(config)) as client:
        without = text(await client.call_tool("guide", {}))
        (config.root / "docs").mkdir()
        (config.root / "docs" / "domain.md").write_text(
            "## 8. Domain notes\n\nVideos.\n"
        )
        with_notes = text(await client.call_tool("guide", {}))

    package_guide = (REPO / "src" / "ico_mcp" / "agent-guide.md").read_text()
    assert without.startswith(package_guide.rstrip())
    assert without.endswith("This domain has no notes yet (`docs/domain.md`).\n")
    assert with_notes.endswith("## 8. Domain notes\n\nVideos.\n")


async def test_about_goes_into_the_instructions(
    make_repo: MakeRepo, tmp_path: Path
) -> None:
    about = make_repo(tmp_path / "video", 'about = "Removes subtitles from videos."\n')
    async with Client(create_server(about)) as client:
        with_about = client.instructions
    async with Client(create_server(make_repo(tmp_path / "plain"))) as client:
        plain = client.instructions

    assert with_about is not None and plain is not None
    assert "This domain (demo): Removes subtitles from videos." in with_about
    assert plain.endswith(INSTRUCTIONS)


def test_guide_covers_every_spec_kind() -> None:
    from ico.spec.builders import _BUILDERS  # pyright: ignore[reportPrivateUsage]  # the kinds ICO can build

    guide = (REPO / "src" / "ico_mcp" / "agent-guide.md").read_text()
    missing = [kind for kind in ["op", *_BUILDERS] if f"| `{kind}` |" not in guide]
    assert missing == []


def test_readme_lists_every_tool() -> None:
    readme = (REPO / "README.md").read_text()
    assert [tool for tool in sorted(TOOLS) if f"`{tool}`" not in readme] == []


async def test_delete_pipeline(config: Config) -> None:
    async with Client(create_server(config)) as client:
        await call(
            client, "save_pipeline", name="old", spec=op("shout@1"), description=""
        )
        await call(
            client, "save_pipeline", name="kept", spec=op("shout@1"), description=""
        )
        deleted = await call(client, "delete_pipeline", name="old")
        listed = await call(client, "list_pipelines")
        missing = await call_error(client, "delete_pipeline", name="old")
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert deleted == {"name": "old", "path": "pipelines/old.spec.json"}
    assert [p["name"] for p in listed["pipelines"]] == ["kept"]
    assert (missing["code"], missing["saved"]) == ("not_found", ["kept"])
    annotations = tools["delete_pipeline"].annotations
    assert annotations is not None and annotations.destructive_hint is True


async def test_traces_record_the_draft_code(config: Config) -> None:
    async with Client(create_server(config)) as client:
        result = await call(client, "run", spec=op("shout@1"), input="x")
        card = await call(client, "get_operator", ref="shout@1")

    assert "drafts" not in result
    [trace] = traces(config)
    assert trace["drafts"] == {"shout@1": card["fingerprint"]}


# ─── wait_run ───


async def test_wait_run_returns_when_the_run_ends(
    config: Config, progress_spec_of: Callable[[int], dict[str, Any]]
) -> None:
    updates: list[tuple[float, float | None, str | None]] = []

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        updates.append((progress, total, message))

    async with Client(create_server(config)) as client:
        started = await call(
            client, "run", spec=progress_spec_of(3), input="a b c", wait=False
        )
        clock = time.monotonic()
        finished = await call(client, "wait_run", run_id=started["run_id"], timeout=30)
        elapsed = time.monotonic() - clock
        again = await client.call_tool(
            "wait_run",
            {"run_id": started["run_id"], "timeout": 5},
            progress_callback=on_progress,
        )

    assert finished["state"] == "done" and elapsed < 25
    assert finished["result"]["outputs"] == ["A", "B", "C"]
    assert not again.is_error
    assert updates == [(3, 3, "words 3/3")]  # the first node, from the board


async def test_wait_run_times_out_on_a_running_run(
    config: Config, tmp_path: Path
) -> None:
    pid_file = tmp_path / "worker.pid"
    async with Client(create_server(config)) as client:
        started = await call(
            client, "run", spec=op("hang@1"), input=str(pid_file), wait=False
        )
        await wait_for(pid_file.exists)
        clock = time.monotonic()
        still = await call(client, "wait_run", run_id=started["run_id"], timeout=1)
        elapsed = time.monotonic() - clock
        await call(client, "cancel_run", run_id=started["run_id"])
        cancelled = await call(client, "wait_run", run_id=started["run_id"], timeout=5)

    assert still["state"] == "running" and 0.9 <= elapsed < 5
    assert cancelled["state"] == "failed"
    assert cancelled["kernel_error"]["code"] == "cancelled"


async def test_wait_run_errors(config: Config) -> None:
    async with Client(create_server(config)) as client:
        missing = await call_error(client, "wait_run", run_id="nope")
        done = await call(client, "run", spec=op("shout@1"), input="x")
        too_short = await call_error(
            client, "wait_run", run_id=done["run_id"], timeout=0
        )
        long = await call(client, "wait_run", run_id=done["run_id"], timeout=601)

    assert missing["code"] == "not_found"
    assert too_short["code"] == "bad_request"
    assert long["state"] == "done" and "waited" not in long  # cut, not an error


async def test_calls_wait_at_most_timeouts_wait(config: Config, tmp_path: Path) -> None:
    config = dataclasses.replace(config, timeouts=Timeouts(wait=1))
    pid_file = tmp_path / "worker.pid"
    async with Client(create_server(config)) as client:
        clock = time.monotonic()
        started = await call(client, "run", spec=op("hang@1"), input=str(pid_file))
        run_elapsed = time.monotonic() - clock
        clock = time.monotonic()
        still = await call(client, "wait_run", run_id=started["run_id"], timeout=600)
        wait_elapsed = time.monotonic() - clock
        quick = await call(client, "run", spec=op("shout@1"), input="x")
        await call(client, "cancel_run", run_id=started["run_id"])

    # A run longer than timeouts.wait goes on in the background.
    assert started["state"] == "running" and started["waited"] == 1
    assert started["run_id"] and started["dashboard"]
    assert 0.9 <= run_elapsed < 10
    # wait_run cuts its timeout to timeouts.wait.
    assert still["state"] == "running" and still["waited"] == 1
    assert 0.9 <= wait_elapsed < 10
    # A quick run returns its result as before.
    assert quick["outputs"] == ["X"] and "waited" not in quick


async def test_profiled_run_summary_dashboard_and_trace(
    config: Config, progress_spec_of: Callable[[int], dict[str, Any]]
) -> None:
    async with Client(create_server(config)) as client:
        url = (await call(client, "dashboard"))["url"]
        plain = await call(client, "run", spec=op("shout@1"), input="x")
        result = await call(
            client, "run", spec=progress_spec_of(3), input="a b c", profile=True
        )
        cells = board_get(url, f"/api/profile?run={result['run_id']}")
        plan = board_get(url, f"/api/plan?run={result['run_id']}")
        started = await call(
            client,
            "run",
            pipeline=None,
            spec=op("shout@1"),
            input="y",
            profile=True,
            wait=False,
        )
        waited = await call(client, "wait_run", run_id=started["run_id"], timeout=30)

    assert "profile" not in plain
    profile = result["profile"]
    assert set(profile) == {"elapsed", "overhead", "top"}
    top = profile["top"]
    assert 0 < len(top) <= 5
    assert set(top[0]) == {"key", "label", "self", "share", "count", "p95", "info"}
    labels = [n["label"] for n in top]
    assert all(" | " not in label for label in labels)  # no chains
    assert set(labels) <= {"words@1", "words", "shout@1", "IcoStream"}
    # The dashboard has the profile, keyed like the plan's rows.
    assert {k for k in plan["keys"] if k is not None} <= set(cells)
    assert waited["result"]["profile"]["top"][0]["label"] == "shout@1"

    lines = traces(config)
    assert lines[0]["profile"] is None
    profiled = lines[1]["profile"]
    shout = next(n for n in profiled if n["label"] == "shout@1")
    assert shout["calls"] == 3 and shout["self"] >= 0
    assert set(shout) >= {"key", "total", "cpu", "p95", "startup", "utilization"}
