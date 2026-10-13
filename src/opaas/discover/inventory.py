"""Environment discovery: what is serving models on this host (D40).

A certificate describes one model on one engine. The host it runs on may be
serving five more, through two engines, one of them reachable from the whole
network without credentials, and none of that was visible to OPAAS. This
module looks: which processes listen, which of them answer as a model server,
what each one serves, and on which GPUs.

**Read-only and deterministic.** It sends GET requests to well-known
endpoints (`/api/version`, `/api/tags`, `/version`, `/v1/models`) and nothing
else. It never sends credentials, loads a model or generates a token, so an
engine that answers is, by that fact, one that answers without credentials.

**This host only, and what it cannot see is said.** A remote endpoint is
included only when the operator names it with `--endpoint`. A listening
process that looks like an engine but does not answer is listed as
unidentified, never guessed at.

**Publishable by construction (invariant 12).** The inventory goes into a
public, signed report. It records process *names*, never command lines,
because a command line can carry an API key or a path the operator does not
want published. It records no hostname.

The inventory has a hash over what identifies the environment: engines,
versions, ports, bind scope, models and their digests, GPUs and driver. Which
models happen to be loaded in memory right now changes by the minute and is
left out.
"""

from __future__ import annotations

import contextlib
import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from opaas.core.hashing import sha256_json

#: Ports engines listen on by default. 8080 is shared by llama-server and TGI.
DEFAULT_PORTS: tuple[int, ...] = (11434, 8000, 8080, 1234, 30000)

#: Substrings of process names that mark a listener worth probing on any port.
ENGINE_PROCESS_HINTS: tuple[str, ...] = (
    "ollama",
    "vllm",
    "llama-server",
    "llama_server",
    "text-generation",
    "lm studio",
    "lmstudio",
    "lms",
    "sglang",
)

PROBE_TIMEOUT_S = 1.5


@dataclass(slots=True)
class Listener:
    port: int
    #: The address the socket is bound to; it decides the endpoint to probe.
    address: str
    process: str | None = None
    #: Named by the operator with --endpoint rather than found listening here.
    named: bool = False

    @property
    def bind(self) -> str:
        """'loopback', 'all-interfaces', 'network', or 'not-observed' for a named endpoint."""
        if self.named:
            return "not-observed"
        if self.address in ("0.0.0.0", "::", ""):
            return "all-interfaces"
        try:
            return "loopback" if ipaddress.ip_address(self.address).is_loopback else "network"
        except ValueError:
            return "network"

    @property
    def base_url(self) -> str:
        if self.bind == "all-interfaces":
            return f"http://127.0.0.1:{self.port}"
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{host}:{self.port}"


def listening_ports() -> tuple[dict[int, Listener], str | None]:
    """Every TCP port listened on, with the owning process name. (listeners, reason)."""
    import psutil

    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return {}, "No permission to enumerate listening sockets on this host."
    except Exception as exc:  # noqa: BLE001 - discovery degrades, it never fails a run
        return {}, f"Could not enumerate listening sockets: {exc}"

    listeners: dict[int, Listener] = {}
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        port = connection.laddr.port
        name = None
        if connection.pid:
            try:
                name = psutil.Process(connection.pid).name()
            except psutil.Error:
                name = None
        current = listeners.get(port)
        listener = Listener(port=port, address=connection.laddr.ip, process=name)
        # A port bound both to 127.0.0.1 and to 0.0.0.0 is reachable from the
        # network: keep the widest scope.
        if current is None or listener.bind == "all-interfaces":
            listeners[port] = listener
    return listeners, None


