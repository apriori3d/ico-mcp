"""JSON-lines protocol between the server and the kernel.

One JSON object per line. A request is ``{"id", "method", "params"}``; a
response is ``{"id", "result"}`` or ``{"id", "error": {"code", "message",
...}}``. Standard library only: the server imports this module and must not
import operator code.
"""

from __future__ import annotations

import json
from typing import Literal, TypeAlias, cast, get_args

JsonValue: TypeAlias = (
    "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
)
JsonObject: TypeAlias = dict[str, JsonValue]

ErrorCode = Literal[
    "bad_request",  # malformed request or params
    "not_found",  # unknown operator reference
    "type_error",  # unknown or ambiguous type name
    "spec_error",  # a spec cannot be parsed or built (``SpecError`` fields)
    "run_error",  # the flow raised
    "internal",  # unexpected error in the kernel
    "timeout",  # the request timed out; the kernel was killed
    "crashed",  # the kernel exited
    "not_running",  # the run asked for (samples) is not in progress
    "cancelled",  # a remote run was cancelled
    "load_error",  # a remote job's operator modules failed to import
    "busy",  # a remote job's work directory is held by another runner
    "storage_error",  # uploading inputs or downloading outputs of a remote run
    "remote_error",  # the target failed (HTTP, a job without a result)
    "missing_assets",  # a remote run's assets are not on the target (prefetch them)
    "fetch_error",  # a remote prefetch failed (manifest, hub, a changed source)
]
ERROR_CODES: tuple[ErrorCode, ...] = get_args(ErrorCode)


class KernelError(Exception):
    """A structured error returned by the kernel or raised by the client.

    Attributes:
        code: One of :data:`ERROR_CODES`.
        message: Human-readable description.
        data: Code-specific JSON fields, e.g. ``candidates`` of a
            ``type_error`` or ``path`` of a ``spec_error``.
    """

    def __init__(
        self, code: ErrorCode, message: str, data: JsonObject | None = None
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.data: JsonObject = data or {}

    def to_dict(self) -> JsonObject:
        return {"code": self.code, "message": self.message, **self.data}

    @classmethod
    def from_dict(cls, error: JsonObject) -> KernelError:
        fields = dict(error)
        code = fields.pop("code", None)
        message = fields.pop("message", None)
        if code not in ERROR_CODES or not isinstance(message, str):
            return cls("internal", f"malformed kernel error: {error!r}")
        return cls(code, message, fields)


def encode(message: JsonObject) -> bytes:
    """One protocol line."""
    return (json.dumps(message, ensure_ascii=False) + "\n").encode()


def decode(line: bytes | str) -> JsonObject:
    """Parse one protocol line.

    Raises:
        KernelError: ``bad_request`` if it is not a JSON object.
    """
    try:
        message = json.loads(line)
    except ValueError as e:
        raise KernelError("bad_request", f"invalid JSON: {e}") from None
    if not isinstance(message, dict):
        raise KernelError("bad_request", "a message must be a JSON object")
    return cast(JsonObject, message)
