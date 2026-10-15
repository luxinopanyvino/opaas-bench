"""Drafting a system manifest from what `discover` observes (D60, phase S3).

The operator should not have to type what the host already says: every model
an engine serves, every MCP server and tool in the configurations named to
`discover`. The draft declares exactly those, so that auditing it on the same
host finds every declared component observed and nothing observed undeclared.

**A draft is not a declaration.** It writes down what is there, which is not
the same as what the system is: an engine may serve a model no application
uses, and an agent cannot be drafted at all, because nothing on the host says
which adapter reaches it. The purpose is left as a marker the system report
refuses to certify until a person replaces it. What the draft could not
decide comes back as notes, never as guesses in the manifest.
"""

from __future__ import annotations

from typing import Any

from opaas.core.hashing import slugify
from opaas.system.manifest import FORMAT

#: The purpose until the operator writes it. The system report refuses to call a
#: system certifiable while it is still there: the AI Act risk classification
#: follows the intended use, and only a person can state it.
PURPOSE_PLACEHOLDER = (
    "TO COMPLETE: the intended use of this system. The AI Act risk classification "
    "(art. 6, annex III) depends on it."
)

#: Discover's engine kind → the adapter that reaches it.
_BACKENDS = {"ollama": "ollama", "vllm": "vllm", "openai-compatible": "openai"}


def draft_manifest(
    inventory: dict[str, Any], *, system_id: str = "system", purpose: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """(manifest, notes): the components the inventory shows, and what it could not decide."""
    components: list[dict[str, Any]] = []
    notes: list[str] = []
    taken: set[str] = set()

    def unique(base: str, suffix: str) -> str:
        candidate = (slugify(base) or "component")[:56]
        if candidate in taken:
            candidate = f"{candidate}-{slugify(suffix)}"[:60]
        number = 2
        stem = candidate
        while candidate in taken:
            candidate, number = f"{stem}-{number}", number + 1
        taken.add(candidate)
        return candidate

    for engine in inventory.get("engines") or []:
        backend = _BACKENDS.get(str(engine.get("kind")))
        if backend is None:
            notes.append(f"Engine {engine.get('kind')} at {engine.get('endpoint')} has no "
                         "adapter: its models were not drafted.")
            continue
        for model in engine.get("models") or []:
            name = str(model.get("name") or "")
            if not name:
                continue
            components.append({
                "id": unique(name, str(engine.get("kind"))),
                "kind": "model",
                "backend": backend,
                "model": name,
                "base_url": f"http://{engine.get('endpoint')}",
            })
    stack = inventory.get("agentStack") or {}
    for config in stack.get("mcpConfigs") or []:
        for server in config.get("servers") or []:
            if server.get("name"):
                components.append({"id": unique(str(server["name"]), "mcp"),
                                   "kind": "mcp-server", "name": str(server["name"])})
    for manifest in stack.get("toolManifests") or []:
        for tool in manifest.get("tools") or []:
            if tool.get("name"):
                components.append({"id": unique(str(tool["name"]), "tool"), "kind": "tool",
                                   "name": str(tool["name"])})
    for runtime in stack.get("agentRuntimes") or []:
        notes.append(
            f"An agent runtime is running ({runtime.get('name') or runtime.get('process')}). "
            "Declare it as an agent component with the adapter and endpoint that reach it: "
            "nothing on the host says which."
        )
    for item in inventory.get("unidentified") or []:
        notes.append(f"Port {item.get('port')} ({item.get('process')}) looks like an engine "
                     "and did not answer as one: not drafted.")
    if not components:
        notes.append("Nothing was observed to draft: name the engines with --endpoint, the MCP "
                     "configurations with --config and the tool manifests with --manifest.")
    notes.append("Flows, records, scenarios, the declaration and thresholds are not observable "
                 "from the host: add them before auditing.")
    if purpose is None:
        notes.append("The purpose is a marker: the system is not certifiable until a person "
                     "writes the intended use.")
    manifest = {
        "format": FORMAT,
        "id": slugify(system_id) or "system",
        "purpose": purpose or PURPOSE_PLACEHOLDER,
        "components": components,
        "flows": [],
    }
    return manifest, notes


__all__ = ["PURPOSE_PLACEHOLDER", "draft_manifest"]
