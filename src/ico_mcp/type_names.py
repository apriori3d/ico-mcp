"""Resolve type names written by an agent to the types of registered signatures.

An agent refers to a type by short name (``Report``), by ``module:qualname``
(``domain.text:Report``) or by the formatted type as cards show it in
``sig`` (``list[Report]``). Known types are given to the index: the inputs
and outputs of registered signatures, including generic arguments, and the
domain types.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_args, get_origin

from ico.core.signature_utils import format_ico_type

from ico_mcp.protocol import JsonValue, KernelError


class TypeIndex:
    """Names of a set of types.

    Args:
        types: The known types; generic arguments are known too.
        unknown: Why a name is unknown, for the error message.
    """

    def __init__(self, types: Iterable[Any], unknown: str) -> None:
        self._types: dict[str, list[Any]] = {}
        self._unknown = unknown
        for tp in types:
            self._add(tp)

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
                f"unknown type {name!r}: {self._unknown}",
                {"name": name},
            )
        if len(candidates) > 1:
            labels: list[JsonValue] = []
            labels.extend(sorted(label(tp) for tp in candidates))
            raise KernelError(
                "type_error",
                f"ambiguous type {name!r}; use one of the candidates",
                {"name": name, "candidates": labels},
            )
        return candidates[0]

    def _add(self, tp: Any) -> None:
        names = {format_ico_type(tp).replace(" ", "")}
        if get_origin(tp) is None and isinstance(tp, type):
            names.add(label(tp))
        for name in names:
            known = self._types.setdefault(name, [])
            if not any(t == tp for t in known):
                known.append(tp)
        for arg in get_args(tp):
            self._add(arg)


def label(tp: Any) -> str:
    if get_origin(tp) is None and isinstance(tp, type):
        return f"{tp.__module__}:{tp.__qualname__}"
    return repr(tp)
