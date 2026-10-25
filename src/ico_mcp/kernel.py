"""The kernel: a child process that imports domain operators and runs specs.

Started by :class:`ico_mcp.kernel_client.KernelClient` as
``python -m ico_mcp.kernel <root>``. It reads protocol requests from stdin
and writes responses and notifications to the original stdout. File
descriptor 1 is redirected to stderr first, so output of operators
(``print``, child processes) never corrupts the protocol.

Notifications (no ``id``), sent while a ``run`` request is in progress::

    {"method": "plan", "params": {"request": <id>, "plan": {...}}}
    {"method": "progress", "params": {"request": <id>, "snapshot": {...}}}
    {"method": "profile", "params": {"request": <id>, "snapshot": {...}}}

A run on a target also bridges ``{"method": "queue", "params": {"request":
<id>, "status": "IN_QUEUE"}}`` when the provider's status of the job
changes (``IN_QUEUE``: waiting for a worker).

``plan`` is the ``plan_view`` of the built spec (the console plan as rows),
sent first, unless the spec does not build. ``progress`` carries a
``JsonProgressTool`` snapshot; every progress node is first reported with
``done = 0``, in flow order. ``profile`` (runs with ``profile: true``)
carries a ``ProfilerTool`` snapshot, keyed like the plan's rows, about once
a second; the final one is the ``profile`` of the run's result.

One-way messages (no ``id``) are read on a thread of their own, so they are
handled while a request runs. ``sample`` asks the run in progress for the
next results of a node (``{"token", "node", "count"}``, ``node`` a plan key
or a label such as ``slow@1``); its answers are notifications of the run::

    {"method": "sample_started", "params": {"request": <id>, "token": <t>,
     "node": "0.1.0", "label": "slow@1"}}
    {"method": "sample_error", "params": {"request": <id>, "token": <t>,
     "error": {"code": ..., "message": ...}}}
    {"method": "sample", "params": {"request": <id>, "sample": {"node",
     "index", "type", "preview"}}}
"""

from __future__ import annotations

import hashlib
import inspect
import os
import queue
import re
import signal
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
from ico.core.plan_keys import plan_nodes
from ico.core.runtime.cancel import IcoCancelToken
from ico.core.runtime.node import IcoRuntimeNode
from ico.core.runtime.runtime import IcoRuntime
from ico.core.runtime.tool import IcoRuntimeTool
from ico.core.signature_utils import format_ico_type
from ico.core.tree_utils import TreePathIndex
from ico.registry import RegistryEntry, default_registry, load_operators
from ico.remote.runner import RUN_ENV, RUN_VAR
from ico.spec import NodeSpec, PipelineSpec, SpecError, build, run, spec_params
from ico.spec.run import DEFAULT_MAX_ITEMS
from ico.tools.profiler import ProfilerTool, ProfileSnapshot
from ico.tools.profiler.instrument import label
from ico.tools.sample import JsonSampleTool, SampleSnapshot

from ico_mcp import assets, environment, remote
from ico_mcp.config import Config, load_config
from ico_mcp.domain_types import DomainType, collect
from ico_mcp.protocol import JsonObject, JsonValue, KernelError, decode, encode
from ico_mcp.type_names import TypeIndex

