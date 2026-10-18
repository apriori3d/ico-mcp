"""Profiles of runs: the summary for the agent and the record for traces.

The kernel sends ``ProfilerTool`` snapshots (``run`` with ``profile``); the
dashboard shows the whole profile on the plan. The agent gets the few nodes
that take the most time, formatted as on the dashboard, and the trace keeps
every node in numbers.
"""

from __future__ import annotations

from ico.describe.plan.options import PlanRendererOptions
from ico.tools.profiler import NodeProfile, ProfileSnapshot, profile_cells

from ico_mcp.protocol import JsonObject, JsonValue

TOP = 5
"""Nodes in the summary, by self time."""

STRUCTURAL = frozenset(t.__name__ for t in PlanRendererOptions().flatten_node_type)
"""Chains and pipelines: the plan shows no row for them, and their self time
is only the overhead of passing items on."""


def summary(snapshot: ProfileSnapshot) -> JsonObject:
    """The nodes with the largest self time, formatted (``self``, ``share``
    of all work, ``count``, ``p95``, ``info`` with startup, pool
    utilization, worker transfer)."""
    cells = profile_cells(snapshot)
    measured = [
        n for n in snapshot["nodes"] if _self(n) > 0 and n["type"] not in STRUCTURAL
    ]
    measured.sort(key=_self, reverse=True)
    top: list[JsonValue] = []
    for node in measured[:TOP]:
        cell = cells[node["key"]]
        top.append(
            {
                "key": node["key"],
                "label": node["label"],
                "self": cell["self"],
                "share": cell["share"],
                "count": cell["count"],
                "p95": cell["p95"],
                "info": cell["info"],
            }
        )
    return {
        "elapsed": round(snapshot["elapsed"], 3),
        "overhead": round(snapshot["overhead"], 3),
        "top": top,
    }


def trace(snapshot: ProfileSnapshot) -> list[JsonValue]:
    """Every node in numbers (seconds), for the run trace."""
    out: list[JsonValue] = []
    for node in snapshot["nodes"]:
        wall = node["metrics"].get("wall")
        cpu = node["metrics"].get("cpu")
        startup = node["startup"]
        start = startup.get("wall") if startup is not None else None
        out.append(
            {
                "key": node["key"],
                "label": node["label"],
                "calls": node["calls"],
                "items": node["items"],
                "errors": node["errors"],
                "self": _round(wall["self"] if wall is not None else None),
                "total": _round(wall["total"] if wall is not None else None),
                "cpu": _round(cpu["self"] if cpu is not None else None),
                "p95": _round(wall["p95"] if wall is not None else None),
                "startup": _round(start["self"] if start is not None else None),
                "utilization": _round(node["utilization"]),
                "transfer": _round(node["transfer"]),
            }
        )
    return out


def _self(node: NodeProfile) -> float:
    wall = node["metrics"].get("wall")
    return float(wall["self"]) if wall is not None else 0.0


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
