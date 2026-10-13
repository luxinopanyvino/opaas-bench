"""`discover`: what is running here, and what can and cannot be audited about it (detect).

Three parts, each deterministic and read-only:

- `inventory`: engines, versions, ports, bind scope, served models and their
  digests, GPUs and driver (D40);
- `agents`: MCP servers, tool and skill manifests, containers and agent
  runtimes, from the files the operator names (D45);
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
from opaas.discover import inventory as _inventory
from opaas.discover.agents import Runner, discover_agent_stack
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
) -> dict[str, Any]:
    """The engine inventory and the agent stack, under one environment hash.

    `engines` and `listeners` can be injected; tests never probe the host.
    """
    if listeners is None:
        listeners, _ = _inventory.listening_ports()
    inventory = engines if engines is not None else _inventory.discover_environment(
        endpoints=endpoints)
    stack = discover_agent_stack(
        configs=configs, manifests=manifests, skill_dirs=skill_dirs,
        listeners=listeners, containers=containers, runner=runner,
    )
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
