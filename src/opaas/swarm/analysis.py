"""Everything `opaas swarm analyze` reports, from records and a declared topology."""

from __future__ import annotations

from typing import Any

from opaas.core.metrics import attribution_completeness
from opaas.swarm.faults import analyse_faults
from opaas.swarm.topology import compare_topology, edge_figures


def analyse(records: list[dict[str, Any]], topology: dict[str, Any]) -> dict[str, Any]:
    delegations = [r for r in records if r.get("action") in ("delegation", "handoff")]
    return {
        "records": len(records),
        "topology": compare_topology(topology, records),
        "edges": edge_figures(records),
        "attribution": {
            "allActions": attribution_completeness(records),
            "delegations": attribution_completeness(delegations) if delegations else {
                "measured": False, "reason": "No delegation was recorded."},
        },
        "faults": analyse_faults(records, retry_budget=topology.get("retryBudget", 3)),
    }


__all__ = ["analyse"]
