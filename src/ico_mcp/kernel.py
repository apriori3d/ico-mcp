"""The kernel: a child process that imports domain operators and runs specs.

Started by :class:`ico_mcp.kernel_client.KernelClient` as
``python -m ico_mcp.kernel <root>``. It reads protocol requests from stdin
and writes responses and notifications to the original stdout. File
descriptor 1 is redirected to stderr first, so output of operators
(``print``, child processes) never corrupts the protocol.

Notifications (no ``id``), sent while a ``run`` request is in progress::

    {"method": "plan", "params": {"request": <id>, "plan": {...}}}
    {"method": "progress", "params": {"request": <id>, "snapshot": {...}}}

``plan`` is the ``plan_view`` of the built spec (the console plan as rows),
sent first, unless the spec does not build. ``snapshot`` is a
``JsonProgressTool`` snapshot; every progress node is first reported with
``done = 0``, in flow order.
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

from ico import (
    IcoOperator,
    JsonProgressTool,
    SignatureIssue,
    plan_view,
    validate_signatures,
)
from ico.core.signature_utils import format_ico_type
from ico.registry import RegistryEntry, default_registry, load_operators
from ico.spec import NodeSpec, PipelineSpec, SpecError, build, run, spec_params
from ico.spec.run import DEFAULT_MAX_ITEMS

from ico_mcp import environment
from ico_mcp.config import Config, load_config
from ico_mcp.domain_types import DomainType, collect
from ico_mcp.protocol import JsonObject, JsonValue, KernelError, decode, encode
from ico_mcp.type_names import TypeIndex

DEFAULT_SEARCH_LIMIT = 20
TRACEBACK_LIMIT = 4000

_ANY = re.compile(r"\bAny\b")

Handler = Callable[[JsonObject], JsonValue]
Notify = Callable[[JsonObject], None]
ProgressSnapshot = dict[str, str | float | None]  # see JsonProgressTool


class Kernel:
    """Request handlers over the default registry.

    Args:
        config: The repository configuration.
        notify: Sends a notification; may be called from other threads.
    """

    def __init__(self, config: Config, notify: Notify) -> None:
        self.config = config
        self.notify = notify
        self.request_id: JsonValue = None
        self.types = TypeIndex((), "")
        self.domain_types: list[DomainType] = []
        self.domain_index = TypeIndex((), "")
        self.handlers: dict[str, Handler] = {
            "load": self.load,
            "search": self.search,
            "card": self.card,
            "module_cards": self.module_cards,
            "types": self.list_types,
            "type": self.type_card,
            "module_types": self.module_types,
            "environment": self.environment,
            "validate": self.validate,
            "run": self.run,
        }

    def handle(self, line: bytes | str) -> JsonObject:
        """Answer one request line."""
        request_id: JsonValue = None
        try:
            request = decode(line)
            request_id = self.request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            handler = self.handlers.get(method) if isinstance(method, str) else None
            if handler is None:
                raise KernelError("bad_request", f"unknown method {method!r}")
            if not isinstance(params, dict):
                raise KernelError("bad_request", "params must be an object")
            return {"id": request_id, "result": handler(params)}
        except KernelError as e:
            return {"id": request_id, "error": e.to_dict()}
        except Exception as e:
            error = KernelError("internal", f"{type(e).__name__}: {e}", _traceback(e))
            return {"id": request_id, "error": error.to_dict()}

    # ─── Methods ───

    def load(self, params: JsonObject) -> JsonValue:
        """Load the stable and the draft package."""
        stable = load_operators(self.config.stable_dir, status="stable")
        draft = load_operators(self.config.draft_dir, status="draft")
        packages = {
            self.config.stable_dir.name: "stable",
            self.config.draft_dir.name: "draft",
        }
        operators = {entry.cls for entry in default_registry if entry.cls is not None}
        self.domain_types = collect(packages, operators)
        domain = [t.cls for t in self.domain_types]
        signatures = [
            tp
            for entry in default_registry
            for tp in (entry.signature.i, entry.signature.o)
        ]
        self.types = TypeIndex(
            [*signatures, *domain],
            "not a domain type and in no registered signature",
        )
        self.domain_index = TypeIndex(domain, "not a domain type (see list_types)")
        return {"stable": stable.to_dict(), "draft": draft.to_dict()}

    def search(self, params: JsonObject) -> JsonValue:
        text = _optional(params, "text", str)
        input_type = _optional(params, "input_type", str)
        output_type = _optional(params, "output_type", str)
        tags = _strings(params, "tags")
        limit = _positive_int(params, "limit", DEFAULT_SEARCH_LIMIT)
        entries = default_registry.search(
            input=self.types.resolve(input_type) if input_type else None,
            output=self.types.resolve(output_type) if output_type else None,
            tags=tags,
            text=text,
        )
        cards: list[JsonValue] = []
        cards.extend(entry.card() for entry in entries[:limit])
        return {"cards": cards, "total": len(entries)}

    def card(self, params: JsonObject) -> JsonValue:
        ref = _required(params, "ref", str)
        try:
            entry = default_registry.get(ref)
        except KeyError:
            raise KernelError(
                "not_found", f"operator {ref!r} is not registered"
            ) from None
        card: JsonObject = {
            **entry.card(),
            "module": entry.module,
            "source": _source(entry),
        }
        if entry.meta.status == "draft" and entry.module is not None:
            card["module_source"] = _module_source(entry.module)
        needed = environment.bin_names(entry.meta.caps)
        if needed:
            resolved: JsonObject = {}
            resolved.update(environment.bins(needed))
            card["bins"] = resolved
        return card

    def module_cards(self, params: JsonObject) -> JsonValue:
        """Cards of the operators a module registers, and the refs whose
        signature has ``Any`` (as formatted in ``sig``)."""
        module = _required(params, "module", str)
        entries = [entry for entry in default_registry if entry.module == module]
        cards: list[JsonValue] = []
        cards.extend(entry.card() for entry in entries)
        any_refs: list[JsonValue] = []
        any_refs.extend(
            entry.ref for entry in entries if _ANY.search(entry.signature.format())
        )
        return {"cards": cards, "any": any_refs}

    def list_types(self, params: JsonObject) -> JsonValue:
        """Compact cards of all domain types."""
        cards: list[JsonValue] = []
        cards.extend(t.card() for t in self.domain_types)
        return {"types": cards}

    def type_card(self, params: JsonObject) -> JsonValue:
        """The full card of a domain type, by name as for ``search``."""
        cls = self.domain_index.resolve(_required(params, "name", str))
        return next(t for t in self.domain_types if t.cls is cls).card(full=True)

    def module_types(self, params: JsonObject) -> JsonValue:
        """Full cards of the domain types a module defines, and the refs of
        the operators it registers."""
        module = _required(params, "module", str)
        cards: list[JsonValue] = []
        cards.extend(t.card(full=True) for t in self.domain_types if t.module == module)
        refs: list[JsonValue] = []
        refs.extend(entry.ref for entry in default_registry if entry.module == module)
        return {"types": cards, "operators": refs}

    def environment(self, params: JsonObject) -> JsonValue:
        """Python, PATH, allowed variables, packages, duplicates and the
        binaries of ``bin:`` capabilities, as operators see them."""
        match = _optional(params, "match", str)
        needed = [
            name
            for entry in default_registry
            for name in environment.bin_names(entry.meta.caps)
        ]
        return environment.report(self.config, needed, match)

    def validate(self, params: JsonObject) -> JsonValue:
        """Signature issues of a spec built with ``params``, and the pipeline
        parameters it declares (also when values are missing)."""
        spec = _spec(params)
        values = _values(params)
        declared: JsonValue = None
        issues: list[JsonValue] = []
        issues.extend(_missing_binaries(_required(params, "spec", dict)))
        try:
            found: list[JsonValue] = []
            found.extend(p.to_dict() for p in spec_params(spec))
            declared = found
            flow = build(spec, params=values)
        except SpecError as e:
            issues.append({"severity": "error", **_spec_error(e)})
            return {"issues": issues, "params": declared}
        issues.extend(_issue(issue) for issue in validate_signatures(flow))
        return {"issues": issues, "params": declared}

    def run(self, params: JsonObject) -> JsonValue:
        spec = _spec(params)
        max_items = _positive_int(params, "max_items", DEFAULT_MAX_ITEMS)
        values = _values(params)
        self._send_plan(spec, values)
        progress = JsonProgressTool(self._progress(self.request_id))
        try:
            result = run(
                spec,
                params.get("input"),
                max_items=max_items,
                tools=[progress],
                params=values,
            )
        except SpecError as e:
            raise KernelError("spec_error", str(e), _spec_error(e)) from None
        except Exception as e:
            raise KernelError(
                "run_error", f"{type(e).__name__}: {e}", _traceback(e)
            ) from None
        return result.to_dict()

    def _send_plan(
        self, spec: PipelineSpec | NodeSpec, values: JsonObject | None
    ) -> None:
        """Notify the plan of a spec with the run's parameter values; a spec
        that does not build fails in ``run`` with a located error instead."""
        try:
            plan = cast(JsonObject, plan_view(build(spec, params=values)))
        except Exception:
            return
        params: JsonObject = {"request": self.request_id, "plan": plan}
        self.notify({"method": "plan", "params": params})

    def _progress(self, request_id: JsonValue) -> Callable[[ProgressSnapshot], None]:
        def send(snapshot: ProgressSnapshot) -> None:
            params: JsonObject = {"request": request_id, "snapshot": dict(snapshot)}
            self.notify({"method": "progress", "params": params})

        return send


# ─── Params ───


def _required(params: JsonObject, key: str, kind: type) -> Any:
    value = params.get(key)
    if not isinstance(value, kind):
        raise KernelError("bad_request", f"'{key}' must be a {kind.__name__}")
    return value


def _optional(params: JsonObject, key: str, kind: type) -> Any:
    return _required(params, key, kind) if params.get(key) is not None else None


def _strings(params: JsonObject, key: str) -> list[str]:
    values = params.get(key) or []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise KernelError("bad_request", f"'{key}' must be a list of strings")
    return cast(list[str], values)


def _positive_int(params: JsonObject, key: str, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KernelError("bad_request", f"'{key}' must be a positive integer")
    return value


def _values(params: JsonObject) -> JsonObject | None:
    """The values of the pipeline parameters (``params.params``), if given."""
    return cast(JsonObject | None, _optional(params, "params", dict))


def _spec(params: JsonObject) -> PipelineSpec | NodeSpec:
    """A spec document (``{"version", "root"}``) or a single node spec."""
    data = _required(params, "spec", dict)
    try:
        if "root" in data:
            return PipelineSpec.from_dict(data)
        return NodeSpec.from_dict(data)
    except SpecError as e:
        raise KernelError("spec_error", str(e), _spec_error(e)) from None


# ─── Results ───


def _spec_error(e: SpecError) -> JsonObject:
    return {
        "message": str(e),
        "reason": e.reason,
        "path": str(e.path) if e.path is not None else None,
        "expected": e.expected,
        "got": e.got,
        "refs": list(e.refs) if e.refs is not None else None,
    }


def _issue(issue: SignatureIssue) -> JsonObject:
    return {
        "severity": "error" if issue.is_error else "warning",
        "kind": issue.kind,
        "message": issue.message,
        "path": str(issue.path) if issue.path is not None else None,
        "expected": format_ico_type(issue.input) if issue.input is not None else None,
        "got": format_ico_type(issue.output) if issue.output is not None else None,
        "refs": [issue.left, issue.right],
    }


def _missing_binaries(spec: JsonObject) -> list[JsonValue]:
    """Warnings for ``bin:`` capabilities of the spec's operators that are not
    on the kernel's PATH (an operator may still get a full path as a param)."""
    root = spec.get("root", spec)
    found: list[JsonValue] = []

    def walk(node: JsonValue, path: tuple[int, ...]) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("op")
        if isinstance(ref, str):
            try:
                caps = default_registry.get(ref).meta.caps
            except KeyError:
                caps = frozenset[str]()  # an unknown operator fails in build
            for name, where in environment.bins(environment.bin_names(caps)).items():
                if where is None:
                    found.append(
                        {
                            "severity": "warning",
                            "kind": "missing_binary",
                            "message": f"{ref} needs `{name}` (cap bin:{name}), "
                            "not on the kernel PATH",
                            "path": ".".join(str(i) for i in path),
                            "refs": [ref],
                        }
                    )
        children = node.get("children")
        if isinstance(children, list):
            for index, child in enumerate(children):
                walk(child, (*path, index))

    walk(root, ())
    return found