CANCEL_REASON = "cancel_run"
MAX_SAMPLES = 20
MAX_CANDIDATES = 30
_PLAN_KEY = re.compile(r"0(\.\d+)*")
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
        self._cancel: IcoCancelToken | None = None
        """The token of the run in progress, if any."""
        self._cancel_requested = False
        """A cancel came while no run was in progress (e.g. while loading):
        the next run is cancelled before it starts."""
        self._runtime: IcoRuntime | None = None
        """The runtime of the run in progress, once it exists (for samples)."""
        self._run_request: JsonValue = None
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
            "modules": self.modules,
            "plan": self.plan,
            "validate": self.validate,
            "assets_status": self.assets_status,
            "volume_up": lambda params: remote.volume_up(params),
            "volume_down": lambda params: remote.volume_down(params),
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
        """Load the stable and the draft package, or only ``modules``
        (qualified names, e.g. ``domain_draft.slug``) and what they import."""
        modules = _strings(params, "modules") if "modules" in params else None
        stable = load_operators(
            self.config.stable_dir,
            status="stable",
            modules=_in_package(modules, self.config.stable_dir.name),
        )
        draft = load_operators(
            self.config.draft_dir,
            status="draft",
            modules=_in_package(modules, self.config.draft_dir.name),
        )
        assets.register_builtins()
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
        entries = [e for e in entries if e.module != assets.BUILTIN_MODULE]
        cards: list[JsonValue] = []
        cards.extend(_card(entry) for entry in entries[:limit])
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
            **_card(entry),
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
        found = assets.operator_assets(ref)
        if found:
            card["assets"] = found
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

    def modules(self, params: JsonObject) -> JsonValue:
        """The modules that register ``refs``; ``unknown`` lists refs that
        are not registered."""
        modules: set[str] = set()
        unknown: list[JsonValue] = []
        for ref in _strings(params, "refs"):
            try:
                module = default_registry.get(ref).module
            except KeyError:
                unknown.append(ref)
                continue
            if module is not None:
                modules.add(module)
        found: list[JsonValue] = []
        found.extend(sorted(modules))
        return {"modules": found, "unknown": unknown}

    def environment(self, params: JsonObject) -> JsonValue:
        """Python, PATH, allowed variables, packages, duplicates and the
        binaries of ``bin:`` capabilities, as operators see them."""
        match = _strings(params, "match") if "match" in params else None
        needed = [
            name
            for entry in default_registry
            for name in environment.bin_names(entry.meta.caps)
        ]
        return {**environment.report(self.config, needed, match), **assets.report()}

    def validate(self, params: JsonObject) -> JsonValue:
        """Signature issues of a spec built with ``params``, and the pipeline
        parameters it declares (also when values are missing)."""
        spec = _spec(params)
        values = _values(params)
        declared: JsonValue = None
        issues: list[JsonValue] = []
        issues.extend(_missing_binaries(_required(params, "spec", dict)))
        issues.extend(assets.issues(spec, values))
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

    def assets_status(self, params: JsonObject) -> JsonValue:
        """The state of the assets of ``spec`` (with ``params``), of
        ``ids``, or of all; and the bytes to fetch."""
        spec = _spec(params) if "spec" in params else None
        ids = _strings(params, "ids") if "ids" in params else None
        try:
            return assets.assets_status(spec, _values(params), ids)
        except SpecError as e:
            raise KernelError("spec_error", str(e), _spec_error(e)) from None

    def run(self, params: JsonObject) -> JsonValue:
        if "target" in params:
            return self._run_remote(params)
        spec = _spec(params)
        max_items = _positive_int(params, "max_items", DEFAULT_MAX_ITEMS)
        values = _values(params)
        input = params.get("input")
        run_id = _optional(params, "run_id", str)
        if run_id is not None:
            # As a remote job's runner does: $RUN in strings, ICO_RUN_ID
            # for operators and the worker processes they start (each run
            # has a kernel of its own).
            os.environ[RUN_ENV] = run_id
            input = _expand_run(input, run_id)
            values = cast(JsonObject | None, _expand_run(values, run_id))
        token = IcoCancelToken()
        if self._cancel_requested:
            token.cancel(CANCEL_REASON)
        self._cancel = token
        self._send_plan(spec, values)
        self._run_request = self.request_id
        progress = JsonProgressTool(self._progress(self.request_id))
        samples = JsonSampleTool(self._sample(self.request_id))
        handle = _RuntimeHandle(self._set_runtime)
        profiler = (
            ProfilerTool(self._profile(self.request_id))
            if _optional(params, "profile", bool)
            else None
        )
        try:
            result = run(
                spec,
                input,
                max_items=max_items,
                tools=[progress, samples, handle],
                params=values,
                profiler=profiler,
                cancel=token,
                recent=bool(_optional(params, "recent", bool)),
            )
        except SpecError as e:
            raise KernelError("spec_error", str(e), _spec_error(e)) from None
        except Exception as e:
            raise KernelError(
                "run_error", f"{type(e).__name__}: {e}", _traceback(e)
            ) from None
        finally:
            self._cancel = None
            self._runtime = None
        out = cast(JsonObject, result.to_dict())
        out["drafts"] = _draft_fingerprints(_required(params, "spec", dict))
        if profiler is not None:
            out["profile"] = cast(JsonObject, dict(profiler.snapshot()))
        return out

    def _run_remote(self, params: JsonObject) -> JsonValue:
        """A run on a remote target (see :mod:`ico_mcp.remote`)."""
        token = IcoCancelToken()
        if self._cancel_requested:
            token.cancel(CANCEL_REASON)
        self._cancel = token
        self._run_request = self.request_id
        request = self.request_id

        def notify(method: str, body: JsonObject) -> None:
            self.notify({"method": method, "params": {"request": request, **body}})

        try:
            if token.cancelled:
                raise KernelError("cancelled", "the run was cancelled")
            if params.get("fetch"):
                return remote.fetch_remote(params, notify, token)
            return remote.run_remote(params, self.config.runs_dir, notify, token)
        finally:
            self._cancel = None

    def cancel(self) -> None:
        """Cancel the run in progress between items (called from the SIGTERM
        handler, in the main thread); without one, the next run is
        cancelled before it starts."""
        if self._cancel is not None:
            self._cancel.cancel(CANCEL_REASON)
        else:
            self._cancel_requested = True

    def _set_runtime(self, runtime: IcoRuntime) -> None:
        self._runtime = runtime

    def control(self, line: str) -> None:
        """Handle a one-way message (on the stdin thread, while a request
        may run); its answers are notifications."""
        message = decode(line)
        params = message.get("params", {})
        if message.get("method") != "sample" or not isinstance(params, dict):
            return
        token = params.get("token")
        try:
            self.sample(params)
        except KernelError as e:
            self._notify_run("sample_error", {"token": token, "error": e.to_dict()})
        except Exception as e:
            error = KernelError("internal", f"{type(e).__name__}: {e}")
            self._notify_run("sample_error", {"token": token, "error": error.to_dict()})

    def sample(self, params: JsonObject) -> None:
        """Ask the run in progress for the next ``count`` results of
        ``node``: notifies ``sample_started``, then the samples come."""
        runtime = self._runtime
        if runtime is None:
            raise KernelError("not_running", "no run is in progress in this kernel")
        node = _required(params, "node", str)
        count = _positive_int(params, "count", 1)
        if count > MAX_SAMPLES:
            raise KernelError("bad_request", f"count must be at most {MAX_SAMPLES}")
        key, name = _resolve_node(runtime, node)
        # Started first: samples may come as soon as the probe is set.
        started: JsonObject = {"token": params.get("token"), "node": key, "label": name}
        self._notify_run("sample_started", started)
        try:
            runtime.sample(key, count)
        except ValueError as e:
            raise KernelError("not_found", str(e)) from None

    def _notify_run(self, method: str, params: JsonObject) -> None:
        params = {"request": self._run_request, **params}
        self.notify({"method": method, "params": params})

    def _sample(self, request_id: JsonValue) -> Callable[[SampleSnapshot], None]:
        def send(sample: SampleSnapshot) -> None:
            params: JsonObject = {
                "request": request_id,
                "sample": cast(JsonObject, dict(sample)),
            }
            self.notify({"method": "sample", "params": params})

        return send

    def plan(self, params: JsonObject) -> JsonValue:
        """``{"plan": ...}``: the plan of ``spec`` with the parameter values
        ``params``, as a run sends it (for a remote run, whose kernel loads
        no operators); ``null`` when the spec does not build."""
        return {"plan": _plan(_spec(params), _values(params))}

    def _send_plan(
        self, spec: PipelineSpec | NodeSpec, values: JsonObject | None
    ) -> None:
        """Notify the plan of a spec with the run's parameter values; a spec
        that does not build fails in ``run`` with a located error instead."""
        plan = _plan(spec, values)
        if plan is None:
            return
        params: JsonObject = {"request": self.request_id, "plan": plan}
        self.notify({"method": "plan", "params": params})

    def _profile(self, request_id: JsonValue) -> Callable[[ProfileSnapshot], None]:
        def send(snapshot: ProfileSnapshot) -> None:
            params: JsonObject = {
                "request": request_id,
                "snapshot": cast(JsonObject, dict(snapshot)),
            }
            self.notify({"method": "profile", "params": params})

        return send

    def _progress(self, request_id: JsonValue) -> Callable[[ProgressSnapshot], None]:
        def send(snapshot: ProgressSnapshot) -> None:
            params: JsonObject = {"request": request_id, "snapshot": dict(snapshot)}
            self.notify({"method": "progress", "params": params})

        return send


