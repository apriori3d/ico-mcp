import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from ico_mcp.config import Config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, KernelError

MakeRepo = Callable[..., Config]

pytestmark = pytest.mark.anyio


def op(ref: str) -> JsonObject:
    return {"kind": "op", "op": ref}


def seq(*refs: str) -> JsonObject:
    return {"kind": "seq", "children": [op(ref) for ref in refs]}


async def request(
    kernel: KernelClient, method: str, params: JsonObject
) -> dict[str, Any]:
    return cast(dict[str, Any], await kernel.request(method, params))


async def kernel_error(
    kernel: KernelClient, method: str, params: JsonObject
) -> KernelError:
    with pytest.raises(KernelError) as info:
        await kernel.request(method, params)
    return info.value


def names(result: dict[str, Any]) -> list[str]:
    return [card["name"] for card in result["cards"]]


# ─── Load and search ───


async def test_load_reports_status_by_location(config: Config) -> None:
    async with KernelClient(config) as kernel:
        report = cast(dict[str, Any], await kernel.start())
        stable = await request(kernel, "search", {"text": "count"})
        draft = await request(kernel, "search", {"text": "shout"})

    assert "count_words@1" in report["stable"]["loaded"]
    assert "shout@1" in report["draft"]["loaded"]
    assert report["stable"]["errors"] == report["draft"]["errors"] == []
    assert stable["cards"][0]["status"] == "stable"
    assert draft["cards"][0]["status"] == "draft"


async def test_search_by_text_tags_and_limit(config: Config) -> None:
    async with KernelClient(config) as kernel:
        tagged = await request(kernel, "search", {"tags": ["text"]})
        limited = await request(kernel, "search", {"tags": ["text"], "limit": 1})

    assert names(tagged) == ["count_words", "shout", "split_words"]
    assert tagged["total"] == 3
    assert len(limited["cards"]) == 1
    assert limited["total"] == 3


async def test_search_by_type_names(config: Config) -> None:
    async with KernelClient(config) as kernel:
        from_str = await request(
            kernel, "search", {"input_type": "str", "output_type": "int"}
        )
        to_list = await request(kernel, "search", {"output_type": "list[ str ]"})
        qualified = await request(
            kernel, "search", {"output_type": "domain.text:Report"}
        )

    assert names(from_str) == ["count_words"]
    assert names(to_list) == ["split_words"]
    assert names(qualified) == ["make_report"]


async def test_ambiguous_type_name_lists_candidates(config: Config) -> None:
    async with KernelClient(config) as kernel:
        error = await kernel_error(kernel, "search", {"output_type": "Report"})

    assert error.code == "type_error"
    assert error.data["candidates"] == [
        "domain.text:Report",
        "domain_draft.tools:Report",
    ]


async def test_unknown_type_name(config: Config) -> None:
    async with KernelClient(config) as kernel:
        error = await kernel_error(kernel, "search", {"input_type": "Mask"})

    assert error.code == "type_error"
    assert "unknown type 'Mask'" in error.message


async def test_bad_params_and_method(config: Config) -> None:
    async with KernelClient(config) as kernel:
        bad_limit = await kernel_error(kernel, "search", {"limit": 0})
        bad_method = await kernel_error(kernel, "rm", {})

    assert bad_limit.code == bad_method.code == "bad_request"


# ─── Domain types ───

FRAMES = '''
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    """A video frame.

    Pixels stay in memory inside a pipeline.
    """

    path: str
    index: int
    tags: list[str]


class Model:
    """Not a dataclass: no fields."""

    model_fields = {"name": None}
    name: str


class Plain:
    """A plain class."""


class _Hidden:
    pass
'''


async def test_types_without_signatures_are_listed(config: Config) -> None:
    (config.draft_dir / "frames.py").write_text(FRAMES)
    async with KernelClient(config) as kernel:
        listed = await request(kernel, "types", {})
        found = await request(kernel, "search", {"input_type": "Frame"})

    cards = {card["type"]: card for card in listed["types"]}
    assert list(cards) == [
        "domain.text:Report",
        "domain_draft.frames:Frame",
        "domain_draft.frames:Model",
        "domain_draft.frames:Plain",
        "domain_draft.tools:Report",
    ]
    assert cards["domain_draft.frames:Frame"] == {
        "name": "Frame",
        "type": "domain_draft.frames:Frame",
        "status": "draft",
        "doc": "A video frame.",
    }
    assert cards["domain.text:Report"]["status"] == "stable"
    assert found == {"cards": [], "total": 0}


async def test_type_card_has_fields(config: Config) -> None:
    (config.draft_dir / "frames.py").write_text(FRAMES)
    async with KernelClient(config) as kernel:
        frame = await request(kernel, "type", {"name": "Frame"})
        model = await request(kernel, "type", {"name": "Model"})
        plain = await request(kernel, "type", {"name": "domain_draft.frames:Plain"})

    assert frame["module"] == "domain_draft.frames"
    assert frame["doc"] == "A video frame.\n\nPixels stay in memory inside a pipeline."
    assert frame["fields"] == [
        {"name": "path", "type": "str"},
        {"name": "index", "type": "int"},
        {"name": "tags", "type": "list[str]"},
    ]
    assert model["fields"] == [{"name": "name", "type": "str"}]
    assert plain["fields"] is None


