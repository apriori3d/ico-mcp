"""Domain types: public classes defined in the stable and the draft package.

A type is known here whether or not a registered signature uses it yet, so
that an agent can find and reuse it before writing a new one. Status comes
from the package, like for operators. Classes registered as operators are
not types.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, cast, get_type_hints

from ico.core.signature_utils import format_ico_type

from ico_mcp.protocol import JsonObject, JsonValue
from ico_mcp.type_names import label


@dataclass(frozen=True, slots=True)
class DomainType:
    cls: type
    status: str

    @property
    def module(self) -> str:
        return self.cls.__module__

    def card(self, *, full: bool = False) -> JsonObject:
        """A compact card (first docstring line), or the full one with the
        module, the whole docstring and the fields."""
        doc = inspect.getdoc(self.cls) or ""
        card: JsonObject = {
            "name": self.cls.__qualname__,
            "type": label(self.cls),
            "status": self.status,
            "doc": doc if full else doc.partition("\n")[0],
        }
        if full:
            card["module"] = self.module
            card["fields"] = _fields(self.cls)
        return card


def collect(packages: Mapping[str, str], exclude: Collection[type]) -> list[DomainType]:
    """Domain types of the imported modules of ``packages`` (name → status),
    by module name, then in definition order."""
    found: list[DomainType] = []
    for name, module in sorted(list(sys.modules.items())):
        status = packages.get(name.partition(".")[0])
        if status is None:
            continue
        for attr, value in list(vars(module).items()):
            if (
                isinstance(value, type)
                and value.__module__ == name
                and not attr.startswith("_")
                and value not in exclude
            ):
                found.append(DomainType(value, status))
    return found


def _fields(cls: type) -> list[JsonValue] | None:
    """Fields of a dataclass or a Pydantic model with their types; ``None``
    for other classes."""
    target: Any = cls  # is_dataclass would narrow a type to Unknown
    if dataclasses.is_dataclass(target):
        names = [f.name for f in dataclasses.fields(target)]
    elif isinstance(model_fields := getattr(target, "model_fields", None), dict):
        names = [str(key) for key in cast(dict[object, object], model_fields)]
    else:
        return None
    try:
        hints: dict[str, Any] = get_type_hints(target)
    except Exception:
        hints = {}  # unresolvable forward references: names only
    fields: list[JsonValue] = []
    for name in names:
        tp = hints.get(name)
        fields.append(
            {"name": name, "type": format_ico_type(tp) if tp is not None else None}
        )
    return fields
