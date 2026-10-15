"""The system manifest: what is being audited, as the operator declares it (D60).

The audited subject is a system, not a model or an agent: a purpose, the
components that serve it and the flows between them. `discover` will draft
this file (phase S3); the operator completes and confirms it. It is a
declaration, and it is audited against what is observed, never added to it
(invariant 9).

Validation is strict on purpose. An unknown key is refused rather than
ignored: a misspelt `"scenario"` silently dropped would leave every agent
uncertifiable with nothing saying why. A component kind decides what the
component must name for OPAAS to reach it, and nothing more.

**MCP agents are accepted before they can be measured.** `"adapter": "mcp"` is
valid today; until the client adapter exists, that agent's cells read *not
measured* with the reason, and the same manifest measures it once the adapter
is registered (system-audit.md §10.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FORMAT = 1

#: Component kinds and the fields each must carry to be reached.
COMPONENT_FIELDS: dict[str, tuple[str, ...]] = {
    "model": ("backend", "model"),
    "agent": ("adapter",),
    "tool": (),
    "mcp-server": (),
    "proxy": (),
    "human-approver": ("name",),
    "data-store": (),
    #: Outside the operator's control: listed, never certified (D22).
    "external": ("provider",),
}

#: Fields a component may carry beyond `id` and `kind`.
_OPTIONAL_FIELDS = frozenset({
    "backend", "model", "base_url", "num_ctx", "adapter", "endpoint", "via", "server",
    "command", "name", "sentinel", "provider", "description",
})

#: Agent adapters the manifest accepts before they are implemented, so that a
#: system's description does not change the day the adapter lands. Empty since
#: the MCP adapter arrived (S4); the mechanism stays for the next one.
PENDING_ADAPTERS: frozenset[str] = frozenset()

_TOP_LEVEL = frozenset({
    "format", "id", "purpose", "components", "flows", "scenarios", "policies", "records",
    "declaration", "thresholds",
})
_RECORD_FIELDS = frozenset({"source", "path", "key", "component"})
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SystemManifestError(ValueError):
    """The system manifest is malformed; the message says where and how to fix it."""


@dataclass(frozen=True, slots=True)
class Component:
    id: str
    kind: str
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SystemManifest:
    id: str
    purpose: str
    components: tuple[Component, ...]
    flows: tuple[dict[str, str], ...]
    scenarios: tuple[str, ...]
    records: tuple[dict[str, Any], ...]
    policies: str | None
    declaration: str | None
    thresholds: str | None

    def component(self, component_id: str) -> Component | None:
        return next((c for c in self.components if c.id == component_id), None)

    def pending(self) -> list[dict[str, str]]:
        """Components declared with an adapter that does not exist yet."""
        return [
            {"component": c.id, "adapter": c.fields["adapter"]}
            for c in self.components
            if c.kind == "agent" and c.fields.get("adapter") in PENDING_ADAPTERS
        ]


def load_manifest(document: Any, *, known_adapters: tuple[str, ...] = ()) -> SystemManifest:
    """Validates a manifest document. `known_adapters`: the agent adapters that exist."""
    if not isinstance(document, dict):
        raise SystemManifestError("A system manifest is a JSON object.")
    unknown = sorted(set(document) - _TOP_LEVEL)
    if unknown:
        raise SystemManifestError(
            f"Unknown key(s) {', '.join(unknown)}. "
            f"A manifest takes: {', '.join(sorted(_TOP_LEVEL))}."
        )
    if document.get("format") != FORMAT:
        raise SystemManifestError(f"'format' must be {FORMAT}, got {document.get('format')!r}.")
    system_id = _identifier(document.get("id"), "the system's 'id'")
    purpose = document.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise SystemManifestError(
            "'purpose' is required: the intended use is what the AI Act risk classification "
            "depends on, and it belongs to the system, not to a model."
        )

    raw_components = document.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise SystemManifestError("'components' must list at least one component.")
    components: list[Component] = []
    for index, raw in enumerate(raw_components):
        components.append(_component(raw, index, known_adapters))
    ids = [c.id for c in components]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemManifestError(f"Component id(s) used twice: {', '.join(duplicates)}.")

    flows = tuple(
        _flow(raw, index, set(ids)) for index, raw in enumerate(document.get("flows") or [])
    )
    for c in components:
        for name in ("via", "server"):
            ref = c.fields.get(name)
            if isinstance(ref, str) and ref not in ids and ref != "mcp":
                raise SystemManifestError(
                    f"Component '{c.id}': '{name}' names '{ref}', which is not a component."
                )

    return SystemManifest(
        id=system_id,
        purpose=purpose.strip(),
        components=tuple(components),
        flows=flows,
        scenarios=_strings(document.get("scenarios"), "scenarios"),
        records=tuple(_record(raw, index, set(ids)) for index, raw in
                      enumerate(document.get("records") or [])),
        policies=_path(document.get("policies"), "policies"),
        declaration=_path(document.get("declaration"), "declaration"),
        thresholds=_path(document.get("thresholds"), "thresholds"),
    )


def _component(raw: Any, index: int, known_adapters: tuple[str, ...]) -> Component:
    where = f"components[{index}]"
    if not isinstance(raw, dict):
        raise SystemManifestError(f"{where} must be an object.")
    component_id = _identifier(raw.get("id"), f"{where}.id")
    where = f"Component '{component_id}'"
    kind = raw.get("kind")
    if kind not in COMPONENT_FIELDS:
        raise SystemManifestError(
            f"{where}: kind must be one of {', '.join(COMPONENT_FIELDS)}, got {kind!r}."
        )
    fields = {k: v for k, v in raw.items() if k not in ("id", "kind")}
    unknown = sorted(set(fields) - _OPTIONAL_FIELDS)
    if unknown:
        raise SystemManifestError(f"{where}: unknown field(s) {', '.join(unknown)}.")
    missing = [name for name in COMPONENT_FIELDS[kind] if not fields.get(name)]
    if missing:
        raise SystemManifestError(f"{where} ({kind}) needs {', '.join(missing)}.")
    if kind == "agent":
        adapter = fields["adapter"]
        accepted = (*known_adapters, *sorted(PENDING_ADAPTERS))
        if known_adapters and adapter not in accepted:
            raise SystemManifestError(
                f"{where}: adapter must be one of {', '.join(accepted)}, got {adapter!r}."
            )
        if adapter in ("http", "mcp") and not fields.get("endpoint"):
            raise SystemManifestError(f"{where}: the {adapter} adapter needs an 'endpoint'.")
    return Component(id=component_id, kind=kind, fields=fields)


def _flow(raw: Any, index: int, ids: set[str]) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - {"from", "to", "via"}:
        raise SystemManifestError(f"flows[{index}] takes 'from', 'to' and optionally 'via'.")
    for name in ("from", "to", "via"):
        if name in raw and raw[name] not in ids:
            raise SystemManifestError(
                f"flows[{index}].{name} names '{raw[name]}', which is not a component."
            )
    if "from" not in raw or "to" not in raw:
        raise SystemManifestError(f"flows[{index}] needs both 'from' and 'to'.")
    if raw["from"] == raw["to"]:
        raise SystemManifestError(f"flows[{index}] goes from '{raw['from']}' to itself.")
    return {k: str(v) for k, v in raw.items()}


def _record(raw: Any, index: int, ids: set[str]) -> dict[str, Any]:
    where = f"records[{index}]"
    if not isinstance(raw, dict) or not raw.get("source") or not raw.get("path"):
        raise SystemManifestError(f"{where} needs 'source' (its format) and 'path'.")
    unknown = sorted(set(raw) - _RECORD_FIELDS)
    if unknown:
        raise SystemManifestError(f"{where}: unknown field(s) {', '.join(unknown)}.")
    if "component" in raw and raw["component"] not in ids:
        raise SystemManifestError(f"{where}.component is not a component of this system.")
    return dict(raw)


def _identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise SystemManifestError(
            f"{what} must be lowercase letters, digits, '.', '_' or '-', got {value!r}."
        )
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise SystemManifestError(f"'{name}' must be a list of paths or patterns.")
    return tuple(value)


def _path(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SystemManifestError(f"'{name}' must be a path.")
    return value


__all__ = [
    "COMPONENT_FIELDS",
    "FORMAT",
    "PENDING_ADAPTERS",
    "Component",
    "SystemManifest",
    "SystemManifestError",
    "load_manifest",
]
