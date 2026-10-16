"""The kernel: a child process that imports domain operators and runs specs.

Started by :class:`ico_mcp.kernel_client.KernelClient` as
``python -m ico_mcp.kernel <root>``. It reads protocol requests from stdin
and writes responses to the original stdout. File descriptor 1 is
redirected to stderr first, so output of operators (``print``, child
processes) never corrupts the protocol.
"""

from __future__ import annotations

import inspect
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

from ico import IcoOperator, SignatureIssue, validate_signatures
from ico.core.signature_utils import format_ico_type  # pyright: ignore[reportMissingTypeStubs]  # ico.core is a namespace package
from ico.registry import RegistryEntry, default_registry, load_operators
from ico.spec import NodeSpec, PipelineSpec, SpecError, build, run
from ico.spec.run import DEFAULT_MAX_ITEMS

from ico_mcp.config import Config, load_config
from ico_mcp.protocol import JsonObject, JsonValue, KernelError, decode, encode
from ico_mcp.type_names import TypeIndex

DEFAULT_SEARCH_LIMIT = 20
TRACEBACK_LIMIT = 4000

Handler = Callable[[JsonObject], JsonValue]


class Kernel:
    """Request handlers over the default registry."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.types = TypeIndex(())
        self.handlers: dict[str, Handler] = {
            "load": self.load,
            "search": self.search,
            "card": self.card,
            "validate": self.validate,
            "run": self.run,
        }

    def handle(self, line: bytes | str) -> JsonObject:
        """Answer one request line."""
        request_id: JsonValue = None
        try:
            request = decode(line)
            request_id = request.get("id")
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
        self.types = TypeIndex(entry.signature for entry in default_registry)
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
        return {**entry.card(), "module": entry.module, "source": _source(entry)}

    def validate(self, params: JsonObject) -> JsonValue:
        try:
            flow = build(_spec(params))
        except SpecError as e:
            issues: list[JsonValue] = [{"severity": "error", **_spec_error(e)}]
            return {"issues": issues}
        issues = []
        issues.extend(_issue(issue) for issue in validate_signatures(flow))
        return {"issues": issues}

    def run(self, params: JsonObject) -> JsonValue:
        spec = _spec(params)
        max_items = _positive_int(params, "max_items", DEFAULT_MAX_ITEMS)
        try:
            result = run(spec, params.get("input"), max_items=max_items)
        except SpecError as e:
            raise KernelError("spec_error", str(e), _spec_error(e)) from None
        except Exception as e:
            raise KernelError(
                "run_error", f"{type(e).__name__}: {e}", _traceback(e)
            ) from None
        return result.to_dict()


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
    kernel = Kernel(load_config(root))
    for line in stdin:
        if line.strip():
            stdout.write(encode(kernel.handle(line)).decode())
            stdout.flush()


def main() -> None:
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    serve(Path(sys.argv[1]), sys.stdin, protocol_out)


if __name__ == "__main__":
    main()