async def test_type_card_errors(config: Config) -> None:
    async with KernelClient(config) as kernel:
        ambiguous = await kernel_error(kernel, "type", {"name": "Report"})
        library = await kernel_error(kernel, "type", {"name": "str"})

    assert ambiguous.data["candidates"] == [
        "domain.text:Report",
        "domain_draft.tools:Report",
    ]
    assert library.code == "type_error"
    assert "not a domain type" in library.message


async def test_module_types_lists_types_and_operators(config: Config) -> None:
    async with KernelClient(config) as kernel:
        found = await request(kernel, "module_types", {"module": "domain.text"})

    assert [card["name"] for card in found["types"]] == ["Report"]
    assert sorted(found["operators"]) == [
        "count_words@1",
        "make_report@1",
        "split_words@1",
    ]


# ─── Card ───


async def test_card_with_source(config: Config) -> None:
    async with KernelClient(config) as kernel:
        card = await request(kernel, "card", {"ref": "count_words@1"})
        missing = await kernel_error(kernel, "card", {"ref": "nope@1"})

    assert card["sig"] == "str → int"
    assert card["module"] == "domain.text"
    assert card["source"].startswith('@register("count_words", 1')
    assert missing.code == "not_found"


# ─── Validate ───


async def test_validate_valid_spec(config: Config) -> None:
    async with KernelClient(config) as kernel:
        result = await request(
            kernel, "validate", {"spec": seq("count_words@1", "make_report@1")}
        )

    assert [i for i in result["issues"] if i["severity"] == "error"] == []


async def test_validate_progress_and_stream_without_warnings(config: Config) -> None:
    spec: JsonObject = {
        "kind": "seq",
        "children": [
            op("words@1"),
            {
                "kind": "stream",
                "children": [
                    {
                        "kind": "seq",
                        "children": [
                            {"kind": "progress", "params": {"total": 3}},
                            op("shout@1"),
                        ],
                    }
                ],
            },
        ],
    }
    async with KernelClient(config) as kernel:
        result = await request(kernel, "validate", {"spec": spec})

    assert result["issues"] == []


async def test_validate_type_mismatch_has_path_and_types(config: Config) -> None:
    async with KernelClient(config) as kernel:
        spec: JsonObject = {"version": 1, "root": seq("count_words@1", "shout@1")}
        result = await request(kernel, "validate", {"spec": spec})

    [issue] = result["issues"]
    assert issue["severity"] == "error"
    assert issue["path"] == "1"
    assert (issue["expected"], issue["got"]) == ("str", "int")
    assert issue["refs"] == ["count_words@1", "shout@1"]


async def test_validate_unknown_operator(config: Config) -> None:
    async with KernelClient(config) as kernel:
        result = await request(
            kernel, "validate", {"spec": seq("count_words@1", "nope@1")}
        )

    [issue] = result["issues"]
    assert issue["path"] == "1"
    assert "nope@1" in issue["reason"]


# ─── Run ───


async def test_run_returns_outputs(config: Config) -> None:
    async with KernelClient(config) as kernel:
        result = await request(
            kernel, "run", {"spec": op("split_words@1"), "input": "a b"}
        )

    assert result == {
        "outputs": [["a", "b"]],
        "total": 1,
        "truncated": False,
        "errors": [],
        "drafts": {},
    }


async def test_run_reports_the_draft_code_it_used(config: Config) -> None:
    async with KernelClient(config) as kernel:
        result = await request(
            kernel, "run", {"spec": seq("shout@1", "count_words@1"), "input": "a b"}
        )
        card = await request(kernel, "card", {"ref": "shout@1"})
        stable = await request(kernel, "card", {"ref": "count_words@1"})

    assert result["drafts"] == {"shout@1": card["fingerprint"]}
    assert len(card["fingerprint"]) == 12
    assert "fingerprint" not in stable


async def test_run_streams_progress_notifications(
    config: Config, progress_spec_of: Callable[[int], JsonObject]
) -> None:
    notifications: list[JsonObject] = []

    async def collect(message: JsonObject) -> None:
        notifications.append(message)

    async with KernelClient(config) as kernel:
        result = await kernel.request(
            "run",
            {"spec": progress_spec_of(3), "input": "a b c"},
            on_notification=collect,
        )

    assert cast(dict[str, Any], result)["outputs"] == ["A", "B", "C"]
    plan, *progress = notifications
    assert plan["method"] == "plan"
    rows = cast(dict[str, Any], plan["params"])["plan"]["rows"]
    assert "".join(s["text"] for s in rows[0][0]) == "words"
    assert all(n["method"] == "progress" for n in progress)
    snapshots = [cast(dict[str, Any], n["params"])["snapshot"] for n in progress]
    assert (snapshots[0]["label"], snapshots[0]["done"]) == ("words", 0)
    assert (snapshots[-1]["done"], snapshots[-1]["total"]) == (3, 3)


