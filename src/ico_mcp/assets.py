"""Assets and resources as the kernel sees them (kernel side only: imports
ICO's registry).

- :func:`issues`: ``validate`` warnings for the assets a spec needs that
  are not present (``missing_asset``), errors for ids that are not in
  ``assets.toml`` (``unknown_asset``), and one warning when the spec needs
  assets but the domain has no manifest (``no_assets_manifest``);
- :func:`operator_assets`: the assets of an operator's defaults with their
  state, for ``get_operator``;
- :func:`report`: the ``assets`` and ``resources`` blocks of
  ``environment``.

- :class:`FetchAssets`: the built-in operator that ``fetch_assets`` runs
  as a job (a run like any other: dashboard, ``wait_run``, ``cancel_run``,
  trace); it yields one item per percent of the bytes to fetch.

Only ``FetchAssets`` downloads, and only when the human asked for it.
"""

from __future__ import annotations

import queue
import shutil
import threading
from collections.abc import Iterator
from typing import cast

from ico import IcoOperator
from ico.assets import AssetMissing, Assets
from ico.assets.fetch import Status, fetch, status
from ico.core.length import with_length
from ico.registry import default_registry
from ico.registry.registry import OperatorMeta
from ico.resources import pool
from ico.spec import NodeSpec, PipelineSpec, SpecError, spec_assets
from ico.spec.model import JsonValue

from ico_mcp.fetching import FETCH_REF
from ico_mcp.protocol import JsonObject


def issues(spec: PipelineSpec | NodeSpec, values: JsonObject | None) -> list[JsonValue]:
    """Issues about the assets of ``spec`` (none when it needs none or
    cannot be read: ``build`` reports that)."""
    try:
        needed = spec_assets(spec, values)
    except SpecError:
        return []
    if not needed:
        return []
    store = _store()
    if store is None:
        return [
            {
                "severity": "warning",
                "kind": "no_assets_manifest",
                "message": f"the spec needs assets ({', '.join(needed)}) but the "
                "domain has no assets.toml",
                "path": None,
                "refs": None,
            }
        ]
    found: list[JsonValue] = []
    for id, state in _states(store, needed):
        if state is None:
            known = ", ".join(sorted(store.manifest.assets)) or "none"
            found.append(
                {
                    "severity": "error",
                    "kind": "unknown_asset",
                    "message": f"asset {id!r} is not in assets.toml; known: {known}",
                    "path": None,
                    "refs": [id],
                }
            )
        elif state.state != "present":
            found.append(
                {
                    "severity": "warning",
                    "kind": "missing_asset",
                    "message": f"asset {id!r} is {state.state} ({_gb(state.size)}, "
                    f"{_gb(state.to_fetch)} to fetch): the run will fail where it "
                    f"is loaded; ask the human before fetching "
                    f"(`ico assets fetch {id}`)",
                    "path": None,
                    "refs": [id],
                }
            )
    return found


def operator_assets(ref: str) -> list[JsonValue]:
    """The assets of an operator's default params and resources, with
    their state here."""
    try:
        needed = spec_assets(NodeSpec(kind="op", op=ref))
    except SpecError:
        return []
    store = _store()
    out: list[JsonValue] = []
    for id in needed:
        if store is None:
            out.append({"id": id, "state": "no_manifest"})
            continue
        state = dict(_states(store, [id]))[id]
        out.append(_status(id, state))
    return out


def report() -> JsonObject:
    """The ``assets`` and ``resources`` blocks of ``environment``."""
    budgets: JsonObject = {"ram": pool().ram, "gpu": pool().gpu}
    store = _store()
    if store is None:
        return {"assets": {"manifest": None}, "resources": budgets}
    root = store.root
    existing = next((p for p in (root, *root.parents) if p.exists()), None)
    free = shutil.disk_usage(existing).free if existing is not None else None
    listed: list[JsonValue] = []
    listed.extend(
        _status(id, s) for id, s in _states(store, sorted(store.manifest.assets))
    )
    return {
        "assets": {
            "manifest": str(store.manifest.path),
            "root": str(root),
            "free": free,
            "items": listed,
        },
        "resources": budgets,
    }


