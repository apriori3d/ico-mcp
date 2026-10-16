"""Turn kernel progress snapshots into MCP progress notifications.

MCP allows one progress stream per request with a strictly increasing
``progress``. The status is the first progress node of the flow: the kernel
reports every node with ``done = 0`` in flow order before any progress, so
the first snapshot names it. Other nodes are only kept for the run trace
(and, later, the dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass

from ico_mcp.protocol import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class Status:
    """Arguments of one MCP progress notification."""

    progress: float
    total: float | None
    message: str


class ProgressReporter:
    """Follows the snapshots of one run."""

    def __init__(self) -> None:
        self._first: str | None = None
        self._last: float | None = None
        self._nodes: dict[str, JsonObject] = {}

    def update(self, snapshot: JsonObject) -> Status | None:
        """The status to send for a snapshot, or ``None`` to send nothing.

        Nothing is sent for other nodes or when ``progress`` would not
        increase (e.g. the first node restarts in a loop).
        """
        key = str(snapshot.get("key"))
        self._nodes[key] = snapshot
        if self._first is None:
            self._first = key
        if key != self._first:
            return None
        done = _number(snapshot.get("done"))
        if self._last is not None and done <= self._last:
            return None
        self._last = done
        total = _number(snapshot.get("total"))
        label = str(snapshot.get("label"))
        if total > 0:
            return Status(done, total, f"{label} {done:g}/{total:g}")
        return Status(done, None, f"{label} {done:g}")

    def summary(self) -> list[JsonValue]:
        """The last state of every node, in the order they were reported."""
        out: list[JsonValue] = []
        for snapshot in self._nodes.values():
            out.append({k: snapshot.get(k) for k in ("key", "label", "done", "total")})
        return out


def _number(value: JsonValue) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
