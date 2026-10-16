"""Resolve type names written by an agent to the types of registered signatures.

An agent refers to a type by short name (``Report``), by ``module:qualname``
(``domain.text:Report``) or by the formatted type as cards show it in
``sig`` (``list[Report]``). Only types that occur in the input or output of
a registered signature, including generic arguments, are known.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_args, get_origin

from ico import IcoSignature
from ico.core.signature_utils import format_ico_type  # pyright: ignore[reportMissingTypeStubs]  # ico.core is a namespace package

from ico_mcp.protocol import JsonValue, KernelError


class TypeIndex:
    """Type names of a set of signatures."""

    def __init__(self, signatures: Iterable[IcoSignature]) -> None:
        self._types: dict[str, list[Any]] = {}
        for signature in signatures:
            self._add(signature.i)
            self._add(signature.o)

    def resolve(self, name: str) -> Any:
        """The type called ``name``.

        Raises:
            KernelError: ``type_error`` if the name is unknown or ambiguous;
                an ambiguous error lists the ``candidates``.
        """
        candidates = self._types.get(name.replace(" ", ""), [])
        if not candidates:
            raise KernelError(
                "type_error",
                f"unknown type {name!r}: it occurs in no registered signature",
                {"name": name},
            )
        if len(candidates) > 1:
            labels: list[JsonValue] = []
            labels.extend(sorted(_label(tp) for tp in candidates))
            raise KernelError(
                "type_error",
                f"ambiguous type {name!r}; use one of the candidates",
                {"name": name, "candidates": labels},
            )
        return candidates[0]

    def _add(self, tp: Any) -> None:
        names = {format_ico_type(tp).replace(" ", "")}
        if get_origin(tp) is None and isinstance(tp, type):
            names.add(_label(tp))
        for name in names:
            known = self._types.setdefault(name, [])
            if not any(t == tp for t in known):
                known.append(tp)
        for arg in get_args(tp):
            self._add(arg)


def _label(tp: Any) -> str:
    if get_origin(tp) is None and isinstance(tp, type):
        return f"{tp.__module__}:{tp.__qualname__}"
    return repr(tp)
