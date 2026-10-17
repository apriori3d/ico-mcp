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

from ico_mcp.config import Config
from ico_mcp.drafts import draft_imports
from ico_mcp.kernel_client import KernelClient
from ico_mcp.protocol import JsonObject, JsonValue, KernelError
from ico_mcp.typecheck import TypecheckError, typecheck

Stage = Literal["write", "typecheck", "load", "register", "examples", "types"]


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
    code: str,
    check_types: bool = True,
    examples: Sequence[Example] = (),
) -> JsonObject:
    """Run all stages; returns the cards of the module on success.

    Raises:
        ProposalError: At the first failing stage.
    """
    path, name, warnings = await _load(config, kernel, module, code, check_types)
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
    return {
        "module": name,
        "path": str(path.relative_to(config.root)),
        "cards": cards_out,
        "warnings": warnings_out,
        "examples": passed,
    }


async def propose_types(
    config: Config,
    kernel: KernelClient,
    *,
    module: str,
    code: str,
    check_types: bool = True,
) -> JsonObject:
    """Run all stages of a types-only module; returns its type cards.

    Raises:
        ProposalError: At the first failing stage.
    """
    path, name, warnings = await _load(config, kernel, module, code, check_types)
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
    return {
        "module": name,
        "path": str(path.relative_to(config.root)),
        "types": types,
        "warnings": warnings_out,
    }


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
            failures.append({**failure, "error": e.to_dict()})
            continue
        outputs = cast(list[JsonValue], result["outputs"])
        if result["total"] != 1 or outputs[0] != example.output:
            failures.append(
                {**failure, "got": outputs if result["total"] != 1 else outputs[0]}
            )
            continue
        passed[example.ref] = passed.get(example.ref, 0) + 1
    if failures:
        raise ProposalError(
            "examples",
            f"{len(failures)} of {len(examples)} example(s) failed",
            failures,
        )
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


async def _request(
    kernel: KernelClient, stage: Stage, method: str, params: JsonObject
) -> JsonValue:
    try:
        return await kernel.request(method, params)
    except KernelError as e:
        raise ProposalError(stage, e.message, [e.to_dict()]) from None
