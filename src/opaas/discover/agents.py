"""The agent stack around the engines: MCP servers, tools, skills, containers (D45).

The engine inventory (`inventory.py`) says which models are served. An agent
deployment is more than that: the tools it can call, the MCP servers that
expose them, the skills it loads, the containers it runs in. Each is a place
an action can happen, and each is something a later result is bound to (N5).

**Only what the operator names, and this host's listeners.** OPAAS reads the
configuration files and manifest directories the operator points at; it never
crawls the disk looking for them. A config it was not given is not observed,
and the result says so under `notObserved`.

**Publishable by construction (invariant 12).** From an MCP server entry it
keeps the server name, the transport, the launcher's *basename*, a package
identifier when one is recognisable, the *names* of environment variables and
the scheme, host and port of a URL. Never argument values, environment values,
URL paths or query strings: those are where tokens live. Each entry carries a
hash computed with environment values masked, so a change is detected without
publishing what changed.

**Skill signatures, in one scheme OPAAS can check (D64).** A skill signed
with `sign_skill` carries a `signature.json`: an Ed25519 signature over the
skill's content hash, the hash excluding the signature files themselves. The
inventory records the signature as found; the gap scan (G05) verifies it
against keys the operator names, never against a key the signature brings.
Signatures in other schemes (`.sig`, `.asc`) are listed with their scheme
unknown: OPAAS cannot tell a valid one from a forged one, and says so.

**Containers by digest.** When `docker` or `podman` answers, running
containers are listed with the image's repository digest. A container running
from a tag alone is listed as such: a tag is a name, and a name can be
re-pushed onto a different image.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from opaas.core.canonical import canonicalize
from opaas.core.credential.keys import b58decode, b58encode, fingerprint
from opaas.core.hashing import sha256_file, sha256_json
from opaas.discover.inventory import Listener

#: Process names that suggest an agent runtime or MCP server is listening.
AGENT_PROCESS_HINTS: tuple[str, ...] = (
    "mcp",
    "langgraph",
    "langserve",
    "openhands",
    "autogen",
    "crewai",
    "letta",
    "n8n",
    "flowise",
    "dify",
    "open-webui",
)

#: A package identifier as `npx`/`uvx`/`pipx` take it, possibly with a version.
_PACKAGE = re.compile(r"^(@[\w.-]+/)?[\w.-]+((@|==)[\w.^~*+-]+)?$")
_PINNED = re.compile(r"(@|==)\d[\w.+-]*$")
_LAUNCHERS = {"npx", "uvx", "pipx", "bunx", "pnpm", "docker", "podman", "node", "python",
              "python3", "uv", "deno"}

#: Free-form tools: their arguments are a program, so no structural policy can
#: tell a harmless call from a harmful one (gap rule G03).
_SHELL_WORDS = re.compile(
    r"\b(shell|bash|sh|zsh|exec|execute|run_command|terminal|subprocess|powershell|cmd)\b", re.I
)
_HTTP_WORDS = re.compile(r"\b(http|https|fetch|request|curl|wget|browse|url)\b", re.I)
_SHELL_PARAMS = {"command", "cmd", "script", "code", "shell"}
_HTTP_PARAMS = {"url", "uri", "endpoint", "href"}

Runner = Callable[[list[str]], str | None]


# ------------------------------------------------------------------ MCP configs


def read_mcp_config(path: Path) -> dict[str, Any]:
    """The MCP servers one named config file declares, without anything secret."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"file": path.name, "readable": False, "reason": str(exc), "servers": []}
    servers = (
        raw.get("mcpServers")
        or raw.get("servers")
        or (raw.get("mcp") or {}).get("servers")
        or {}
    ) if isinstance(raw, dict) else {}
    return {
        "file": path.name,
        "fileHash": "sha256:" + sha256_file(path),
        "readable": True,
        "servers": [
            _server(str(name), entry)
            for name, entry in sorted(servers.items())
            if isinstance(entry, dict)
        ],
    }