def discover_environment(
    *, endpoints: Iterable[str] = (), environment: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The real scan: this host's listeners, plus any endpoint the operator names."""
    from opaas.core.report import describe_environment

    listeners, note = listening_ports()
    return scan(
        listeners,
        endpoints=endpoints,
        environment=environment if environment is not None else describe_environment(),
        listener_note=note,
    )


def scan(
    listeners: dict[int, Listener],
    *,
    endpoints: Iterable[str] = (),
    environment: dict[str, Any],
    listener_note: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Probes the candidate listeners and named endpoints. Pure given its inputs."""
    candidates: dict[str, Listener] = {}
    for port, listener in sorted(listeners.items()):
        name = (listener.process or "").lower()
        if port in DEFAULT_PORTS or any(hint in name for hint in ENGINE_PROCESS_HINTS):
            candidates[listener.base_url] = listener
    for endpoint in endpoints:
        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        if not parsed.hostname:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        candidates[f"{parsed.scheme}://{parsed.netloc}"] = Listener(
            port=port, address=parsed.hostname, named=True
        )

    engines: list[dict[str, Any]] = []
    unidentified: list[dict[str, Any]] = []
    # Building a client builds an SSL context, which is slow on some hosts:
    # with nothing to probe, there is no reason to pay for it.
    opened = (
        httpx.Client(timeout=PROBE_TIMEOUT_S, transport=transport)
        if candidates else contextlib.nullcontext()
    )
    with opened as client:
        for base_url, listener in candidates.items():
            engine = _identify(client, base_url)
            if engine is None:
                # Only an engine-looking process is worth mentioning; a random
                # service on port 8080 is none of the certificate's business.
                if any(hint in (listener.process or "").lower() for hint in ENGINE_PROCESS_HINTS):
                    unidentified.append(
                        {"port": listener.port, "process": listener.process,
                         "reason": "Listens like an engine but did not answer as one."}
                    )
                continue
            engines.append(
                {
                    **engine,
                    "endpoint": urlparse(base_url).netloc,
                    "port": listener.port,
                    "bind": listener.bind,
                    "process": listener.process,
                    # Nothing was sent but a GET: anything that answered did so
                    # without credentials.
                    "answeredWithoutCredentials": True,
                }
            )

    gpus = environment.get("gpus") or []
    inventory: dict[str, Any] = {
        "available": True,
        "scope": "this host, plus endpoints named with --endpoint",
        "engines": _merge_mirrors(engines),
        "unidentified": unidentified,
        "gpus": [str(gpu.get("name")) for gpu in gpus],
        "gpuDrivers": sorted({str(g["driverVersion"]) for g in gpus if g.get("driverVersion")}),
        "notes": [n for n in (listener_note, environment.get("gpuTelemetryNote")) if n],
    }
    inventory["hash"] = inventory_hash(inventory)
    return inventory


def _merge_mirrors(engines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One engine answering on several ports is one engine.

    The Ollama desktop app answers the same API on an ephemeral port next to
    11434, with the same models. Counted twice, the inventory would change on
    every restart and the sentinel would report drift that never happened. The
    primary is the default port if there is one, else the lowest; the others
    are kept under `alsoAnswersOn` and stay out of the hash.
    """
    def identity(engine: dict[str, Any]) -> tuple:
        return (
            engine.get("kind"),
            engine.get("version"),
            tuple(sorted((str(m.get("name")), str(m.get("digest")))
                         for m in engine.get("models", []))),
        )

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for engine in engines:
        groups.setdefault(identity(engine), []).append(engine)

    merged = []
    for group in groups.values():
        group.sort(key=lambda e: (e["port"] not in DEFAULT_PORTS, e["port"]))
        primary, mirrors = group[0], group[1:]
        if mirrors:
            primary = {
                **primary,
                "alsoAnswersOn": [
                    {"port": m["port"], "bind": m["bind"], "process": m["process"]}
                    for m in mirrors
                ],
            }
        merged.append(primary)
    return sorted(merged, key=lambda e: (str(e["kind"]), int(e["port"])))


def inventory_hash(inventory: dict[str, Any]) -> str:
    """Over what identifies the environment; never over what is loaded right now."""
    return sha256_json(
        {
            "engines": sorted(
                (
                    {
                        "kind": e.get("kind"),
                        "version": e.get("version"),
                        "port": e.get("port"),
                        "bind": e.get("bind"),
                        "models": sorted(
                            ({"name": m.get("name"), "digest": m.get("digest")}
                             for m in e.get("models", [])),
                            key=lambda m: str(m["name"]),
                        ),
                    }
                    for e in inventory.get("engines", [])
                ),
                key=lambda e: (str(e["kind"]), int(e["port"] or 0)),
            ),
            "gpus": sorted(inventory.get("gpus", [])),
            "gpuDrivers": inventory.get("gpuDrivers", []),
        }
    )


# ------------------------------------------------------------------ identify


def _get(client: httpx.Client, url: str) -> Any:
    try:
        response = client.get(url)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _identify(client: httpx.Client, base_url: str) -> dict[str, Any] | None:
    """Ollama first (its native API is unambiguous), then vLLM, then anything OpenAI-like."""
    version = _get(client, f"{base_url}/api/version")
    if isinstance(version, dict) and "version" in version:
        tags = _get(client, f"{base_url}/api/tags") or {}
        return {
            "kind": "ollama",
            "version": str(version["version"]),
            "models": [_ollama_model(m) for m in tags.get("models", []) if isinstance(m, dict)],
        }

    listed = _get(client, f"{base_url}/v1/models")
    if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
        return None
    vllm = _get(client, f"{base_url}/version")
    is_vllm = isinstance(vllm, dict) and "version" in vllm
    return {
        "kind": "vllm" if is_vllm else "openai-compatible",
        "version": str(vllm["version"]) if is_vllm else None,
        "models": [_openai_model(m) for m in listed["data"] if isinstance(m, dict)],
    }


def _ollama_model(entry: dict[str, Any]) -> dict[str, Any]:
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    digest = entry.get("digest")
    return {
        "name": entry.get("name"),
        "digest": f"sha256:{digest}" if digest and not str(digest).startswith("sha256:")
        else digest,
        "quantization": details.get("quantization_level"),
        "parameterSize": details.get("parameter_size"),
        "sizeBytes": entry.get("size"),
    }


def _openai_model(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry.get("id"),
        "digest": None,
        "maxModelLen": entry.get("max_model_len"),
    }


# ------------------------------------------------------------------- compare


def compare_inventories(certified: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """What changed on the host since certification, engine by engine and model by model."""

    def engines(inventory: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
        return {(str(e.get("kind")), int(e.get("port") or 0)): e
                for e in inventory.get("engines", [])}

    def models(engine: dict[str, Any]) -> dict[str, Any]:
        return {str(m.get("name")): m.get("digest") for m in engine.get("models", [])}

    then, now = engines(certified), engines(current)
    changes: dict[str, list[dict[str, Any]]] = {
        "enginesAdded": [], "enginesRemoved": [], "enginesUpgraded": [],
        "modelsAdded": [], "modelsRemoved": [], "modelsChanged": [], "bindChanged": [],
    }
    for key in sorted(now.keys() - then.keys()):
        changes["enginesAdded"].append({"kind": key[0], "port": key[1],
                                        "version": now[key].get("version")})
    for key in sorted(then.keys() - now.keys()):
        changes["enginesRemoved"].append({"kind": key[0], "port": key[1]})
    for key in sorted(then.keys() & now.keys()):
        a, b = then[key], now[key]
        where = {"kind": key[0], "port": key[1]}
        if a.get("version") != b.get("version"):
            changes["enginesUpgraded"].append(
                {**where, "certified": a.get("version"), "current": b.get("version")})
        if a.get("bind") != b.get("bind"):
            changes["bindChanged"].append(
                {**where, "certified": a.get("bind"), "current": b.get("bind")})
        old, new = models(a), models(b)
        changes["modelsAdded"] += [{**where, "name": n} for n in sorted(new.keys() - old.keys())]
        changes["modelsRemoved"] += [{**where, "name": n} for n in sorted(old.keys() - new.keys())]
        changes["modelsChanged"] += [
            {**where, "name": n, "certified": old[n], "current": new[n]}
            for n in sorted(old.keys() & new.keys())
            if old[n] and new[n] and old[n] != new[n]
        ]
    return {k: v for k, v in changes.items() if v}


__all__ = [
    "DEFAULT_PORTS",
    "ENGINE_PROCESS_HINTS",
    "Listener",
    "compare_inventories",
    "discover_environment",
    "inventory_hash",
    "listening_ports",
    "scan",
]
