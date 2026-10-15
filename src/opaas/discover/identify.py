"""Identifying the agents of a system, not only its engines (D63).

D45 listed agent runtimes by process name and never probed them, because an
agent runtime had no well-known read-only endpoint. Some now publish one, and
an operator should not have to declare by hand what the host can say:

- **A2A agents** publish an *agent card* at `/.well-known/agent-card.json`
  (`agent.json` before A2A 0.3): name, description, URL, skills. It exists to
  be fetched by whoever wants to know what the agent is.
- **Open WebUI** answers `/api/config` with its own name and version.
- **A service with an OpenAPI document** whose title names an agent framework
  (LangGraph, LangServe, Flowise, Dify, n8n, Letta, AutoGen, CrewAI, OpenHands,
  AnythingLLM) or the word "agent".

Every probe is a GET with a short timeout, on listeners that are not already
identified as engines, and every agent found says **how** it was identified
(`basis`): a published card is not the same evidence as a title. Nothing is
sent that could change state, and nothing is guessed: a listener that answers
none of these is not an agent as far as OPAAS knows.

**MCP configurations in their usual places.** The MCP clients people use keep
their server lists in known files (Claude Desktop, Claude Code, Cursor, VS
Code, Windsurf, Continue). `well_known_mcp_configs` returns the ones that
exist on this host, for `discover` to read exactly as if they had been named:
names, transports, launchers and package ids, never a value (N4). Known files
only: OPAAS still never crawls the disk.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from opaas.discover.inventory import Listener

#: Bumped when a probe is added or changed: the inventory says which set it ran.
AGENT_PROBES_VERSION = 1

#: Words in an OpenAPI title that mark an agent framework, and the adapter name
#: it gets in a system manifest.
FRAMEWORKS: dict[str, str] = {
    "langgraph": "langgraph",
    "langserve": "langserve",
    "langchain": "langserve",
    "flowise": "flowise",
    "dify": "dify",
    "n8n": "n8n",
    "letta": "letta",
    "autogen": "autogen",
    "crewai": "crewai",
    "openhands": "openhands",
    "anythingllm": "anythingllm",
}

PROBE_TIMEOUT_S = 0.8

#: Listeners probed at most: a host with hundreds of ports is not walked whole.
MAX_LISTENERS = 64


def scan_agents(
    listeners: dict[int, Listener],
    *,
    endpoints: Iterable[str] = (),
    skip_ports: Iterable[int] = (),
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Agents that identify themselves on the listeners and the named endpoints."""
    skipped = set(skip_ports)
    targets: dict[str, dict[str, Any]] = {}
    for port, listener in sorted(listeners.items()):
        if port in skipped or (port < 1024 and port != 80):
            continue
        targets[listener.base_url] = {"port": port, "bind": listener.bind,
                                      "process": listener.process}
        if len(targets) >= MAX_LISTENERS:
            break
    for endpoint in endpoints:
        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        if parsed.hostname:
            targets[f"{parsed.scheme}://{parsed.netloc}"] = {
                "port": parsed.port, "bind": "not-observed", "process": None}
    if not targets:
        return []
    found = []
    with httpx.Client(timeout=PROBE_TIMEOUT_S, transport=transport) as client:
        for base_url, where in targets.items():
            agent = _identify_agent(client, base_url)
            if agent is not None:
                found.append({**agent, "endpoint": urlparse(base_url).netloc, **where})
    return sorted(found, key=lambda a: (str(a["kind"]), str(a.get("port"))))


#: What `discover` calls. Tests replace it: no test probes the host's ports.
probe_agents = scan_agents


def _get(client: httpx.Client, url: str) -> tuple[bool, Any]:
    """(answered over HTTP, JSON body or None)."""
    try:
        response = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return False, None
    if response.status_code != 200:
        return True, None
    try:
        return True, response.json()
    except ValueError:
        return True, None


def _identify_agent(client: httpx.Client, base_url: str) -> dict[str, Any] | None:
    answered = False
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        http, card = _get(client, base_url + path)
        answered = answered or http
        if not http:
            return None  # not an HTTP service at all: nothing else to ask
        if isinstance(card, dict) and card.get("name") and (
            card.get("skills") is not None or card.get("capabilities") is not None
            or card.get("url")
        ):
            return {
                "kind": "a2a", "adapter": "a2a", "name": str(card["name"]),
                "version": card.get("version"), "basis": f"A2A agent card at {path}",
                "skills": sorted(str(s.get("name") or s.get("id")) for s in card.get("skills")
                                 or [] if isinstance(s, dict)),
            }
    if not answered:
        return None
    _, config = _get(client, base_url + "/api/config")
    if isinstance(config, dict) and "open webui" in str(config.get("name", "")).lower():
        return {"kind": "open-webui", "adapter": "open-webui", "name": str(config["name"]),
                "version": config.get("version"), "basis": "Open WebUI /api/config"}
    _, spec = _get(client, base_url + "/openapi.json")
    info = spec.get("info") if isinstance(spec, dict) else None
    if isinstance(info, dict) and info.get("title"):
        title = str(info["title"])
        words = title.lower().replace("-", "").replace("_", "")
        framework = next((adapter for key, adapter in FRAMEWORKS.items() if key in words), None)
        if framework is None and "agent" in words:
            framework = "agent-api"
        if framework is not None:
            return {"kind": framework, "adapter": framework, "name": title,
                    "version": info.get("version"), "basis": "OpenAPI title"}
    # A framework's process that did not identify itself stays an agent runtime,
    # listed by process name (D45): it is never promoted to an agent by guess.
    return None


def well_known_mcp_configs(environ: dict[str, str] | None = None,
                           platform: str | None = None) -> list[Path]:
    """MCP client configuration files that exist on this host, in their usual places."""
    env = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    candidates = [
        home / ".claude.json",                      # Claude Code
        home / ".cursor" / "mcp.json",              # Cursor
        home / ".codeium" / "windsurf" / "mcp_config.json",  # Windsurf
        home / ".continue" / "config.json",         # Continue
    ]
    if platform.startswith("win"):
        appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        candidates += [appdata / "Claude" / "claude_desktop_config.json",
                       appdata / "Code" / "User" / "mcp.json"]
    elif platform == "darwin":
        support = home / "Library" / "Application Support"
        candidates += [support / "Claude" / "claude_desktop_config.json",
                       support / "Code" / "User" / "mcp.json"]
    else:
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        candidates += [config / "Claude" / "claude_desktop_config.json",
                       config / "Code" / "User" / "mcp.json"]
    found = []
    for path in candidates:
        with contextlib.suppress(OSError):
            if path.is_file():
                found.append(path)
    return found


__all__ = [
    "AGENT_PROBES_VERSION",
    "FRAMEWORKS",
    "probe_agents",
    "scan_agents",
    "well_known_mcp_configs",
]
