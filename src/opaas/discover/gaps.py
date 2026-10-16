"""The auditability gap scan: deterministic rules over what discovery found (D45).

*Auditable Agents* calls this OP1: detect, before deployment, the missing
preconditions for auditing. Each rule here looks at the inventory, the agent
stack, and the logs the operator names, and answers one question per subject:

- `gap`: a precondition for auditing is missing. **A gap is not a predicted
  violation**, and nothing here says the deployment misbehaves; it says that
  if it did, some part of the audit could not tell.
- `ok`: the precondition is observed to hold.
- `unsupported`: the operator declares it holds, and nothing observed backs
  the declaration (D14). Declared and observed are never added together.
- `not-observed`: discovery could not look. Never silently counted as `ok`.

Every finding names the dimension it weakens and the Auditability Card
question (Q1–Q6) it bears on, so `card` can cite it.

The rule set is versioned and hashed like the refusal patterns (D11): a scan
says which rules produced it, and two scans under different rule sets are not
compared as if they were the same instrument.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.events import read_log, records
from opaas.core.hashing import sha256_json
from opaas.core.integrity import assess_log
from opaas.core.metrics import integrity_rank
from opaas.discover.agents import verify_skill_signature

RULESET_VERSION = 2

GAP, OK, UNSUPPORTED, NOT_OBSERVED = "gap", "ok", "unsupported", "not-observed"


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    dimension: str
    question: str
    statement: str


RULES: tuple[Rule, ...] = (
    Rule("G01", "actionRecoverability", "Q1",
         "Tool calls pass through a logging layer that records them."),
    Rule("G02", "evidenceIntegrity", "Q5",
         "The agent's logs are at least hash-chained."),
    Rule("G03", "policyCheckability", "Q3",
         "Tools take structured arguments, not free-form shell or HTTP."),
    Rule("G04", "responsibilityAttribution", "Q4",
         "Skills and MCP packages are pinned to a version."),
    Rule("G05", "evidenceIntegrity", "Q5",
         "Skills carry a signature that verifies under a key the operator trusts."),
    Rule("G06", "lifecycleCoverage", "Q2",
         "Approvals are represented as events in the log."),
    Rule("G07", "responsibilityAttribution", "Q4",
         "No engine answers anonymous callers beyond this host."),
    Rule("G08", "responsibilityAttribution", "Q4",
         "Logged actions carry an actor chain."),
    Rule("G09", "evidenceIntegrity", "Q5",
         "Containers run from an image pinned by digest."),
)


def ruleset_hash() -> str:
    return sha256_json({"version": RULESET_VERSION,
                        "rules": [[r.id, r.dimension, r.question, r.statement] for r in RULES]})


def _finding(rule_id: str, status: str, subject: str, detail: str,
             basis: str = "observed") -> dict[str, Any]:
    rule = next(r for r in RULES if r.id == rule_id)
    return {"rule": rule.id, "status": status, "dimension": rule.dimension,
            "question": rule.question, "subject": subject, "detail": detail, "basis": basis}


def scan_gaps(
    discovered: dict[str, Any],
    *,
    logs: Iterable[Path] = (),
    public_key: Ed25519PublicKey | None = None,
    declaration: dict[str, Any] | None = None,
    logging_endpoints: Iterable[str] = (),
    skill_keys: Iterable[Ed25519PublicKey] = (),
) -> dict[str, Any]:
    """Every rule against the discovery document and the named logs.

    `skill_keys` are the keys the operator trusts to sign skills (G05). With
    none, a signed skill is `not-observed`: OPAAS looked at the signature and
    had nothing to check it against.
    """
    declaration = declaration or {}
    logs = list(logs)
    stack = discovered.get("agentStack") or {}
    loaded_logs = [_load(path, public_key, declaration) for path in logs]
    context = _Context(discovered, stack, loaded_logs, declaration,
                       {e.lower().rstrip("/") for e in logging_endpoints}, list(skill_keys))

    findings: list[dict[str, Any]] = []
    for check in _CHECKS:
        findings += check(context)

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["status"]] = counts.get(finding["status"], 0) + 1
    return {
        "ruleset": {"version": RULESET_VERSION, "hash": ruleset_hash(),
                    "rules": [{"id": r.id, "dimension": r.dimension, "question": r.question,
                               "statement": r.statement} for r in RULES]},
        "inventoryHash": discovered.get("hash"),
        "logs": [entry["integrity"] for entry in loaded_logs],
        "findings": findings,
        "counts": dict(sorted(counts.items())),
        "note": "A gap is a missing precondition for auditing, never a predicted violation.",
    }


@dataclass(slots=True)
class _Context:
    discovered: dict[str, Any]
    stack: dict[str, Any]
    logs: list[dict[str, Any]]
    declaration: dict[str, Any]
    logging_endpoints: set[str]
    skill_keys: list[Ed25519PublicKey]

    def servers(self) -> list[dict[str, Any]]:
        return [s for c in self.stack.get("mcpConfigs", []) for s in c.get("servers", [])]

    def tools(self) -> list[dict[str, Any]]:
        return [t for m in self.stack.get("toolManifests", []) for t in m.get("tools", [])]

    def skills(self) -> list[dict[str, Any]]:
        return [s for root in self.stack.get("skills", []) for s in root.get("skills", [])]


def _load(path: Path, key: Ed25519PublicKey | None, declaration: dict[str, Any]):
    append_only = path.name in set(declaration.get("appendOnlyLogs") or [])
    integrity = assess_log(path, public_key=key, declared_append_only=append_only)
    events: list[dict[str, Any]] | None = None
    if integrity.get("format") == "opaas-event-log":
        events = list(records(read_log(path)[0]))
    elif integrity.get("format") == "jsonl":
        events = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
                  if line.strip()]
        events = [e for e in events if isinstance(e, dict)]
    return {"path": path, "integrity": integrity, "events": events}


# ------------------------------------------------------------------------ rules


def _g01_tool_logging(ctx: _Context) -> list[dict[str, Any]]:
    servers = ctx.servers()
    declared = (ctx.declaration.get("toolLogging") or {}).get("layer")
    if not servers:
        return [_finding("G01", NOT_OBSERVED, "MCP servers",
                         "No MCP configuration was named, so no tool route was observed.")]
    out = []
    for server in servers:
        endpoint = str(server.get("endpoint") or "").lower()
        if endpoint and endpoint in ctx.logging_endpoints:
            out.append(_finding("G01", OK, server["name"],
                                f"Reached through {endpoint}, a named logging layer."))
        elif declared:
            out.append(_finding(
                "G01", UNSUPPORTED, server["name"],
                f"The operator declares tool calls are logged by '{declared}', but this "
                f"server's {server['transport']} route does not pass through a named "
                "logging endpoint.", basis="declared"))
        else:
            out.append(_finding(
                "G01", GAP, server["name"],
                f"A {server['transport']} server reached directly: no logging layer was "
                "observed between the agent and its tools, so a tool call leaves no record "
                "outside the agent's own."))
    return out


def _g02_log_integrity(ctx: _Context) -> list[dict[str, Any]]:
    if not ctx.logs:
        return [_finding("G02", NOT_OBSERVED, "agent logs",
                         "No agent log was named: its integrity could not be assessed.")]
    out = []
    for entry in ctx.logs:
        info = entry["integrity"]
        level = info.get("level")
        name = entry["path"].name
        if level is None:
            out.append(_finding("G02", NOT_OBSERVED, name, "; ".join(info["problems"])))
        elif integrity_rank(level) >= integrity_rank("hash-chained"):
            out.append(_finding("G02", OK, name, f"Integrity level found: {level}."))
        else:
            problems = f" Problems: {'; '.join(info['problems'])}" if info["problems"] else ""
            out.append(_finding(
                "G02", GAP, name,
                f"Integrity level found: {level}"
                + (" (declared, not observed)" if level == "append-only" else "")
                + ". Any record in it can be altered or removed without trace." + problems))
    return out


def _g03_free_form(ctx: _Context) -> list[dict[str, Any]]:
    tools = ctx.tools()
    if not tools:
        return [_finding("G03", NOT_OBSERVED, "tools",
                         "No tool manifest was named: tool arguments were not inspected.")]
    out = []
    for tool in tools:
        if tool.get("freeForm"):
            out.append(_finding(
                "G03", GAP, tool["name"],
                f"Free-form {tool['freeForm']} tool: its argument is a program or an "
                "arbitrary address, so no structural policy can decide what a call does."))
        else:
            out.append(_finding("G03", OK, tool["name"], "Structured arguments."))
    return out


def _g04_pinning(ctx: _Context) -> list[dict[str, Any]]:
    out = []
    for skill in ctx.skills():
        out.append(_finding(
            "G04", OK if skill["versionPinned"] else GAP, f"skill {skill['path']}",
            f"Pinned at version {skill['version']}." if skill["versionPinned"]
            else "No version: the code that acted cannot be named afterwards (OP2)."))
    for server in ctx.servers():
        if "package" in server:
            out.append(_finding(
                "G04", OK if server["versionPinned"] else GAP, f"MCP {server['name']}",
                f"Package {server['package']}"
                + (" is pinned." if server["versionPinned"]
                   else " is fetched at its latest version on every start.")))
    return out or [_finding("G04", NOT_OBSERVED, "skills and packages",
                            "No skill directory or packaged MCP server was named.")]


def _g05_signed_skills(ctx: _Context) -> list[dict[str, Any]]:
    skills = ctx.skills()
    if not skills:
        return [_finding("G05", NOT_OBSERVED, "skills", "No skill directory was named.")]
    # Verified is ok; absent or failing is a gap; a signature OPAAS could not
    # check (unknown scheme, no trusted key named) is not observed, never ok.
    status = {"verified": OK, "invalid": GAP, "absent": GAP, "unverifiable": NOT_OBSERVED}
    out = []
    for skill in skills:
        outcome, detail = verify_skill_signature(skill, ctx.skill_keys)
        out.append(_finding("G05", status[outcome], f"skill {skill['path']}", detail))
    return out


def _g06_approvals(ctx: _Context) -> list[dict[str, Any]]:
    readable = [e for e in ctx.logs if e["events"] is not None]
    if not readable:
        return [_finding("G06", NOT_OBSERVED, "approvals",
                         "No readable agent log was named: approvals could not be looked for.")]
    seen = any(
        e.get("action") == "approval" or e.get("phase") in ("approval", "pending")
        for entry in readable for e in entry["events"]
    )
    if seen:
        return [_finding("G06", OK, "approvals", "Approval events are present in the log.")]
    if ctx.declaration.get("approvals"):
        return [_finding(
            "G06", UNSUPPORTED, "approvals",
            "The operator declares human approvals exist, but no approval or pending event "
            "appears in the named logs.", basis="declared")]
    return [_finding(
        "G06", GAP, "approvals",
        "No approval or pending event in the named logs: if approvals happen, they leave no "
        "record, and a policy requiring one is undecidable.")]


def _g07_anonymous_engines(ctx: _Context) -> list[dict[str, Any]]:
    engines = ctx.discovered.get("engines") or []
    if not ctx.discovered.get("available") or not engines:
        return [_finding("G07", NOT_OBSERVED, "engines", "No engine was found to assess.")]
    out = []
    for engine in engines:
        subject = f"{engine.get('kind')} :{engine.get('port')}"
        wide = engine.get("bind") in ("all-interfaces", "network")
        if engine.get("answeredWithoutCredentials") and wide:
            out.append(_finding(
                "G07", GAP, subject,
                f"Answers without credentials on {engine.get('bind')}: a call to it cannot "
                "be attributed to any principal."))
        elif engine.get("bind") == "not-observed":
            out.append(_finding("G07", NOT_OBSERVED, subject,
                                "A named remote endpoint: its bind scope cannot be seen."))
        else:
            out.append(_finding("G07", OK, subject, f"Bound to {engine.get('bind')}."))
    return out


def _g08_actor_chains(ctx: _Context) -> list[dict[str, Any]]:
    readable = [e for e in ctx.logs if e["events"]]
    if not readable:
        return [_finding("G08", NOT_OBSERVED, "actor chains",
                         "No readable agent log with events was named.")]
    out = []
    for entry in readable:
        total = len(entry["events"])
        with_chain = sum(1 for e in entry["events"] if isinstance(e.get("actorChain"), list))
        out.append(_finding(
            "G08", OK if with_chain == total else GAP, entry["path"].name,
            f"{with_chain} of {total} event(s) carry an actor chain."))
    return out


def _g09_container_digests(ctx: _Context) -> list[dict[str, Any]]:
    containers = ctx.stack.get("containers") or {}
    if not containers.get("available"):
        return [_finding("G09", NOT_OBSERVED, "containers",
                         str(containers.get("reason") or "Containers were not listed."))]
    return [
        _finding("G09", OK if c["pinnedByDigest"] else GAP, f"container {c['name']}",
                 f"Runs {c['image']}." if c["pinnedByDigest"]
                 else f"Runs {c['image']} by tag; the image it resolved to is "
                 f"{c['imageDigest'] or 'unknown'}, and the tag can be re-pushed.")
        for c in containers.get("containers", [])
    ]


_CHECKS: tuple[Callable[[_Context], list[dict[str, Any]]], ...] = (
    _g01_tool_logging,
    _g02_log_integrity,
    _g03_free_form,
    _g04_pinning,
    _g05_signed_skills,
    _g06_approvals,
    _g07_anonymous_engines,
    _g08_actor_chains,
    _g09_container_digests,
)


__all__ = [
    "GAP",
    "NOT_OBSERVED",
    "OK",
    "RULES",
    "RULESET_VERSION",
    "UNSUPPORTED",
    "Rule",
    "ruleset_hash",
    "scan_gaps",
]
