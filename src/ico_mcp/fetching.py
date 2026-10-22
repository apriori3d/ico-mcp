"""The pipeline that ``fetch_assets`` runs as a job (server side: no ICO
imports; the operator is ``ico_mcp.assets.FetchAssets`` in the kernel)."""

from __future__ import annotations

from ico_mcp.protocol import JsonObject

FETCH_REF = "ico_mcp.fetch_assets@1"
FETCH_SPEC: JsonObject = {
    "kind": "seq",
    "name": "fetch assets",
    "children": [
        {"kind": "op", "op": FETCH_REF},
        {"kind": "stream", "children": [{"kind": "progress", "name": "fetch"}]},
    ],
}
"""Its input is the list of asset ids to fetch."""
CONFIRM_BYTES = 2 * 10**9
"""Above this, ``fetch_assets`` asks for ``confirm=true`` (the human's
word): a download of tens of GB can take hours."""
FETCHABLE = ("missing", "partial", "wrong_size")
MAX_ITEMS = 101
"""One item per percent, then the summary: all of them in the result."""


def fetch_snapshot(id: str, done: float, total: float) -> JsonObject:
    """The progress snapshot of one asset of a prefetch (bytes shown in
    MB); the server shows every asset at zero before the job starts."""
    return {
        "key": f"fetch.{id}",
        "label": f"fetch {id} (MB)",
        "node": None,
        "done": round(done / 1e6, 1),
        "total": round(total / 1e6, 1),
        "rate": None,
        "eta": None,
    }