async def test_operator_stdout_does_not_break_the_protocol(config: Config) -> None:
    async with KernelClient(config) as kernel:
        result = await request(kernel, "run", {"spec": op("shout@1"), "input": "hi"})
        again = await request(kernel, "run", {"spec": op("shout@1"), "input": "yo"})

    assert result["outputs"] == ["HI"]
    assert again["outputs"] == ["YO"]


async def test_no_plan_for_a_spec_that_does_not_build(config: Config) -> None:
    notifications: list[JsonObject] = []

    async def collect(message: JsonObject) -> None:
        notifications.append(message)

    async with KernelClient(config) as kernel:
        with pytest.raises(KernelError):
            await kernel.request(
                "run", {"spec": op("nope@1"), "input": "a"}, on_notification=collect
            )

    assert notifications == []


async def test_run_error_and_spec_error(config: Config) -> None:
    async with KernelClient(config) as kernel:
        failed = await kernel_error(kernel, "run", {"spec": op("fail@1"), "input": "x"})
        unbuilt = await kernel_error(kernel, "run", {"spec": op("nope@1")})
        alive = await request(
            kernel, "run", {"spec": op("count_words@1"), "input": "a"}
        )

    assert failed.code == "run_error"
    assert failed.message == "RuntimeError: boom"
    assert "raise RuntimeError" in cast(str, failed.data["traceback"])
    assert unbuilt.code == "spec_error"
    assert alive["outputs"] == [1]


# ─── Process control ───


async def test_timeout_kills_kernel_and_its_workers(
    tmp_path: Path, make_repo: MakeRepo
) -> None:
    config = make_repo(tmp_path / "repo", "timeouts = { run = 1 }\n")
    pid_file = tmp_path / "worker.pid"

    async with KernelClient(config) as kernel:
        error = await kernel_error(
            kernel, "run", {"spec": op("hang@1"), "input": str(pid_file)}
        )
        assert not kernel.running
        alive = await request(
            kernel, "run", {"spec": op("count_words@1"), "input": "a b"}
        )

    assert error.code == "timeout"
    assert alive["outputs"] == [2]
    assert await wait_dead(int(pid_file.read_text()))


async def test_crash_is_reported_and_kernel_restarts(config: Config) -> None:
    async with KernelClient(config) as kernel:
        error = await kernel_error(kernel, "run", {"spec": op("crash@1"), "input": "x"})
        alive = await request(
            kernel, "run", {"spec": op("count_words@1"), "input": "a"}
        )

    assert error.code == "crashed"
    assert error.data["returncode"] == 3
    assert "dying" in cast(str, error.data["stderr"])
    assert alive["outputs"] == [1]


async def test_env_is_an_allowlist(
    make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_repo(tmp_path, 'env = ["ICO_TEST_ALLOWED"]\n')
    monkeypatch.setenv("ICO_TEST_ALLOWED", "yes")
    monkeypatch.setenv("ICO_TEST_SECRET", "token")

    async with KernelClient(config) as kernel:
        allowed = await request(
            kernel, "run", {"spec": op("getenv@1"), "input": "ICO_TEST_ALLOWED"}
        )
        secret = await request(
            kernel, "run", {"spec": op("getenv@1"), "input": "ICO_TEST_SECRET"}
        )

    assert allowed["outputs"] == ["yes"]
    assert secret["outputs"] == ["<unset>"]


def test_client_does_not_import_the_registry() -> None:
    code = "import sys, ico_mcp.kernel_client; print('ico.registry' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "False"


async def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await anyio.sleep(0.05)
    return False


async def test_run_with_profile_notifies_and_returns_the_profile(
    config: Config, progress_spec_of: Callable[[int], JsonObject]
) -> None:
    notifications: list[JsonObject] = []

    async def collect(message: JsonObject) -> None:
        notifications.append(message)

    async with KernelClient(config) as kernel:
        result = cast(
            dict[str, Any],
            await kernel.request(
                "run",
                {"spec": progress_spec_of(3), "input": "a b c", "profile": True},
                on_notification=collect,
            ),
        )
        with pytest.raises(KernelError) as bad:
            await request(
                kernel, "run", {"spec": op("shout@1"), "input": "x", "profile": "yes"}
            )

    assert result["outputs"] == ["A", "B", "C"]
    profile = result["profile"]
    assert profile["final"] is True
    nodes = {n["label"]: n for n in profile["nodes"]}
    assert nodes["shout@1"]["calls"] == 3
    # Keys of the profile are the keys of the plan's rows.
    plan = cast(dict[str, Any], notifications[0]["params"])["plan"]
    keys = {k for k in plan["keys"] if k is not None}
    assert keys <= {n["key"] for n in profile["nodes"]}
    profiled = [n for n in notifications if n["method"] == "profile"]
    assert profiled  # at least the final snapshot
    assert bad.value.code == "bad_request"
