"""`discover`: what is running here, and what can and cannot be audited about it (detect).

Three parts, each deterministic and read-only:

- `inventory`: engines, versions, ports, bind scope, served models and their
  digests, GPUs and driver (D40);
- `agents`: MCP servers, tool and skill manifests, containers and agent
  runtimes, from the files the operator names (D45);
- `identify`: agents that identify themselves (A2A cards, Open WebUI, agent
  frameworks by their OpenAPI title) and MCP configurations in their usual
  places (D63);
- `gaps`: the auditability gap scan over both (OP1-lite).

`discover()` assembles the first two into one evidence document whose `hash`
is what every later agent result is bound to (N5): `smith`, `sentinel` and
`inquest` results carry it, and `sentinel` alerts when the environment leaves
it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opaas.core.hashing import sha256_json
from opaas.discover import identify as _identify
from opaas.discover import inventory as _inventory
from opaas.discover.agents import Runner, agent_stack_hash, discover_agent_stack
from opaas.discover.inventory import compare_inventories


def discover(
    *,
    endpoints: Iterable[str] = (),
    configs: Iterable[Path] = (),
    manifests: Iterable[Path] = (),
    skill_dirs: Iterable[Path] = (),
    containers: bool = True,
    runner: Runner | None = None,
    engines: dict[str, Any] | None = None,
    listeners: dict | None = None,
    well_known: bool = False,
    agent_endpoints: Iterable[str] = (),
) -> dict[str, Any]:
    """The engine inventory and the agent stack, under one environment hash.

    `engines` and `listeners` can be injected; tests never probe the host.
    `well_known` adds the MCP configurations found in their usual places, and
    the agents that identify themselves are probed on every listener that is
    not an engine (D63). Agent runtimes get one GET each (D64).
    """
    if listeners is None:
        listeners, _ = _inventory.listening_ports()
    inventory = engines if engines is not None else _inventory.discover_environment(
        endpoints=endpoints)
    configs = list(configs)
    known = _identify.well_known_mcp_configs() if well_known else []
    configs += [path for path in known if path not in configs]
    stack = discover_agent_stack(
        configs=configs, manifests=manifests, skill_dirs=skill_dirs,
        listeners=listeners, containers=containers, runner=runner,
    )
    engine_ports = {port for engine in inventory.get("engines") or []
                    for port in [engine.get("port"),
                                 *(m.get("port") for m in engine.get("alsoAnswersOn") or []),
                                 *(r.get("port") for r in engine.get("runners") or [])]
                    if port}
    stack["agentEndpoints"] = _identify.probe_agents(
        listeners or {}, endpoints=agent_endpoints, skip_ports=engine_ports)
    stack["agentProbes"] = _identify.AGENT_PROBES_VERSION
    # Probed after identification, so a runtime that is an identified agent
    # says so; probe results stay out of the stack hash, as ports do (D64).
    stack["agentRuntimes"] = _identify.probe_runtimes(
        stack["agentRuntimes"], listeners or {}, agents=stack["agentEndpoints"])
    if well_known:
        stack["wellKnownConfigs"] = [path.name for path in known]
        stack["notObserved"] = [n for n in stack["notObserved"]
                                if not n.startswith("MCP servers:")]
    stack["hash"] = agent_stack_hash(stack)
    document = {**inventory, "enginesHash": inventory.get("hash"), "agentStack": stack}
    document["hash"] = environment_hash(document)
    return document


def environment_hash(document: dict[str, Any]) -> str:
    return sha256_json({"engines": document.get("enginesHash"),
                        "agentStack": (document.get("agentStack") or {}).get("hash")})


def compare_discoveries(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Engine changes (as `compare_inventories`) plus changes to the agent stack."""
    changes = compare_inventories(baseline, current)

    def index(document: dict[str, Any], section: str) -> dict[str, str]:
        stack = document.get("agentStack") or {}
        if section == "mcpServers":
            return {s["name"]: s["entryHash"]
                    for c in stack.get("mcpConfigs", []) for s in c.get("servers", [])}
        if section == "tools":
            return {t["name"]: t["definitionHash"]
                    for m in stack.get("toolManifests", []) for t in m.get("tools", [])}
        if section == "skills":
            return {s["path"]: s["hash"]
                    for r in stack.get("skills", []) for s in r.get("skills", [])}
        return {c["name"]: c["imageDigest"] or c["image"]
                for c in (stack.get("containers") or {}).get("containers", [])}

    for section in ("mcpServers", "tools", "skills", "containers"):
        then, now = index(baseline, section), index(current, section)
        for kind, names in (
            ("Added", sorted(now.keys() - then.keys())),
            ("Removed", sorted(then.keys() - now.keys())),
            ("Changed", sorted(n for n in then.keys() & now.keys() if then[n] != now[n])),
        ):
            if names:
                changes[f"{section}{kind}"] = [{"name": n} for n in names]
    return changes


__all__ = ["compare_discoveries", "discover", "environment_hash"]