def _server(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    url = entry.get("url") or entry.get("serverUrl")
    transport = str(entry.get("type") or entry.get("transport") or
                    ("http" if url else "stdio")).lower()
    server: dict[str, Any] = {"name": name, "transport": transport}
    if url:
        parsed = urlparse(str(url))
        server["endpoint"] = f"{parsed.scheme}://{parsed.hostname}" + (
            f":{parsed.port}" if parsed.port else "")
    command = entry.get("command")
    if command:
        launcher = Path(str(command)).name.lower().removesuffix(".exe").removesuffix(".cmd")
        server["launcher"] = launcher
        package = next(
            (str(a) for a in entry.get("args") or []
             if isinstance(a, str) and not a.startswith("-") and _PACKAGE.match(a)),
            None,
        ) if launcher in _LAUNCHERS else None
        if package:
            server["package"] = package
            server["versionPinned"] = bool(_PINNED.search(package))
    env = entry.get("env") or {}
    if isinstance(env, dict) and env:
        server["envNames"] = sorted(str(k) for k in env)
    masked = {**entry, "env": {k: "***" for k in env} if isinstance(env, dict) else env}
    server["entryHash"] = sha256_json(masked)
    return server


# --------------------------------------------------------------- tool manifests


def read_tool_manifest(path: Path) -> dict[str, Any]:
    """Tools from an MCP `tools/list` result or an OpenAI function list."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"file": path.name, "readable": False, "reason": str(exc), "tools": []}
    items = raw.get("tools") if isinstance(raw, dict) else raw
    tools = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        definition = item.get("function") if isinstance(item.get("function"), dict) else item
        name = definition.get("name")
        if not name:
            continue
        schema = definition.get("inputSchema") or definition.get("parameters") or {}
        tools.append({
            "name": str(name),
            "definitionHash": sha256_json(definition),
            "freeForm": classify_tool(str(name), str(definition.get("description", "")),
                                      schema),
        })
    return {"file": path.name, "fileHash": "sha256:" + sha256_file(path), "readable": True,
            "tools": sorted(tools, key=lambda t: t["name"])}


def classify_tool(name: str, description: str, schema: Any) -> str | None:
    """'shell', 'http' or None. Deterministic: names, descriptions and parameter names.

    A tool counts as free-form when its arguments are themselves a program or
    an arbitrary address. A parameter with an `enum` is not free.
    """
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    free_params = {
        str(key).lower() for key, spec in properties.items()
        if isinstance(spec, dict) and spec.get("type") in (None, "string") and "enum" not in spec
    }
    text = f"{name.replace('_', ' ')} {description}"
    if free_params & _SHELL_PARAMS or (_SHELL_WORDS.search(text) and free_params):
        return "shell"
    if free_params & _HTTP_PARAMS and _HTTP_WORDS.search(text + " " + " ".join(free_params)):
        return "http"
    return None


# ----------------------------------------------------------------------- skills


#: The one skill-signature scheme OPAAS verifies, and the file that carries it.
SKILL_SIGNATURE_SCHEME = "opaas-skill-ed25519-jcs"
SKILL_SIGNATURE_FILE = "signature.json"


def _is_signature_file(path: Path) -> bool:
    return path.suffix in (".sig", ".asc") or path.name in (SKILL_SIGNATURE_FILE, "SIGNATURE")


def skill_hash(directory: Path) -> str:
    """The hash a skill is identified, and signed, by: every file but its signatures.

    A signature cannot cover itself, so every signature file is left out, the
    one OPAAS reads and those it cannot. Adding or replacing a signature does
    not change what the skill is.
    """
    files = sorted(p for p in directory.rglob("*") if p.is_file() and not _is_signature_file(p))
    return sha256_json([
        {"path": p.relative_to(directory).as_posix(), "sha256": sha256_file(p)} for p in files
    ])


def _signed_payload(content_hash: str) -> bytes:
    return canonicalize({"scheme": SKILL_SIGNATURE_SCHEME, "skillHash": content_hash})


def sign_skill(directory: Path, key: Ed25519PrivateKey) -> dict[str, Any]:
    """Writes `signature.json` for the skill in `directory`, and returns it.

    The signature names its key by fingerprint so a reader can tell which key
    to try; the key itself is never in it, because a verifier that took the
    key from the signature would accept anyone's (invariant 6, D4).
    """
    if not ((directory / "SKILL.md").is_file() or (directory / "skill.json").is_file()):
        raise ValueError(f"{directory} is not a skill: it has neither SKILL.md nor skill.json.")
    content_hash = skill_hash(directory)
    signature = {
        "scheme": SKILL_SIGNATURE_SCHEME,
        "skillHash": content_hash,
        "keyFingerprint": fingerprint(key.public_key()),
        "proofValue": "z" + b58encode(key.sign(_signed_payload(content_hash))),
    }
    with (directory / SKILL_SIGNATURE_FILE).open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(signature, indent=2) + "\n")
    return signature


def verify_skill_signature(
    skill: dict[str, Any], keys: Iterable[Ed25519PublicKey]
) -> tuple[str, str]:
    """('verified' | 'invalid' | 'unverifiable' | 'absent', why) for one inventoried skill.

    Works on the inventory alone: the skill's hash there was computed from
    the disk at discovery, and the signature must be over exactly that hash.
    `unverifiable` is OPAAS not being able to look (an unknown scheme, no key
    named), never a pass.
    """
    signature = skill.get("signature")
    if signature is None:
        if skill.get("signaturePresent"):
            return "unverifiable", ("A signature file is present, but this inventory predates "
                                    "signature verification: scan again to check it.")
        return "absent", "No signature: its origin cannot be checked."
    if signature.get("scheme") != SKILL_SIGNATURE_SCHEME:
        return "unverifiable", (
            f"{signature.get('file')} is a signature in a scheme OPAAS does not verify "
            f"({signature.get('scheme')}): whether it is valid was not looked at.")
    if signature.get("readable") is False:
        return "invalid", (f"{signature.get('file')} cannot be read as a signature "
                           f"({signature.get('reason')}): its origin cannot be checked.")
    if signature.get("skillHash") != skill.get("hash"):
        return "invalid", ("The signature covers different content from what is on disk: "
                           "the skill changed after it was signed.")
    keys = list(keys)
    if not keys:
        return "unverifiable", (
            "Signed, but no trusted key was named to verify it with. The key is never "
            "taken from the signature.")
    try:
        raw = b58decode(str(signature.get("proofValue", "")).removeprefix("z"))
    except ValueError:
        return "invalid", "The signature value is not valid base58."
    for key in keys:
        try:
            key.verify(raw, _signed_payload(str(skill["hash"])))
        except InvalidSignature:
            continue
        return "verified", f"Signature verifies under the key {fingerprint(key)}."
    return "invalid", (f"The signature does not verify under any of the {len(keys)} named "
                       "key(s): signed by another key, or altered.")


def _read_signature(directory: Path) -> dict[str, Any] | None:
    """The signature as found. Publishable: what a signature carries is public."""
    path = directory / SKILL_SIGNATURE_FILE
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"file": path.name, "scheme": SKILL_SIGNATURE_SCHEME, "readable": False,
                    "reason": type(exc).__name__}
        if isinstance(raw, dict) and raw.get("scheme") == SKILL_SIGNATURE_SCHEME:
            fields = ("skillHash", "keyFingerprint", "proofValue")
            if all(isinstance(raw.get(f), str) for f in fields):
                return {"file": path.name, "scheme": SKILL_SIGNATURE_SCHEME,
                        **{f: raw[f] for f in fields}}
            return {"file": path.name, "scheme": SKILL_SIGNATURE_SCHEME, "readable": False,
                    "reason": f"missing one of {', '.join(fields)}"}
        scheme = raw.get("scheme") if isinstance(raw, dict) else None
        return {"file": path.name, "scheme": str(scheme or "unknown")}
    other = sorted(p for p in directory.rglob("*") if p.is_file() and _is_signature_file(p))
    if other:
        return {"file": other[0].relative_to(directory).as_posix(),
                "scheme": "openpgp" if other[0].suffix == ".asc" else "unknown"}
    return None


def read_skills(root: Path) -> list[dict[str, Any]]:
    """Every skill under a named directory: one with SKILL.md or skill.json.

    `signaturePresent` says a signature file sits next to the skill;
    `signature` says what it is. Whether it verifies is the gap scan's
    question (G05), asked with the keys the operator trusts.
    """
    if not root.is_dir():
        return []
    skills = []
    for marker in sorted([*root.rglob("SKILL.md"), *root.rglob("skill.json")]):
        directory = marker.parent
        files = sorted(p for p in directory.rglob("*") if p.is_file())
        version = _skill_version(marker)
        signature = _read_signature(directory)
        skill = {
            "name": directory.name,
            "path": directory.relative_to(root).as_posix() or ".",
            "version": version,
            "versionPinned": version is not None,
            "signaturePresent": signature is not None,
            "hash": skill_hash(directory),
            "fileCount": len(files),
        }
        if signature is not None:
            skill["signature"] = signature
        skills.append(skill)
    return skills


def _skill_version(marker: Path) -> str | None:
    try:
        text = marker.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    if marker.suffix == ".json":
        try:
            version = json.loads(text).get("version")
        except (json.JSONDecodeError, AttributeError):
            return None
        return str(version) if version else None
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return None
    found = re.search(r"^version:\s*['\"]?([^'\"\n]+)", match.group(1), re.M)
    return found.group(1).strip() if found else None


# ------------------------------------------------------------------- containers


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                   check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def list_containers(runner: Runner | None = None) -> dict[str, Any]:
    """Running containers with their image digests, from docker or podman."""
    runner = runner or _run
    for tool in ("docker", "podman"):
        listing = runner([tool, "ps", "--no-trunc", "--format", "{{json .}}"])
        if listing is None:
            continue
        containers = []
        for line in listing.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            image = str(item.get("Image", ""))
            digests = runner([tool, "image", "inspect", "--format", "{{json .RepoDigests}}",
                              image])
            try:
                repo_digests = json.loads(digests) if digests else []
            except json.JSONDecodeError:
                repo_digests = []
            containers.append({
                "name": str(item.get("Names", "")),
                "image": image,
                "imageDigest": sorted(repo_digests)[0] if repo_digests else None,
                "pinnedByDigest": "@sha256:" in image,
                "ports": str(item.get("Ports", "")),
            })
        return {"available": True, "runtime": tool,
                "containers": sorted(containers, key=lambda c: c["name"])}
    return {"available": False,
            "reason": "Neither docker nor podman answered on this host."}


# ----------------------------------------------------------------- agent stack


def agent_runtimes(listeners: dict[int, Listener]) -> list[dict[str, Any]]:
    """Listeners whose process name suggests an agent runtime or MCP server.

    Listed here from the process name alone. `discover` then probes each with
    one GET to `/` (`identify.scan_runtimes`, D64): enough to tell whether it
    speaks HTTP and asks for credentials, never a request that could act.
    """
    return [
        {"port": port, "process": listener.process, "bind": listener.bind,
         "identified": False}
        for port, listener in sorted(listeners.items())
        if any(hint in (listener.process or "").lower() for hint in AGENT_PROCESS_HINTS)
    ]


def discover_agent_stack(
    *,
    configs: Iterable[Path] = (),
    manifests: Iterable[Path] = (),
    skill_dirs: Iterable[Path] = (),
    listeners: dict[int, Listener] | None = None,
    containers: bool = True,
    runner: Runner | None = None,
) -> dict[str, Any]:
    configs, manifests, skill_dirs = list(configs), list(manifests), list(skill_dirs)
    stack: dict[str, Any] = {
        "mcpConfigs": [read_mcp_config(p) for p in configs],
        "toolManifests": [read_tool_manifest(p) for p in manifests],
        "skills": [
            {"root": p.name, **({"skills": read_skills(p)} if p.is_dir()
                                else {"readable": False, "reason": "Not a directory."})}
            for p in skill_dirs
        ],
        "agentRuntimes": agent_runtimes(listeners or {}),
        "containers": list_containers(runner) if containers
        else {"available": False, "reason": "Container listing was not requested."},
    }
    not_observed = [
        "Services on other hosts, unless named with --endpoint.",
        "Configuration files and manifests that were not named.",
    ]
    if not configs:
        not_observed.append("MCP servers: no MCP configuration file was named.")
    if not manifests:
        not_observed.append("Tool definitions: no tool manifest was named.")
    if not skill_dirs:
        not_observed.append("Skills: no skill directory was named.")
    if not stack["containers"].get("available"):
        not_observed.append(f"Containers: {stack['containers']['reason']}")
    if listeners is None:
        not_observed.append("Agent runtimes: listening sockets were not enumerated.")
    stack["notObserved"] = not_observed
    stack["hash"] = agent_stack_hash(stack)
    return stack


def agent_stack_hash(stack: dict[str, Any]) -> str:
    """Over what identifies the stack. Ports of agent runtimes are left out: they move."""
    agents = sorted(
        (str(a.get("kind")), str(a.get("name")), str(a.get("endpoint")))
        for a in stack.get("agentEndpoints", [])
    )
    return sha256_json({
        # Only when there are any: an inventory with no identified agent keeps
        # the hash it had before agents were identified (D63).
        **({"agents": agents} if agents else {}),
        "mcp": sorted(
            (s["name"], s["entryHash"])
            for c in stack.get("mcpConfigs", []) for s in c.get("servers", [])
        ),
        "tools": sorted(
            (t["name"], t["definitionHash"])
            for m in stack.get("toolManifests", []) for t in m.get("tools", [])
        ),
        "skills": sorted(
            (s["path"], s["hash"])
            for root in stack.get("skills", []) for s in root.get("skills", [])
        ),
        "containers": sorted(
            (c["name"], c["imageDigest"] or c["image"])
            for c in (stack.get("containers") or {}).get("containers", [])
        ),
    })


__all__ = [
    "AGENT_PROCESS_HINTS",
    "SKILL_SIGNATURE_FILE",
    "SKILL_SIGNATURE_SCHEME",
    "agent_runtimes",
    "agent_stack_hash",
    "classify_tool",
    "discover_agent_stack",
    "list_containers",
    "read_mcp_config",
    "read_skills",
    "read_tool_manifest",
    "sign_skill",
    "skill_hash",
    "verify_skill_signature",
]
