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
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import anyio

from ico_mcp import drafts, pipelines
from ico_mcp.config import Config, load_config
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError
from ico_mcp.typecheck import TypecheckError, typecheck

Stage = Literal["check", "git", "gate", "publish"]

EXAMPLES_TEST = "tests/test_domain_examples.py"
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
) -> JsonObject:
    """Open a pull request that makes ``refs`` stable.

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
    await _check_gh(config)
    cards = await _draft_cards(kernel, refs)
    for name in pipeline_names:
        if not pipelines.exists(config.pipelines_dir, name):
            raise CrystallizeError("check", f"no saved pipeline {name!r}")

    draft_package = config.draft_dir.name
    direct = sorted({str(c["module"]).removeprefix(f"{draft_package}.") for c in cards})
    modules = drafts.closure(config.draft_dir, direct)
    module_refs = await _module_refs(kernel, draft_package, modules)

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
            gate = await _gate(load_config(tree))
            if not all(step["ok"] for step in gate):
                raise CrystallizeError(
                    "gate",
                    "the gate failed; nothing was pushed",
                    {"gate": cast(list[JsonValue], gate)},
                )
            await _git(tree, "add", "-A")
            await _git(tree, "commit", "-q", "-m", title, "-m", summary)
            await _git(tree, "push", "-q", "-u", "origin", branch)
            pushed = True
            body = _body(config, summary, cards, direct, modules, gate)
            url = await _pr(config, tree, branch, title, body)
        finally:
            await _git(config.root, "worktree", "remove", "--force", str(tree))
            if not pushed:
                await _git(config.root, "branch", "-q", "-D", branch, check=False)
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
    target.examples_dir.mkdir(exist_ok=True)
    for ref in refs:
        example = config.examples_dir / f"{ref}.json"
        if example.is_file():
            shutil.copy2(example, target.examples_dir / example.name)
    target.pipelines_dir.mkdir(exist_ok=True)
    for name in pipeline_names:
        source = config.pipelines_dir / f"{name}.spec.json"
        shutil.copy2(source, target.pipelines_dir / source.name)


# ─── Gate ───


async def _gate(config: Config) -> list[JsonObject]:
    """Run every gate step on the worktree; each is ``{step, ok, diagnostics}``."""
    return [
        await _gate_load_and_pipelines(config),
        await _gate_typecheck(config),
        _gate_no_draft_imports(config),
        await _gate_examples(config),
    ]


def _step(step: str, diagnostics: list[JsonObject]) -> JsonObject:
    out: list[JsonValue] = []
    out.extend(diagnostics)
    return {"step": step, "ok": not diagnostics, "diagnostics": out}


async def _gate_load_and_pipelines(config: Config) -> JsonObject:
    """The stable package loads and every saved pipeline validates."""
    problems: list[JsonObject] = []
    async with KernelClient(config) as kernel:
        try:
            report = cast(JsonObject, await kernel.start())
        except KernelError as e:
            return _step("load and validate pipelines", [e.to_dict()])
        stable = cast(JsonObject, report["stable"])
        problems.extend(cast(list[JsonObject], stable["errors"]))
        for name in pipelines.names(config.pipelines_dir):
            doc = pipelines.load(config.pipelines_dir, name)
            request: JsonObject = {"spec": doc}
            values = pipelines.check_params(doc)
            if values is not None:
                request["params"] = values
            try:
                checked = cast(JsonObject, await kernel.request("validate", request))
            except KernelError as e:
                problems.append({"pipeline": name, **e.to_dict()})
                continue
            for issue in cast(list[JsonObject], checked["issues"]):
                if issue.get("severity") == "error":
                    problems.append({"pipeline": name, **issue})
    return _step("load and validate pipelines", problems)


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
    lines += [
        "",
        "## Runs (traces)",
        "",
        "| operator | runs | ok | last run |",
        "|---|---|---|---|",
    ]
    stats = _trace_stats(config.runs_dir)
    for card in cards:
        ref = f"{card['name']}@{card['version']}"
        runs, ok, last = stats.get(ref, (0, 0, "—"))
        lines.append(f"| `{ref}` | {runs} | {ok} | {last} |")
    return "\n".join(lines) + "\n"


def _trace_stats(runs_dir: Path) -> dict[str, tuple[int, int, str]]:
    """Per ref: runs, successful runs and the last run's timestamp."""
    stats: dict[str, tuple[int, int, str]] = {}
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
    return stats


def _text(output: bytes) -> str:
    return output.decode(errors="replace")[-OUTPUT_TAIL:]