def _module_source(module: str) -> str | None:
    """The text of an imported module's file, for editing a draft."""
    loaded = sys.modules.get(module)
    file = getattr(loaded, "__file__", None)
    if not isinstance(file, str):
        return None
    try:
        return Path(file).read_text()
    except OSError:
        return None


def _source(entry: RegistryEntry) -> str | None:
    target: object = entry.cls
    if target is None and type(entry.node) is IcoOperator:
        target = cast(IcoOperator[Any, Any], entry.node).fn
    if target is None:
        return None
    try:
        return inspect.getsource(cast(Any, target))
    except (OSError, TypeError):
        return None


def _traceback(e: BaseException) -> JsonObject:
    text = "".join(traceback.format_exception(e))
    return {"traceback": text[-TRACEBACK_LIMIT:]}


# ─── Process ───


def serve(root: Path, stdin: IO[str], stdout: IO[str]) -> None:
    """Answer requests from ``stdin`` until it closes."""
    lock = threading.Lock()

    def write(message: JsonObject) -> None:
        with lock:
            stdout.write(encode(message).decode())
            stdout.flush()

    kernel = Kernel(load_config(root), write)
    for line in stdin:
        if line.strip():
            write(kernel.handle(line))


def main() -> None:
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    serve(Path(sys.argv[1]), sys.stdin, protocol_out)


if __name__ == "__main__":
    main()
