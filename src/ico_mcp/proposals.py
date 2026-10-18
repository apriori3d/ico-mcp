"""``propose_operator`` / ``propose_types``: add or replace a draft module
in stages.

Operators: write → typecheck → load → register → examples. Types: write →
typecheck → load → types. The first failing stage stops the proposal with a
:class:`ProposalError`. The file stays in the draft package so the agent can
fix it; whatever of it loads stays loaded.

Draft modules import each other relatively (``from .frames import Frame``)
and the stable package absolutely, so that crystallization moves files
without rewriting them; ``write`` rejects absolute imports of the draft
package.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ico_mcp import pipelines, traces
from ico_mcp.config import Config
from ico_mcp.drafts import draft_imports
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError
from ico_mcp.typecheck import TypecheckError, typecheck

PIPELINES_HINT = (
    "a saved pipeline no longer validates: fix it (save_pipeline with "
    "replace=true), or register a new operator version (@2) in a new draft "
    "module if the old behaviour is still needed"
)

NOT_FOR_EXAMPLES = (
    "examples are for operators with JSON input and JSON output that need no "
    "files or environment; an operator on domain data (e.g. returning a "
    "dataclass, or reading a video) needs no examples: check it with a "
    "pipeline run instead"
)

Stage = Literal["write", "typecheck", "load", "register", "examples", "types"]


@dataclass(frozen=True, slots=True)
class Edit:
    """Replace the only occurrence of ``old`` in a draft module by ``new``."""

    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Example:
    """``ref`` applied to ``input`` must return ``output``."""

    ref: str
    input: JsonValue
    output: JsonValue


class ProposalError(Exception):
    """A proposal stage failed."""

    def __init__(
        self, stage: Stage, message: str, diagnostics: Sequence[JsonObject] = ()
    ) -> None:
        super().__init__(message)
        self.stage: Stage = stage
        self.message = message
        self.diagnostics = list(diagnostics)

    def to_dict(self) -> JsonObject:
        diagnostics: list[JsonValue] = []
        diagnostics.extend(self.diagnostics)
        return {
            "code": "proposal_failed",
            "stage": self.stage,
            "message": self.message,
            "diagnostics": diagnostics,
        }


async def propose(
    config: Config,
    kernel: KernelClient,
    *,
    module: str,
    code: str | None = None,
    edits: Sequence[Edit] | None = None,
    check_types: bool = True,
    examples: Sequence[Example] = (),
) -> JsonObject:
    """Run all stages; returns the cards of the module on success.

    The module is ``code``, or the current draft with ``edits`` applied.

    Raises:
        ProposalError: At the first failing stage.
    """
    text = module_code(config, module, code, edits)
    before = await _module_cards(kernel, f"{config.draft_dir.name}.{module}")
    path, name, warnings = await _load(config, kernel, module, text, check_types)
    found = cast(
        JsonObject, await _request(kernel, "register", "module_cards", {"module": name})
    )
    cards = cast(list[JsonObject], found["cards"])
    if not cards:
        raise ProposalError(
            "register", f"{module} registers no operator; use @register"
        )
    any_refs = cast(list[str], found["any"])
    if any_refs:
        raise ProposalError(
            "register",
            "signatures must not be inferred as Any; annotate inputs and outputs",
            [{"ref": ref} for ref in any_refs],
        )

    refs = {f"{card['name']}@{card['version']}" for card in cards}
    passed = await _check_examples(kernel, refs, examples)
    _save_examples(config, examples)

    cards_out: list[JsonValue] = []
    cards_out.extend(cards)
    warnings_out: list[JsonValue] = []
    warnings_out.extend(warnings)
    changes, affected = await _changes(config, kernel, before, cards)
    result: JsonObject = {
        "module": name,
        "path": str(path.relative_to(config.root)),
        "cards": cards_out,
        "warnings": warnings_out,
        "examples": passed,
        "changes": changes,
        "pipelines": affected,
    }
    if any(not cast(JsonObject, p)["ok"] for p in affected):
        result["hint"] = PIPELINES_HINT
    if edits is not None:
        result["edits"] = len(edits)
    return result


async def propose_types(
    config: Config,
    kernel: KernelClient,
    *,
    module: str,
    code: str | None = None,
    edits: Sequence[Edit] | None = None,
    check_types: bool = True,
) -> JsonObject:
    """Run all stages of a types-only module; returns its type cards.

    The module is ``code``, or the current draft with ``edits`` applied.

    Raises:
        ProposalError: At the first failing stage.
    """
    text = module_code(config, module, code, edits)
    path, name, warnings = await _load(config, kernel, module, text, check_types)
    found = cast(
        JsonObject, await _request(kernel, "types", "module_types", {"module": name})
    )
    refs = cast(list[str], found["operators"])
    if refs:
        raise ProposalError(
            "types",
            f"{module} registers operators; put them in a module of their own "
            "with propose_operator",
            [{"ref": ref} for ref in refs],
        )
    types = cast(list[JsonValue], found["types"])
    if not types:
        raise ProposalError("types", f"{module} defines no public class")
    warnings_out: list[JsonValue] = []
    warnings_out.extend(warnings)
    result: JsonObject = {
        "module": name,
        "path": str(path.relative_to(config.root)),
        "types": types,
        "warnings": warnings_out,
    }
    if edits is not None:
        result["edits"] = len(edits)
    return result


def module_code(
    config: Config, module: str, code: str | None, edits: Sequence[Edit] | None
) -> str:
    """The new text of a draft module: ``code``, or the current draft with
    ``edits`` applied in order (stage ``write``; nothing is written here).

    Raises:
        ProposalError: Unless exactly one of ``code`` and ``edits`` is given,
            the module has no draft to edit, or an edit's ``old`` is empty or
            does not occur exactly once at the time it is applied.
    """
    if (code is None) == (edits is None):
        raise ProposalError("write", "give exactly one of code and edits")
    if code is not None:
        return code
    assert edits is not None
    _check_name(config, module)
    path = config.draft_dir / f"{module}.py"
    if not path.is_file():
        raise ProposalError(
            "write", f"no draft module {module!r} to edit; send its code instead"
        )
    text = path.read_text()
    for index, edit in enumerate(edits):
        count = text.count(edit.old) if edit.old else 0
        if not edit.old or count != 1:
            reason = (
                "empty old text"
                if not edit.old
                else "old text not found"
                if count == 0
                else f"old text found {count} times; add surrounding lines to make it unique"
            )
            raise ProposalError(
                "write",
                f"edit {index}: {reason}; nothing was changed",
                [{"edit": index, "found": count, "old": edit.old[:200]}],
            )
        text = text.replace(edit.old, edit.new, 1)
    return text


def _check_name(config: Config, module: str) -> None:
    if not module.isidentifier() or module.startswith("_"):
        raise ProposalError(
            "write", f"invalid module name {module!r}: use a public Python identifier"
        )
    if (config.stable_dir / f"{module}.py").exists():
        raise ProposalError(
            "write",
            f"{config.package}.{module} is a stable module; choose another name "
            "or register a new version of its operators",
        )


async def _load(
    config: Config, kernel: KernelClient, module: str, code: str, check_types: bool
) -> tuple[Path, str, list[JsonObject]]:
    """Stages write, typecheck and load: the file, the module name and the
    pyright warnings."""
    path = _write(config, module, code)

    warnings: list[JsonObject] = []
    if check_types:
        try:
            diagnostics = await typecheck(config, path)
        except TypecheckError as e:
            raise ProposalError("typecheck", str(e)) from None
        errors = [d for d in diagnostics if d.get("severity") == "error"]
        if errors:
            raise ProposalError("typecheck", f"pyright: {len(errors)} error(s)", errors)
        warnings = [d for d in diagnostics if d.get("severity") != "error"]

    try:
        report = cast(JsonObject, await kernel.start())
    except KernelError as e:
        raise ProposalError("load", e.message, [e.to_dict()]) from None
    draft = cast(JsonObject, report["draft"])
    load_errors = [
        error
        for error in cast(list[JsonObject], draft["errors"])
        if Path(str(error.get("file"))).resolve() == path
    ]
    if load_errors:
        raise ProposalError("load", f"{module} failed to import", load_errors)
    return path, f"{config.draft_dir.name}.{module}", warnings


def _write(config: Config, module: str, code: str) -> Path:
    _check_name(config, module)
    absolute = draft_imports(code, config.draft_dir.name)
    if absolute:
        raise ProposalError(
            "write",
            "draft modules import each other relatively; use the `fix` form",
            absolute,
        )
    path = config.draft_dir / f"{module}.py"
    path.write_text(code)
    # The bytecode cache is validated by mtime in whole seconds and size: a
    # same-size rewrite within a second would otherwise load stale code.
    Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
    return path


async def _check_examples(
    kernel: KernelClient, refs: set[str], examples: Sequence[Example]
) -> JsonObject:
    foreign = sorted({e.ref for e in examples} - refs)
    if foreign:
        raise ProposalError(
            "examples",
            f"examples refer to operators not in this module: {foreign}",
            [{"ref": ref} for ref in foreign],
        )
    failures: list[JsonObject] = []
    passed: dict[str, int] = {}
    for example in examples:
        params: JsonObject = {
            "spec": {"kind": "op", "op": example.ref},
            "input": example.input,
        }
        failure: JsonObject = {
            "ref": example.ref,
            "input": example.input,
            "expected": example.output,
        }
        try:
            result = cast(JsonObject, await kernel.request("run", params))
        except KernelError as e:
            failed: JsonObject = {**failure, "error": e.to_dict()}
            if "not JSON-compatible" in e.message:
                failed["hint"] = NOT_FOR_EXAMPLES
            failures.append(failed)
            continue
        outputs = cast(list[JsonValue], result["outputs"])
        if result["total"] != 1 or outputs[0] != example.output:
            failures.append(
                {**failure, "got": outputs if result["total"] != 1 else outputs[0]}
            )
            continue
        passed[example.ref] = passed.get(example.ref, 0) + 1
    if failures:
        message = f"{len(failures)} of {len(examples)} example(s) failed"
        if any("hint" in f for f in failures):
            message += f"; {NOT_FOR_EXAMPLES}"
        raise ProposalError("examples", message, failures)
    out: JsonObject = {}
    out.update(passed)
    return out


def _save_examples(config: Config, examples: Sequence[Example]) -> None:
    by_ref: dict[str, list[JsonValue]] = {}
    for example in examples:
        by_ref.setdefault(example.ref, []).append(
            {"input": example.input, "output": example.output}
        )
    if not by_ref:
        return
    config.examples_dir.mkdir(parents=True, exist_ok=True)
    for ref, items in by_ref.items():
        path = config.examples_dir / f"{ref}.json"
        path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")


async def _module_cards(kernel: KernelClient, module: str) -> dict[str, JsonObject]:
    """The cards a module registers now, by ref (empty if it does not load)."""
    try:
        found = cast(
            JsonObject, await kernel.request("module_cards", {"module": module})
        )
    except KernelError:
        return {}
    cards = cast(list[JsonObject], found["cards"])
    return {f"{c['name']}@{c['version']}": c for c in cards}


async def _changes(
    config: Config,
    kernel: KernelClient,
    before: dict[str, JsonObject],
    after: Sequence[JsonObject],
) -> tuple[JsonObject, list[JsonValue]]:
    """What a replaced module changed for the saved pipelines.

    Returns ``{"changed": [...], "removed": [...]}`` (operators whose
    ``sig`` or ``params`` changed, operators no longer registered, each
    with the saved pipelines that use it) and the validation of those
    pipelines, ``[{"name", "ok", "issues"}]``.
    """
    now = {f"{c['name']}@{c['version']}": c for c in after}
    uses: dict[str, list[str]] = {}
    for pipeline in pipelines.names(config.pipelines_dir):
        doc = pipelines.load(config.pipelines_dir, pipeline)
        for ref in traces.op_refs(doc):
            uses.setdefault(ref, []).append(pipeline)

    changed: list[JsonValue] = []
    removed: list[JsonValue] = []
    touched: set[str] = set()
    for ref, old in sorted(before.items()):
        users: list[JsonValue] = []
        users.extend(uses.get(ref, []))
        new = now.get(ref)
        if new is None:
            removed.append({"ref": ref, "pipelines": users})
        elif (old.get("sig"), old.get("params")) != (new.get("sig"), new.get("params")):
            entry: JsonObject = {"ref": ref, "pipelines": users}
            if old.get("sig") != new.get("sig"):
                entry["sig"] = {"before": old.get("sig"), "after": new.get("sig")}
            if old.get("params") != new.get("params"):
                entry["params"] = {
                    "before": old.get("params"),
                    "after": new.get("params"),
                }
            changed.append(entry)
        else:
            continue
        touched.update(uses.get(ref, []))

    checked: list[JsonValue] = []
    for pipeline in sorted(touched):
        doc = pipelines.load(config.pipelines_dir, pipeline)
        request: JsonObject = {"spec": doc}
        values = pipelines.check_params(doc)
        if values is not None:
            request["params"] = values
        try:
            report = cast(JsonObject, await kernel.request("validate", request))
            issues = [
                i
                for i in cast(list[JsonObject], report["issues"])
                if i.get("severity") == "error"
            ]
        except KernelError as e:
            issues = [e.to_dict()]
        errors: list[JsonValue] = []
        errors.extend(issues)
        checked.append({"name": pipeline, "ok": not issues, "issues": errors})
    return {"changed": changed, "removed": removed}, checked


async def _request(
    kernel: KernelClient, stage: Stage, method: str, params: JsonObject
) -> JsonValue:
    try:
        return await kernel.request(method, params)
    except KernelError as e:
        raise ProposalError(stage, e.message, [e.to_dict()]) from None
