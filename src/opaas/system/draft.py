"""Drafting a system manifest from what `discover` observes (D60, phase S3).

The operator should not have to type what the host already says: every model
an engine serves, every MCP server and tool in the configurations named to
`discover`. The draft declares exactly those, so that auditing it on the same
host finds every declared component observed and nothing observed undeclared.

**Agents are drafted from what identifies them** (D63): an agent that
publishes an A2A card or names its framework, found by `discover`, with the
adapter smith will use for it (pending until smith has one); and, given the
records of a running system (`--records`), every agent that acted in them,
the models and tools it called and the flows between them. With records, only
the models actually used are declared; the others an engine serves are
noted, not drafted.

**A draft is not a declaration.** It writes down what is there, which is not
the same as what the system is: an engine may serve a model no application
uses, and an agent nothing identifies and that left no records cannot be
drafted at all. The purpose is left as a marker the system report
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
    inventory: dict[str, Any],
    *,
    system_id: str = "system",
    purpose: str | None = None,
    records: list[dict[str, Any]] | None = None,
    records_source: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """(manifest, notes): the components the inventory and records show, and what was not decided.

    `records`: AuditEvent records of the running system (as `inquest` imports
    them); `records_source`: the manifest's `records` entry pointing at them.
    """
    components: list[dict[str, Any]] = []
    notes: list[str] = []
    taken: set[str] = set()
    usage = _usage(records) if records is not None else None

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
            if usage is not None and name not in usage["models"]:
                notes.append(f"{name} is served by {engine.get('kind')} and was not called in "
                             "the records: not drafted.")
                continue
            components.append({
                "id": unique(name, str(engine.get("kind"))),
                "kind": "model",
                "backend": backend,
                "model": name,
                "base_url": f"http://{engine.get('endpoint')}",
            })
    stack = inventory.get("agentStack") or {}
    model_ids = {c["model"]: c["id"] for c in components if c["kind"] == "model"}
    agent_ids: dict[str, str] = {}
    for agent in stack.get("agentEndpoints") or []:
        component_id = unique(str(agent.get("name") or agent.get("kind")), str(agent["kind"]))
        agent_ids[str(agent.get("name"))] = component_id
        components.append({"id": component_id, "kind": "agent", "adapter": agent["adapter"],
                           "endpoint": f"http://{agent['endpoint']}",
                           "name": str(agent.get("name")),
                           "description": f"Identified by {agent.get('basis')}."})
    proxy = None
    if usage is not None:
        if records_source and records_source.get("source") == "sentinel":
            proxy = unique("sentinel", "proxy")
            components.append({"id": proxy, "kind": "proxy", "sentinel": True})
        for actor in sorted(usage["agents"]):
            if actor in agent_ids:
                continue
            agent_ids[actor] = unique(actor, "agent")
            components.append({"id": agent_ids[actor], "kind": "agent", "adapter": "observed",
                               "name": actor,
                               "description": "Known from the records it left."})
        tool_ids = {}
        for tool in sorted(usage["tools"]):
            tool_ids[tool] = unique(tool, "tool")
            components.append({"id": tool_ids[tool], "kind": "tool", "name": tool})
        for model in sorted(usage["models"] - set(model_ids)):
            notes.append(f"{model} is called in the records and no engine on this host serves "
                         "it: it may be remote (add --endpoint) or gone. Not drafted.")
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
    flows: list[dict[str, str]] = []
    if usage is not None:
        known = {**model_ids, **tool_ids}
        for actor, target in sorted(usage["edges"]):
            if actor in agent_ids and target in known:
                flow = {"from": agent_ids[actor], "to": known[target]}
                if proxy and target in model_ids:
                    flow["via"] = proxy
                flows.append(flow)
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
    if usage is None:
        notes.append("Flows and the models each agent uses come from records: pass the "
                     "sentinel's log with --records to draft them.")
    notes.append("Scenarios, the declaration and thresholds are the operator's: add them "
                 "before auditing.")
    if purpose is None:
        notes.append("The purpose is a marker: the system is not certifiable until a person "
                     "writes the intended use.")
    manifest = {
        "format": FORMAT,
        "id": slugify(system_id) or "system",
        "purpose": purpose or PURPOSE_PLACEHOLDER,
        "components": components,
        "flows": flows,
    }
    if records_source:
        manifest["records"] = [{**records_source, **({"component": proxy} if proxy else {})}]
    return manifest, notes


def _usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Agents, models and tools that appear in the records, and who called what."""
    agents: set[str] = set()
    models: set[str] = set()
    tools: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for record in records:
        chain = [h for h in record.get("actorChain") or [] if isinstance(h, dict)]
        agents |= {str(h.get("id")) for h in chain if h.get("role") == "agent" and h.get("id")}
        target, action = record.get("target"), record.get("action")
        if not target:
            continue
        if action == "model_call":
            models.add(str(target))
        elif action == "tool_call":
            tools.add(str(target))
        else:
            continue
        caller = next((h for h in reversed(chain) if h.get("role") == "agent"), None)
        if caller and caller.get("id"):
            edges.add((str(caller["id"]), str(target)))
    return {"agents": agents, "models": models, "tools": tools, "edges": edges}


__all__ = ["PURPOSE_PLACEHOLDER", "draft_manifest"]