class _RuntimeHandle(IcoRuntimeTool):
    """Gives the runtime of a run to the kernel, for samples."""

    def __init__(self, on_runtime: Callable[[IcoRuntime], None]) -> None:
        super().__init__()
        self._on_runtime = on_runtime

    def register_node(self, node: IcoRuntimeNode, path: TreePathIndex) -> None:
        if isinstance(node, IcoRuntime):
            self._on_runtime(node)


def _resolve_node(runtime: IcoRuntime, node: str) -> tuple[str, str | None]:
    """The plan key and label of ``node``: a plan key, a label (``slow@1``)
    or a label without its version (``slow``)."""
    labels: dict[str, str] = {}
    for planned in plan_nodes(runtime.plan_root, expand_remote_flows=True):
        labels.setdefault(planned.key, label(planned.node))
    if _PLAN_KEY.fullmatch(node):
        return node, labels.get(node)  # a key below a worker: its runner checks it
    exact = [k for k, name in labels.items() if name == node]
    found = exact or [k for k, name in labels.items() if name.split("@")[0] == node]
    candidates: list[JsonValue] = [
        cast(JsonValue, {"node": k, "label": labels[k]})
        for k in (found or list(labels))
    ][:MAX_CANDIDATES]
    if not found:
        raise KernelError(
            "not_found", f"no node {node!r} in the plan", {"nodes": candidates}
        )
    if len(found) > 1:
        raise KernelError(
            "not_found",
            f"{node!r} names {len(found)} nodes; pass a plan key",
            {"candidates": candidates},
        )
    return found[0], labels[found[0]]


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


