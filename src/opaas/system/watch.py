"""`opaas watch` for a system certificate: every component, and which one moved (D60, S3).

A system changes when any of its components does. Watching one model of a
five-component system and calling the system current would be the omission
this project exists to prevent, so the system watch re-checks every member it
can, names each one's state, and lists the components that moved first
(§10.3).

What each kind gets:

- **model**: the same checks as `opaas watch` on its certified bench report
  (fingerprint, canaries, and the statistical checks when undecided).
- **agent**: a `bare-model` agent is its model in OPAAS's loop, so it follows
  that model's state. Any other agent cannot be re-checked without rerunning
  its scenarios, and says so: it is listed, never assumed current.
- **the environment**: the current inventory against the one the system was
  audited in. A model served now that was not served then is an alert; one
  that was already there was a composition finding of the audit, not a change.
- **everything else** (tools, proxies, approvers): not re-checkable on its own,
  listed with the reason.

The state settles as `opaas watch` does (D36): drifted if anything drifted,
inconclusive if a model could not be checked or nothing was, current
otherwise. Exit codes are the same: 0, 1, 3.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

CheckModel = Callable[[str, dict[str, Any]], dict[str, Any]]

_NOT_RECHECKED = {
    "agent": "An agent's behaviour is re-checked by running its scenarios again: "
             "`opaas audit run` with the same manifest.",
    "proxy": "A proxy is re-checked through the records it keeps, by auditing them again.",
    "tool": "A tool is re-checked through the agents that call it.",
    "mcp-server": "An MCP server is re-checked through the environment inventory and the "
                  "agents that call it.",
    "human-approver": "An approver is not a measured component.",
    "data-store": "A data store is declared, never read (N4).",
    "external": "Outside the operator's control: never evaluated (D22).",
}


def watch_system(
    report: dict[str, Any],
    *,
    model_reports: dict[str, dict[str, Any]],
    check_model: CheckModel,
    baseline_inventory: dict[str, Any] | None = None,
    current_inventory: dict[str, Any] | None = None,
    check_environment: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The state of every component, the environment, and the system's.

    `model_reports`: component id → its certified bench report, already
    checked against the hash the system report records.
    """
    composition = report.get("composition") or {}
    components = {c["id"]: c for c in composition.get("components") or [] if "id" in c}
    adapters = {cid: c.get("adapter") for cid, c in components.items()}
    states: dict[str, dict[str, Any]] = {}

    for component_id, component in components.items():
        if component.get("kind") != "model":
            continue
        certified = model_reports.get(component_id)
        if certified is None:
            states[component_id] = {
                "kind": "model", "state": "inconclusive",
                "detail": "The certified bench report is not available here: pass "
                          f"--member {component_id}=<path to it>.",
            }
            continue
        states[component_id] = {"kind": "model", **check_model(component_id, certified)}

    for component_id, component in components.items():
        kind = component.get("kind")
        if kind == "model":
            continue
        target = _model_of(component_id, composition) if kind == "agent" else None
        if kind == "agent" and adapters.get(component_id) == "bare-model" and target in states:
            states[component_id] = {
                "kind": "agent", "state": states[target]["state"], "follows": target,
                "detail": f"A bare-model agent is {target} in OPAAS's loop: it follows "
                          "that model's state.",
            }
        else:
            states[component_id] = {"kind": kind, "state": "not-checked",
                                    "detail": _NOT_RECHECKED.get(str(kind), "Not re-checkable.")}

    environment = None
    if baseline_inventory is not None and current_inventory is not None and check_environment:
        environment = check_environment(baseline_inventory, current_inventory)

    deciding = [s for s in states.values() if s["state"] != "not-checked"]
    drifted = [cid for cid, s in states.items() if s["state"] == "drifted"]
    if environment is not None and environment.get("state") == "drifted":
        drifted.append("environment")
    if drifted:
        state = "drifted"
    elif any(s["state"] == "inconclusive" for s in deciding) or not deciding:
        state = "inconclusive"
    else:
        state = "current"
    return {"state": state, "changed": drifted, "components": states,
            **({"environment": environment} if environment is not None else {})}


def system_result(
    state: str, *, credential: dict[str, Any], report_hash: str,
    body: dict[str, Any] | None = None, detail: str | None = None,
) -> dict[str, Any]:
    """The result file for a system: one entry per component, never one combined verdict."""
    return {
        "state": state,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"),
        "credentialId": credential.get("id"),
        "reportHash": report_hash,
        "validUntil": credential.get("expirationDate"),
        "changed": (body or {}).get("changed", []),
        "components": (body or {}).get("components", {}),
        **({"environment": body["environment"]} if body and "environment" in body else {}),
        "detail": detail,
        "note": "Not a certificate: the sentinel signs nothing. It says, component by "
                "component, whether the signed system certificate still describes this system.",
    }


def _model_of(agent: str, composition: dict[str, Any]) -> str | None:
    kinds = {c.get("id"): c.get("kind") for c in composition.get("components") or []}
    for flow in composition.get("flows") or []:
        if flow.get("from") == agent and kinds.get(flow.get("to")) == "model":
            return str(flow["to"])
    return None


__all__ = ["watch_system", "system_result"]