def _store() -> Assets | None:
    try:
        return Assets.from_env()
    except FileNotFoundError:
        return None


def _states(store: Assets, ids: list[str]) -> list[tuple[str, Status | None]]:
    out: list[tuple[str, Status | None]] = []
    for id in ids:
        try:
            out.append((id, status(store, [id])[0]))
        except AssetMissing:
            out.append((id, None))
    return out


def _status(id: str, state: Status | None) -> JsonObject:
    if state is None:
        return {"id": id, "state": "unknown"}
    return {
        "id": id,
        "state": state.state,
        "size": state.size,
        "to_fetch": state.to_fetch,
        "verified": state.verified,
    }


def _gb(size: int) -> str:
    return f"{size / 1e9:.2f} GB"


def assets_status(
    spec: PipelineSpec | NodeSpec | None,
    values: JsonObject | None,
    ids: list[str] | None,
) -> JsonObject:
    """The state of the assets a spec needs (or of ``ids``; all when both
    are missing), and the bytes to fetch."""
    store = _store()
    if store is None:
        return {"manifest": None, "items": [], "to_fetch": 0}
    if spec is not None:
        wanted = spec_assets(spec, values)
    else:
        wanted = ids if ids else sorted(store.manifest.assets)
    states = _states(store, wanted)
    items: list[JsonValue] = []
    items.extend(_status(id, s) for id, s in states)
    to_fetch = sum(s.to_fetch for _, s in states if s is not None)
    return {"manifest": str(store.manifest.path), "items": items, "to_fetch": to_fetch}


# ─── The fetch job ───

TICKS = 100


class _Stopped(Exception):
    pass


class FetchAssets(IcoOperator[list[str], Iterator[JsonObject]]):
    """Download assets (as ``ico assets fetch``): one item per percent of
    the bytes to fetch, then a summary with the state of each asset. A
    cancelled fetch stops in the middle of a file; the part on disk is
    continued by the next fetch."""

    def __init__(self) -> None:
        super().__init__(fn=self._apply)

    def _apply(self, ids: list[str]) -> Iterator[JsonObject]:
        store = Assets.from_env()
        total = sum(s.to_fetch for s in status(store, ids))
        return with_length(self._ticks(store, ids, total), TICKS + 1 if total else 1)

    def _ticks(self, store: Assets, ids: list[str], total: int) -> Iterator[JsonObject]:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        stop = threading.Event()
        done: dict[str, int] = {}
        shown = 0

        def progress(id: str, got: int, size: int) -> None:
            if stop.is_set():
                raise _Stopped
            done[id] = got
            events.put(("tick", id))

        def work() -> None:
            try:
                events.put(("done", fetch(store, ids, progress=progress)))
            except BaseException as e:  # reported to the run as its error
                events.put(("error", e))

        thread = threading.Thread(target=work, name="ico-fetch-assets", daemon=True)
        thread.start()
        try:
            while True:
                kind, value = events.get()
                if kind == "tick":
                    id = cast(str, value)
                    percent = sum(done.values()) * TICKS // max(total, 1)
                    while shown < min(percent, TICKS):
                        shown += 1
                        yield {"percent": shown, "asset": id}
                elif kind == "done":
                    while shown < TICKS and total:
                        shown += 1
                        yield {"percent": shown}
                    after = cast(list[Status], value)
                    items: list[JsonValue] = []
                    items.extend(_status(s.id, s) for s in after)
                    yield {"fetched": items}
                    return
                else:
                    raise cast(BaseException, value)
        finally:
            stop.set()


BUILTIN_MODULE = __name__


def register_builtins() -> None:
    """Register :class:`FetchAssets` (again after a reload)."""
    default_registry.add_class(
        FetchAssets,
        OperatorMeta(name=FETCH_REF.split("@")[0], version=1, tags=frozenset({"system"}),
                     status="stable"),
        replace=True,
    )  # fmt: skip


__all__ = [
    "BUILTIN_MODULE",
    "FetchAssets",
    "assets_status",
    "issues",
    "operator_assets",
    "register_builtins",
    "report",
]