def _expand_run(value: JsonValue, run_id: str) -> JsonValue:
    """``value`` with ``$RUN`` in its strings replaced by the run id."""
    if isinstance(value, str):
        return value.replace(RUN_VAR, run_id)
    if isinstance(value, list):
        return [_expand_run(v, run_id) for v in value]
    if isinstance(value, dict):
        return {k: _expand_run(v, run_id) for k, v in value.items()}
    return value


def _values(params: JsonObject) -> JsonObject | None:
    """The values of the pipeline parameters (``params.params``), if given."""
    return cast(JsonObject | None, _optional(params, "params", dict))


def _plan(
    spec: PipelineSpec | NodeSpec, values: JsonObject | None
) -> JsonObject | None:
    """The ``plan_view`` of a spec, or ``None`` when it does not build."""
    try:
        return cast(JsonObject, plan_view(build(spec, params=values)))
    except Exception:
        return None


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


def _in_package(modules: list[str] | None, package: str) -> list[str] | None:
    """The module names of ``package`` among qualified ``modules``."""
    if modules is None:
        return None
    prefix = f"{package}."
    return [m.removeprefix(prefix) for m in modules if m.startswith(prefix)]


def _card(entry: RegistryEntry) -> JsonObject:
    """A registry card; a draft's card adds the fingerprint of its module."""
    card = cast(JsonObject, entry.card())
    if entry.meta.status == "draft" and entry.module is not None:
        card["fingerprint"] = _fingerprint(entry.module)
    return card


def _fingerprint(module: str) -> str | None:
    """A short hash of a draft module's text: which code a run used."""
    text = _module_source(module)
    if text is None:
        return None
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _draft_fingerprints(spec: JsonObject) -> JsonObject:
    """``{ref: fingerprint}`` of the draft operators a spec uses."""
    out: JsonObject = {}
    for ref in sorted(_op_refs(spec)):
        try:
            entry = default_registry.get(ref)
        except KeyError:
            continue
        if entry.meta.status == "draft" and entry.module is not None:
            out[ref] = _fingerprint(entry.module)
    return out


def _op_refs(value: JsonValue) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("op")
        if isinstance(ref, str):
            refs.add(ref)
        for child in value.values():
            refs |= _op_refs(child)
    elif isinstance(value, list):
        for child in value:
            refs |= _op_refs(child)
    return refs


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
    # cancel_run sends SIGTERM to this process (not to its workers): the run
    # stops between items and returns what it produced.
    signal.signal(signal.SIGTERM, lambda signum, frame: kernel.cancel())
    requests: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        # One-way messages are handled here, also while a request runs.
        for line in stdin:
            if not line.strip():
                continue
            if _has_id(line):
                requests.put(line)
            else:
                kernel.control(line)
        requests.put(None)

    threading.Thread(target=read, name="stdin", daemon=True).start()
    while (line := requests.get()) is not None:
        write(kernel.handle(line))


def _has_id(line: str) -> bool:
    try:
        return "id" in decode(line)
    except KernelError:
        return True  # a bad line: the request loop answers it


def main() -> None:
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    serve(Path(sys.argv[1]), sys.stdin, protocol_out)


if __name__ == "__main__":
    main()
