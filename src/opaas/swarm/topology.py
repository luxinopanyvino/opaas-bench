"""Flows of several models and agents: declared against observed (D51).

The operator declares the flow — agent A delegates to agent B, which calls
model X and tool T — and OPAAS observes the calls that actually happened,
through the sentinel proxy or imported traces. The two are compared and
never merged (D14): an observed edge nobody declared is a finding, and so is
a declared edge nothing used.

Nodes are `role:id` (`agent:planner`, `model:qwen2.5:7b`, `tool:search`,
`user:*`). `*` matches any id, so `user:*` declares "some user", which is
what a flow usually means.

An edge is observed from a record's actor chain (each hop to the next) and
from the last hop to the record's target. A record without an actor chain
cannot place its call on any edge: it is counted as unattributed rather than
assigned to the likeliest caller.

Per edge: call count, latency (median with its distribution-free interval),
error rate (Wilson) and tokens when the record kept usage. No flow-level
score (invariant 1).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Any

from opaas.core.metrics import attribution_completeness
from opaas.core.stats import median_interval, wilson_interval

_TARGET_ROLE = {"model_call": "model", "tool_call": "tool", "delegation": "agent",
                "handoff": "agent"}


class TopologyError(ValueError):
    """The declared topology is malformed."""


def load_topology(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("edges"), list):
        raise TopologyError("A topology is an object with an 'edges' list of {from, to}.")
    edges = []
    for index, edge in enumerate(raw["edges"]):
        if not isinstance(edge, dict) or not edge.get("from") or not edge.get("to"):
            raise TopologyError(f"edges[{index}] needs 'from' and 'to'.")
        for end in ("from", "to"):
            if ":" not in str(edge[end]):
                raise TopologyError(f"edges[{index}].{end} must be role:id, got {edge[end]}.")
        edges.append({"from": str(edge["from"]), "to": str(edge["to"])})
    return {"edges": edges, "retryBudget": int(raw.get("retryBudget", 3))}


def node(role: str, ident: str) -> str:
    return f"{role}:{ident}"


def record_edges(record: dict[str, Any]) -> list[tuple[str, str]]:
    chain = record.get("actorChain")
    if not isinstance(chain, list) or not chain:
        return []
    hops = [node(str(h.get("role")), str(h.get("id"))) for h in chain if isinstance(h, dict)]
    edges = list(zip(hops, hops[1:], strict=False))
    role = _TARGET_ROLE.get(str(record.get("action")))
    if role and record.get("target"):
        edges.append((hops[-1], node(role, str(record["target"]))))
    return edges


def _declared(edge: tuple[str, str], declared: Sequence[dict[str, str]]) -> bool:
    return any(fnmatch.fnmatchcase(edge[0], d["from"]) and fnmatch.fnmatchcase(edge[1], d["to"])
               for d in declared)


def compare_topology(declared: dict[str, Any],
                     records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    observed: dict[tuple[str, str], int] = {}
    unattributed: dict[str, int] = {}
    for record in records:
        edges = record_edges(record)
        if not edges:
            if record.get("action") in _TARGET_ROLE:
                key = f"{record.get('action')} {record.get('target')}"
                unattributed[key] = unattributed.get(key, 0) + 1
            continue
        for edge in edges:
            observed[edge] = observed.get(edge, 0) + 1
    used = [d for d in declared["edges"]
            if any(fnmatch.fnmatchcase(a, d["from"]) and fnmatch.fnmatchcase(b, d["to"])
                   for a, b in observed)]
    return {
        "declaredEdges": len(declared["edges"]),
        "observedEdges": len(observed),
        "undeclared": [{"from": a, "to": b, "calls": n}
                       for (a, b), n in sorted(observed.items())
                       if not _declared((a, b), declared["edges"])],
        "declaredButUnused": [d for d in declared["edges"] if d not in used],
        "unattributedCalls": dict(sorted(unattributed.items())),
        "note": "Declared and observed are compared, never added (D14).",
    }


def edge_figures(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latency, error rate and tokens for every observed caller → target edge."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        edges = record_edges(record)
        if edges:
            groups.setdefault(edges[-1], []).append(record)
    rows = []
    for (caller, target), group in sorted(groups.items()):
        durations = [r["durationMs"] for r in group if isinstance(r.get("durationMs"),
                                                                    (int, float))]
        errors = sum(1 for r in group if r.get("error"))
        tokens = [_tokens(r) for r in group]
        known = [t for t in tokens if t is not None]
        rows.append({
            "from": caller, "to": target, "calls": len(group),
            "latencyMs": median_interval(durations) if durations
            else {"point": None, "reason": "no recorded duration"},
            "errorRate": wilson_interval(errors, len(group)),
            "tokens": {"total": sum(known), "callsWithUsage": len(known)} if known
            else {"total": None, "reason": "usage was not kept in the record"},
        })
    return rows


def _tokens(record: dict[str, Any]) -> int | None:
    usage = ((record.get("output") or {}).get("value") or {}).get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("total_tokens"), int):
            return usage["total_tokens"]
        parts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        if all(isinstance(p, int) for p in parts):
            return sum(parts)
    return None


def attribute(records: Sequence[dict[str, Any]], event_id: str) -> dict[str, Any]:
    """The subgraph behind one outcome: every record of its trace, and their edges.

    When an outcome comes from joint behaviour, responsibility is a subgraph,
    not a single actor. Without a trace id, only the event's own actor chain
    is recoverable, and the result says so.
    """
    target = next((r for r in records if r.get("id") == event_id), None)
    if target is None:
        raise TopologyError(f"No record with id {event_id}.")
    trace = (target.get("trace") or {}).get("traceId")
    members = [r for r in records if trace and (r.get("trace") or {}).get("traceId") == trace]
    if not members:
        members = [target]
    edges = sorted({e for r in members for e in record_edges(r)})
    return {
        "event": event_id,
        "traceId": trace,
        "scope": "trace" if trace else "actor chain only: the record carries no trace id",
        "records": [r.get("id") for r in members],
        "nodes": sorted({n for e in edges for n in e}),
        "edges": [{"from": a, "to": b} for a, b in edges],
        "attribution": attribution_completeness(members),
    }


__all__ = [
    "TopologyError",
    "attribute",
    "compare_topology",
    "edge_figures",
    "load_topology",
    "record_edges",
]
