"""``crystallize``: propose drafts as stable operators in a pull request.

The work happens in a temporary git worktree of the base branch, so the
working tree (from which the server runs) is never switched or changed:

1. check: ``github`` is configured, ``gh`` is authenticated, the refs are
   drafts and the pipelines are saved;
2. worktree: ``crystallize/<slug>`` from ``origin/<base>``;
3. move: the modules of the refs and the draft modules they import
   relatively into the stable package, with the examples of their
   operators and the given pipelines; files are copied unchanged;
4. gate: the stable package loads, passes pyright strict and never imports
   the draft package; the examples of stable operators pass
   (``tests/test_domain_examples.py``); every saved pipeline validates;
5. publish: commit, push, ``gh pr create`` with the operator cards, the gate
   report and trace statistics.

Drafts stay in the working tree until the pull request is merged and
pulled; ``reload`` then retires them (:func:`ico_mcp.drafts.retire`).

The gate can take longer than a client's tool-call timeout, so the server
runs a crystallization in the background (:class:`Crystallizations`) and a
tool call waits for it at most ``timeouts.wait``.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, cast

import anyio
from anyio.abc import TaskGroup

from ico_mcp import drafts, pipelines
from ico_mcp.config import Config, load_config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError
from ico_mcp.typecheck import TypecheckError, typecheck

Stage = Literal["check", "git", "gate", "publish"]

EXAMPLES_TEST = "tests/test_domain_examples.py"
KEPT = 10
"""Finished crystallizations kept for ``wait_crystallize``."""
RECENT_RUNS = 3
"""Successful runs shown per operator in the pull request."""
RUN_INPUT_MAX = 80
OUTPUT_TAIL = 4000


class CrystallizeError(Exception):
    """A stage failed; nothing was pushed."""

    def __init__(
        self, stage: Stage, message: str, data: JsonObject | None = None
    ) -> None:
        super().__init__(message)
        self.stage: Stage = stage
        self.message = message
        self.data: JsonObject = data or {}

    def to_dict(self) -> JsonObject:
        return {
            "code": "crystallize_failed",
            "stage": self.stage,
            "message": self.message,
            **self.data,
        }


async def crystallize(
    config: Config,
    kernel: KernelClient,
    *,
    refs: Sequence[str],
    title: str,
    summary: str,
    pipeline_names: Sequence[str] = (),
    drafts_lock: anyio.Lock,
    on_stage: Callable[[Stage], None] = lambda _: None,
) -> JsonObject:
    """Open a pull request that makes ``refs`` stable.

    ``drafts_lock`` is held while the drafts are read (check and move), not
    during the gate and the push, so proposals can go on meanwhile.
    ``on_stage`` is told each stage as it begins.

    Returns:
        ``{"pr_url", "branch", "modules", "added_modules", "gate"}``.

    Raises:
        CrystallizeError: At the first failing stage; the branch is not
            pushed and the worktree is removed.
    """
    github = config.github
    if github is None:
        raise CrystallizeError(
            "check",
            'set github = { repo = "owner/name", base = "main" } in ico.toml',
        )
    if not refs:
        raise CrystallizeError("check", "give at least one operator ref")
    locked = True
    await drafts_lock.acquire()
    try:
        on_stage("check")
        await _check_gh(config)
        cards = await _draft_cards(kernel, refs)
        for name in pipeline_names:
            if not pipelines.exists(config.pipelines_dir, name):
                raise CrystallizeError("check", f"no saved pipeline {name!r}")
        # The pipelines they use go with them, as relative imports do.
        pipeline_names = pipelines.closure(config.pipelines_dir, pipeline_names)

        draft_package = config.draft_dir.name
        direct = sorted(
            {str(c["module"]).removeprefix(f"{draft_package}.") for c in cards}
        )
        modules = drafts.closure(config.draft_dir, direct)
        module_refs = await _module_refs(kernel, draft_package, modules)

        on_stage("git")
        await _git(config.root, "fetch", "origin", github.base)
        branch = await _free_branch(config.root, f"crystallize/{_slug(title)}")
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "worktree"
            await _git(
                config.root,
                "worktree",
                "add",
                "-b",
                branch,
                str(tree),
                f"origin/{github.base}",
            )
            pushed = False
            try:
                _copy(config, tree, modules, module_refs, pipeline_names)
                # The branch exists and the drafts are copied: the rest
                # works on the worktree only.
                drafts_lock.release()
                locked = False
                on_stage("gate")
                gate = await _gate(load_config(tree), module_refs)
                if not all(step["ok"] for step in gate):
                    raise CrystallizeError(
                        "gate",
                        "the gate failed; nothing was pushed",
                        {"gate": cast(list[JsonValue], gate)},
                    )
                on_stage("publish")
                await _git(tree, "add", "-A")
                await _git(tree, "commit", "-q", "-m", title, "-m", summary)
                await _git(tree, "push", "-q", "-u", "origin", branch)
                pushed = True
                body = _body(config, summary, cards, direct, modules, gate)
                url = await _pr(config, tree, branch, title, body)
            finally:
                # Also when the server stops a background crystallization.
                with anyio.CancelScope(shield=True):
                    await _git(config.root, "worktree", "remove", "--force", str(tree))
                    if not pushed:
                        await _git(
                            config.root, "branch", "-q", "-D", branch, check=False
                        )
    finally:
        if locked:
            drafts_lock.release()
    added: list[JsonValue] = []
    added.extend(m for m in modules if m not in direct)
    moved: list[JsonValue] = []
    moved.extend(modules)
    return {
        "pr_url": url,
        "branch": branch,
        "modules": moved,
        "added_modules": added,
        "gate": cast(list[JsonValue], gate),
    }


# ─── Background ───


class Crystallization:
    """One ``crystallize`` call: its stage and, once finished, its outcome."""

    def __init__(self) -> None:
        self.id = f"c-{uuid.uuid4().hex[:8]}"
        self.stage: Stage = "check"
        self.result: JsonObject | None = None
        self.error: JsonObject | None = None
        self.finished = anyio.Event()

    def running(self, waited: float) -> JsonObject:
        return {
            "crystallize_id": self.id,
            "state": "running",
            "stage": self.stage,
            "waited": waited,
        }


class Crystallizations:
    """Crystallizations that go on in the background, so that no tool call
    waits for one longer than ``timeouts.wait``; use as an async context
    manager (the server lifespan), which stops active ones on exit."""

    def __init__(self) -> None:
        self._all: dict[str, Crystallization] = {}
        self._tasks: TaskGroup | None = None

    async def __aenter__(self) -> Self:
        self._tasks = anyio.create_task_group()
        await self._tasks.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        tasks, self._tasks = self._tasks, None
        if tasks is not None:
            tasks.cancel_scope.cancel()
            await tasks.__aexit__(exc_type, exc, tb)

    def start(
        self, work: Callable[[Callable[[Stage], None]], Awaitable[JsonObject]]
    ) -> Crystallization:
        """Start ``work(on_stage)`` (a :func:`crystallize` call)."""
        if self._tasks is None:
            raise RuntimeError("Crystallizations is not entered")
        job = Crystallization()
        finished = [i for i, c in self._all.items() if c.finished.is_set()]
        for old in finished[: max(0, len(finished) - KEPT)]:
            del self._all[old]
        self._all[job.id] = job
        self._tasks.start_soon(self._run, job, work)
        return job

    def get(self, crystallize_id: str) -> Crystallization:
        """A crystallization by id.

        Raises:
            KernelError: ``not_found`` for an unknown or forgotten one.
        """
        job = self._all.get(crystallize_id)
        if job is None:
            raise KernelError(
                "not_found",
                f"no crystallization {crystallize_id!r}; only active and recent"
                " ones are kept",
                {"crystallize_id": crystallize_id},
            )
        return job

    async def _run(
        self,
        job: Crystallization,
        work: Callable[[Callable[[Stage], None]], Awaitable[JsonObject]],
    ) -> None:
        def on_stage(stage: Stage) -> None:
            job.stage = stage

        try:
            job.result = await work(on_stage)
        except CrystallizeError as e:
            job.error = e.to_dict()
        except Exception as e:  # a bug must not stop the server
            job.error = CrystallizeError(job.stage, repr(e)).to_dict()
        finally:
            job.finished.set()


# ─── Check ───


async def _check_gh(config: Config) -> None:
    if shutil.which("gh") is None:
        raise CrystallizeError(
            "check", "gh is not installed: https://cli.github.com, then gh auth login"
        )
    done = await anyio.run_process(
        ["gh", "auth", "status"], cwd=config.root, check=False
    )
    if done.returncode != 0:
        raise CrystallizeError(
            "check",
            "gh is not authenticated: run gh auth login",
            {"output": _text(done.stderr or done.stdout)},
        )


async def _draft_cards(kernel: KernelClient, refs: Sequence[str]) -> list[JsonObject]:
    cards: list[JsonObject] = []
    for ref in refs:
        try:
            card = cast(JsonObject, await kernel.request("card", {"ref": ref}))
        except KernelError as e:
            raise CrystallizeError("check", e.message, {"ref": ref}) from None
        if card.get("status") != "draft":
            raise CrystallizeError(
                "check", f"{ref} is {card.get('status')}, not a draft", {"ref": ref}
            )
        cards.append(card)
    return cards


async def _module_refs(
    kernel: KernelClient, draft_package: str, modules: Sequence[str]
) -> list[str]:
    """Refs of all operators the moved modules register (for examples)."""
    refs: list[str] = []
    for module in modules:
        found = cast(
            JsonObject,
            await kernel.request(
                "module_cards", {"module": f"{draft_package}.{module}"}
            ),
        )
        for card in cast(list[JsonObject], found["cards"]):
            refs.append(f"{card['name']}@{card['version']}")
    return refs


# ─── Git ───


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    done = await anyio.run_process(["git", *args], cwd=cwd, check=False)
    if check and done.returncode != 0:
        raise CrystallizeError(
            "git",
            f"git {args[0]} failed",
            {"command": ["git", *args], "output": _text(done.stderr or done.stdout)},
        )
    return done.stdout.decode(errors="replace")


async def _free_branch(root: Path, name: str) -> str:
    """``name``, or ``name-2``, ``name-3``... if taken locally or on origin."""
    candidate, n = name, 1
    while True:
        local = await _git(root, "branch", "--list", candidate)
        remote = await _git(root, "ls-remote", "--heads", "origin", candidate)
        if not local.strip() and not remote.strip():
            return candidate
        n += 1
        candidate = f"{name}-{n}"


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].rstrip("-") or "operators"


# ─── Move ───


def _copy(
    config: Config,
    tree: Path,
    modules: Sequence[str],
    refs: Sequence[str],
    pipeline_names: Sequence[str],
) -> None:
    target = load_config(tree)
    for module in modules:
        shutil.copy2(
            config.draft_dir / f"{module}.py", target.stable_dir / f"{module}.py"
        )
        # Drafts committed in the domain repository move: the branch drops
        # them, and the merge removes them from the working copy.
        (target.draft_dir / f"{module}.py").unlink(missing_ok=True)
    target.examples_dir.mkdir(exist_ok=True)
    for ref in refs:
        example = config.examples_dir / f"{ref}.json"
        if example.is_file():
            shutil.copy2(example, target.examples_dir / example.name)
    target.pipelines_dir.mkdir(exist_ok=True)
    for name in pipeline_names:
        source = config.pipelines_dir / f"{name}.spec.json"
        shutil.copy2(source, target.pipelines_dir / source.name)
    # The manifest goes with the operators that use its assets.
    for manifest in (
        config.assets_manifest,
        config.assets_manifest.with_name("assets.lock"),
    ):
        if manifest.is_file():
            shutil.copy2(manifest, tree / manifest.name)


# ─── Gate ───


async def _gate(config: Config, refs: Sequence[str]) -> list[JsonObject]:
    """Run every gate step on the worktree; each is ``{step, ok, diagnostics}``."""
    loaded, assets = await _gate_load_and_pipelines(config, refs)
    return [
        loaded,
        await _gate_typecheck(config),
        _gate_no_draft_imports(config),
        await _gate_examples(config),
        assets,
    ]


async def _gate_assets(kernel: KernelClient, refs: Sequence[str]) -> JsonObject:
    """Every asset the moved operators use (their asset params and the
    fields of their resources) is in ``assets.toml`` and ``assets.lock``.
    The files are not needed. The step keeps ``assets`` (id, size, used
    by) for the pull request."""
    problems: list[JsonObject] = []
    used: dict[str, JsonObject] = {}
    for ref in refs:
        card = cast(JsonObject, await kernel.request("card", {"ref": ref}))
        for item in cast(list[JsonObject], card.get("assets", [])):
            id, state = str(item["id"]), item.get("state")
            if state in ("unknown", "unlocked", "no_manifest"):
                problems.append(
                    {"ref": ref, "asset": id, "state": state,
                     "message": f"{ref} uses asset {id!r}, {state}: add it to "
                     "assets.toml and run `ico assets lock`"}
                )  # fmt: skip
                continue
            entry = used.setdefault(
                id, {"id": id, "size": item.get("size"), "used_by": []}
            )
            cast(list[JsonValue], entry["used_by"]).append(ref)
    step = _step("assets in the manifest", problems)
    step["assets"] = list[JsonValue](used[id] for id in sorted(used))
    return step


def _step(step: str, diagnostics: list[JsonObject]) -> JsonObject:
    out: list[JsonValue] = []
    out.extend(diagnostics)
    return {"step": step, "ok": not diagnostics, "diagnostics": out}


async def _gate_load_and_pipelines(
    config: Config, refs: Sequence[str]
) -> tuple[JsonObject, JsonObject]:
    """The stable package loads and every saved pipeline validates; and the
    assets step (:func:`_gate_assets`), in the same kernel."""
    problems: list[JsonObject] = []
    async with KernelClient(config) as kernel:
        try:
            report = cast(JsonObject, await kernel.start())
        except KernelError as e:
            failed = _step("load and validate pipelines", [e.to_dict()])
            return failed, _step("assets in the manifest", [e.to_dict()])
        stable = cast(JsonObject, report["stable"])
        problems.extend(cast(list[JsonObject], stable["errors"]))
        for name in pipelines.names(config.pipelines_dir):
            doc = pipelines.load(config.pipelines_dir, name)
            values = pipelines.check_params(doc)
            try:
                request: JsonObject = {
                    "spec": pipelines.inline(config.pipelines_dir, doc)
                }
                if values is not None:
                    request["params"] = values
                checked = cast(JsonObject, await kernel.request("validate", request))
            except KernelError as e:
                problems.append({"pipeline": name, **e.to_dict()})
                continue
            for issue in cast(list[JsonObject], checked["issues"]):
                if issue.get("severity") == "error":
                    problems.append({"pipeline": name, **issue})
        assets = await _gate_assets(kernel, refs)
    return _step("load and validate pipelines", problems), assets


async def _gate_typecheck(config: Config) -> JsonObject:
    try:
        diagnostics = await typecheck(
            replace(config, typecheck="strict"), config.stable_dir
        )
    except TypecheckError as e:
        return _step("pyright strict", [{"message": str(e)}])
    return _step(
        "pyright strict", [d for d in diagnostics if d.get("severity") == "error"]
    )


def _gate_no_draft_imports(config: Config) -> JsonObject:
    problems: list[JsonObject] = []
    for module in drafts.module_names(config.stable_dir):
        path = config.stable_dir / f"{module}.py"
        for found in drafts.draft_imports(path.read_text(), config.draft_dir.name):
            problems.append({"file": f"{config.package}/{module}.py", **found})
    return _step("no draft imports", problems)


async def _gate_examples(config: Config) -> JsonObject:
    if not (config.root / EXAMPLES_TEST).is_file():
        return _step(
            "examples", [{"message": f"{EXAMPLES_TEST} is missing in the base"}]
        )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        EXAMPLES_TEST,
    ]
    try:
        with anyio.fail_after(config.timeouts.run):
            done = await anyio.run_process(command, cwd=config.root, check=False)
    except TimeoutError:
        return _step("examples", [{"message": "the examples timed out"}])
    if done.returncode not in (0, 5):  # 5: no examples collected
        return _step("examples", [{"output": _text(done.stdout + done.stderr)}])
    return _step("examples", [])


# ─── Publish ───


async def _pr(config: Config, tree: Path, branch: str, title: str, body: str) -> str:
    github = config.github
    assert github is not None
    body_file = tree.parent / "pr-body.md"
    body_file.write_text(body)
    command = [
        "gh", "pr", "create", "--repo", github.repo, "--base", github.base,
        "--head", branch, "--title", title, "--body-file", str(body_file),
    ]  # fmt: skip
    done = await anyio.run_process(command, cwd=tree, check=False)
    if done.returncode != 0:
        raise CrystallizeError(
            "publish",
            f"gh pr create failed; the branch {branch} is pushed",
            {"output": _text(done.stderr or done.stdout)},
        )
    return _text(done.stdout).strip().splitlines()[-1]


def _body(
    config: Config,
    summary: str,
    cards: list[JsonObject],
    direct: list[str],
    modules: list[str],
    gate: list[JsonObject],
) -> str:
    lines = [summary, "", "## Operators", ""]
    for card in cards:
        ref = f"{card['name']}@{card['version']}"
        lines.append(f"- `{ref}` `{card.get('sig')}`: {card.get('description') or ''}")
    added = [m for m in modules if m not in direct]
    lines += ["", f"Modules moved to `{config.package}`: " + ", ".join(modules)]
    if added:
        lines.append(
            "Added as relative imports of the moved modules: " + ", ".join(added)
        )
    lines += ["", "## Gate", ""]
    for step in gate:
        lines.append(f"- {'✅' if step['ok'] else '❌'} {step['step']}")
    assets = [
        cast(JsonObject, a)
        for step in gate
        for a in cast(list[JsonValue], step.get("assets", []))
    ]
    if assets:
        lines += ["", "## Assets", "", "| asset | size | used by |", "|---|---|---|"]
        for a in assets:
            size = a.get("size")
            shown = f"{cast(int, size) / 1e9:.2f} GB" if isinstance(size, int) else "?"
            used_by = ", ".join(f"`{r}`" for r in cast(list[str], a["used_by"]))
            lines.append(f"| `{a['id']}` | {shown} | {used_by} |")
    lines += [
        "",
        "## Runs (traces)",
        "",
        "| operator | runs | ok | last run |",
        "|---|---|---|---|",
    ]
    stats, recent = _trace_stats(config.runs_dir)
    for card in cards:
        ref = f"{card['name']}@{card['version']}"
        runs, ok, last = stats.get(ref, (0, 0, "—"))
        lines.append(f"| `{ref}` | {runs} | {ok} | {last} |")
    lines += ["", "## Recent successful runs", ""]
    for card in cards:
        ref = f"{card['name']}@{card['version']}"
        lines.append(f"`{ref}`:")
        runs = recent.get(ref, [])
        if not runs:
            lines.append("- none recorded")
        current = card.get("fingerprint")
        lines.extend(f"- {_run_line(trace, ref, current)}" for trace in runs)
    return "\n".join(lines) + "\n"


def _trace_stats(
    runs_dir: Path,
) -> tuple[dict[str, tuple[int, int, str]], dict[str, list[JsonObject]]]:
    """Per ref: runs, successful runs and the last run's timestamp; and the
    last ``RECENT_RUNS`` successful traces, newest first."""
    stats: dict[str, tuple[int, int, str]] = {}
    recent: dict[str, list[JsonObject]] = {}
    for path in sorted(runs_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                trace = cast(JsonObject, json.loads(line))
            except ValueError:
                continue
            ok = trace.get("ok") is True
            ts = str(trace.get("ts", ""))
            for ref in cast(list[str], trace.get("refs") or []):
                runs, good, last = stats.get(ref, (0, 0, ""))
                stats[ref] = (runs + 1, good + int(ok), max(last, ts))
                if ok:
                    recent.setdefault(ref, []).append(trace)
    for traces in recent.values():
        traces.sort(key=lambda t: str(t.get("ts", "")), reverse=True)
        del traces[RECENT_RUNS:]
    return stats, recent


def _run_line(trace: JsonObject, ref: str, current: JsonValue = None) -> str:
    """``2026-10-18 09:12 · split_scenes · input `/v/02.mp4` · 12 output(s)
    · 0 item error(s) · 41.2 s``"""
    parts = [str(trace.get("ts", "")).replace("T", " ")[:16]]
    pipeline = trace.get("pipeline")
    if isinstance(pipeline, str):
        parts.append(pipeline)
    shown = trace.get("input")
    text = shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)
    if len(text) > RUN_INPUT_MAX:
        text = text[: RUN_INPUT_MAX - 1] + "…"
    parts.append(f"input `{text}`")
    params = trace.get("params")
    if isinstance(params, dict) and params:
        parts.append(
            f"params `{json.dumps(params, ensure_ascii=False)[:RUN_INPUT_MAX]}`"
        )
    result = trace.get("result")
    if isinstance(result, dict):
        parts.append(f"{result.get('total')} output(s)")
        parts.append(f"{result.get('errors', 0)} item error(s)")
    duration = trace.get("duration")
    if isinstance(duration, int | float):
        parts.append(f"{duration:.1f} s")
    drafts = trace.get("drafts")
    used = drafts.get(ref) if isinstance(drafts, dict) else None
    if current is not None and used is not None and used != current:
        parts.append("(earlier draft code)")
    return " · ".join(parts)


def _text(output: bytes) -> str:
    return output.decode(errors="replace")[-OUTPUT_TAIL:]
