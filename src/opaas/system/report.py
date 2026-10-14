"""The system report: every member by hash, read into one table (D60, phase S1).

Assembly only: this module runs nothing. It takes the manifest, the member
documents (whoever produced them) and what happened to each planned step, and
writes down what they say about the system, cell by cell.

**Nothing is added up.** The report is a table of components × dimensions
(invariant 1). Coverage says, per cell, whether anything was measured; it
never says how well.

**Declared and observed stay apart** (invariant 9), for components and for
flows (§10.1): what the manifest says, what the inventory and the analysed
records show, and every disagreement as a finding.

**A cell no evidence covers is still written**: *not measured* with the reason
(the step was skipped, failed, or its adapter does not exist yet), or *not
applicable* where the dimension has no meaning for the component's kind. A
reader never has to infer a gap from an absence.

**Thresholds are the operator's** (N6), keyed by component and by kind (§10.2);
the component key wins, and the cell says which applied. Each yields a
three-valued verdict: comply or violate only when the whole interval sits on
one side, undecidable otherwise (N1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opaas.core.hashing import file_digest
from opaas.system.extract import Extraction, extract
from opaas.system.manifest import SystemManifest
from opaas.system.measurement import AUDITABILITY, BEHAVIOUR, Measurement

#: The row the system's own evidence goes in: gap scans, flows, the declaration.
SYSTEM_ROW = "system"

#: Dimensions that can apply to each kind of row. A dimension outside this set
#: is written as *not applicable*, with the reason in _NOT_APPLICABLE.
APPLICABLE: dict[str, frozenset[str]] = {
    "model": frozenset(BEHAVIOUR),
    "agent": frozenset({"capability", "consistency", "safety", *AUDITABILITY}),
    "proxy": frozenset({"performance", *AUDITABILITY}),
    SYSTEM_ROW: frozenset({"performance", "consistency", "safety", "compliance",
                           *AUDITABILITY}),
}
_NOT_APPLICABLE = {
    "model": "A model keeps no record of its own: the auditability of the calls made to it "
             "belongs to the proxy or the agent that made them.",
    "agent": "Measured on the agent's model and tools, and on the system: an agent has no "
             "latency or compliance of its own.",
    "proxy": "A proxy forwards requests: only the latency it adds and the records it keeps "
             "are its own.",
    "tool": "A tool is measured through the agents that call it, in their scenarios.",
    "mcp-server": "An MCP server is measured through the agents that call its tools.",
    "data-store": "A data store is declared, never read (N4).",
    "human-approver": "An approver's decisions are recorded as events of the gate that asked "
                      "them, and audited there.",
    SYSTEM_ROW: "Measured per component, not for the system as a whole.",
}
_EXTERNAL = ("Outside the operator's control: listed, never evaluated or certified (D22). Every "
             "flow through it leaves the system's boundary.")

#: Card questions and the auditability dimension each answers (card/auditability.py).
QUESTIONS = {
    "Q1": "actionRecoverability",
    "Q2": "lifecycleCoverage",
    "Q3": "policyCheckability",
    "Q4": "responsibilityAttribution",
    "Q5": "evidenceIntegrity",
    "Q6": "degradation",
}


class SystemReportError(ValueError):
    """A member or a threshold file cannot be used; the message says which and why."""


@dataclass(slots=True)
class Member:
    """One document behind the report, and the component it describes."""

    component: str
    step: str
    path: Path


@dataclass(slots=True)
class StepOutcome:
    """What happened to one planned step: ran, supplied, skipped, failed or pending."""

    step: str
    component: str
    status: str
    reason: str | None = None


def build_report(
    manifest: SystemManifest,
    *,
    manifest_hash: str,
    members: list[Member],
    steps: list[StepOutcome],
    thresholds: dict[str, Any] | None = None,
    declared_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The body of the system report (wrapped with `envelope("system-report", …)`)."""
    rows = _rows(manifest)
    extractions: list[tuple[Member, str, Extraction]] = []
    inventory: dict[str, Any] | None = None
    swarm: list[dict[str, Any]] = []
    for member in members:
        if member.component not in rows:
            raise SystemReportError(
                f"{member.path.name}: '{member.component}' is not a component of this system."
            )
        try:
            document = json.loads(member.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemReportError(f"{member.path}: {exc}") from None
        digest = file_digest(member.path)
        extraction = extract(document, component=member.component, file=member.path.name,
                             file_hash=digest)
        extractions.append((member, digest, extraction))
        if extraction.kind == "inventory":
            inventory = document.get("inventory")
        if extraction.kind == "swarm-analysis":
            swarm.append(document)

    measurements = [m for _, _, e in extractions for m in e.measurements]
    measurements += _missing_cells(rows, measurements, steps)
    records = [_with_threshold(m, rows, thresholds or {}) for m in measurements]

    return {
        "system": {"id": manifest.id, "purpose": manifest.purpose, "manifestHash": manifest_hash},
        "composition": _composition(manifest, rows, inventory, swarm, extractions),
        "members": [
            {"component": member.component, "step": member.step, "kind": extraction.kind,
             "file": member.path.name, "hash": digest,
             **({"environment": extraction.environment} if extraction.environment else {})}
            for member, digest, extraction in extractions
        ],
        "steps": [_step(outcome) for outcome in steps],
        "environments": _environments(extractions),
        "measurements": records,
        "coverage": _coverage(rows, records),
        "card": _card(records),
        "compliance": _compliance(manifest, records, declared_items),
        "limitations": [
            {"component": member.component, "source": member.path.name, "text": text}
            for member, _, extraction in extractions
            for text in extraction.limitations
        ],
        "note": "Every member is certified by the hash of its own bytes (invariant 4). This "
                "report reads them into one table and adds no score, no tier and no aggregate "
                "of their results (invariant 1).",
    }


# ------------------------------------------------------------------ rows


def _rows(manifest: SystemManifest) -> dict[str, str]:
    """Row id → its kind: one per component, plus the system's own row."""
    rows = {c.id: c.kind for c in manifest.components}
    if SYSTEM_ROW in rows:
        raise SystemReportError(
            f"'{SYSTEM_ROW}' is reserved for the system's own row: rename that component."
        )
    rows[SYSTEM_ROW] = SYSTEM_ROW
    return rows


def _missing_cells(
    rows: dict[str, str], measured: list[Measurement], steps: list[StepOutcome]
) -> list[Measurement]:
    """A cell for every row × dimension no evidence wrote, saying why."""
    covered = {(m.component, m.dimension) for m in measured}
    why_not = {}
    for outcome in steps:
        if outcome.status in ("skipped", "failed", "pending"):
            why_not.setdefault(outcome.component, []).append(
                f"{outcome.step} {outcome.status}: {outcome.reason}"
            )
    cells = []
    for row, kind in rows.items():
        for axis, dimensions in (("behaviour", BEHAVIOUR), ("auditability", AUDITABILITY)):
            for dimension in dimensions:
                if (row, dimension) in covered:
                    continue
                source = {"kind": "system-report"}
                if kind == "external":
                    status, reason = "not-measured", _EXTERNAL
                elif dimension not in APPLICABLE.get(kind, frozenset()):
                    status, reason = "not-applicable", _NOT_APPLICABLE[kind]
                else:
                    status = "not-measured"
                    reason = ("; ".join(why_not[row]) if row in why_not
                              else "No evidence in this audit covers it.")
                cells.append(Measurement(component=row, axis=axis, dimension=dimension,
                                         aspect=dimension, metric=dimension, status=status,
                                         reason=reason, source=source))
    return cells


# ------------------------------------------------------------- thresholds


def load_thresholds(document: Any) -> dict[str, Any]:
    """`{"byComponent": {id: {metric: rule}}, "byKind": {kind: {metric: rule}}}`.

    A rule is `{"min": x}` or `{"max": x}`, optionally with `"stat"` (p50, p95,
    p99) for a distribution, or `{"in": [words]}` for a verdict.
    """
    if not isinstance(document, dict) or set(document) - {"byComponent", "byKind"}:
        raise SystemReportError("Thresholds take 'byComponent' and 'byKind' only.")
    for scope in ("byComponent", "byKind"):
        for owner, rules in (document.get(scope) or {}).items():
            if not isinstance(rules, dict):
                raise SystemReportError(f"{scope}.{owner} must map metrics to rules.")
            for metric, rule in rules.items():
                keys = set(rule) if isinstance(rule, dict) else set()
                if not keys or not keys <= {"min", "max", "stat", "in"} or (
                    len(keys & {"min", "max", "in"}) != 1
                ):
                    raise SystemReportError(
                        f"{scope}.{owner}.{metric}: a rule is one of min, max or in, "
                        "optionally with stat."
                    )
    return document


def _with_threshold(
    measurement: Measurement, rows: dict[str, str], thresholds: dict[str, Any]
) -> dict[str, Any]:
    record = measurement.to_record()
    by_component = (thresholds.get("byComponent") or {}).get(measurement.component) or {}
    by_kind = (thresholds.get("byKind") or {}).get(rows.get(measurement.component, "")) or {}
    for scope, rules in (("component", by_component), ("kind", by_kind)):
        rule = rules.get(measurement.metric)
        if rule is not None:
            record["threshold"] = {**rule, "scope": scope, "declaredBy": "operator"}
            record["verdict"] = _judge(measurement, rule)
            break
    return record


def _judge(measurement: Measurement, rule: dict[str, Any]) -> dict[str, Any]:
    """comply, violate or undecidable, and why (N1)."""
    if measurement.status != "measured" or measurement.value is None:
        return {"verdict": "undecidable", "reason": f"The cell is {measurement.status}."}
    value = measurement.value
    if "in" in rule:
        if value["type"] != "verdict":
            return {"verdict": "undecidable", "reason": "'in' applies to verdict cells."}
        return {"verdict": "comply" if value["verdict"] in rule["in"] else "violate"}
    if value["type"] == "rate":
        point, low, high = value["point"], value["low"], value["high"]
    elif value["type"] == "number":
        point = low = high = value["value"]
    elif value["type"] == "distribution":
        stat = rule.get("stat", "p50")
        point = value.get(stat, value.get("point") if stat == "p50" else None)
        if point is None:
            return {"verdict": "undecidable", "reason": f"The distribution has no {stat}."}
        low, high = value.get("low", point), value.get("high", point)
        if stat != "p50" or "low" not in value:
            low = high = point
    else:
        return {"verdict": "undecidable", "reason": "A verdict cell takes an 'in' rule."}
    if "min" in rule:
        bound = float(rule["min"])
        if low >= bound:
            return {"verdict": "comply"}
        return {"verdict": "violate"} if high < bound else {
            "verdict": "undecidable", "reason": "The interval straddles the threshold."}
    bound = float(rule["max"])
    if high <= bound:
        return {"verdict": "comply"}
    return {"verdict": "violate"} if low > bound else {
        "verdict": "undecidable", "reason": "The interval straddles the threshold."}


# ------------------------------------------------------------ composition


def _composition(
    manifest: SystemManifest,
    rows: dict[str, str],
    inventory: dict[str, Any] | None,
    swarm: list[dict[str, Any]],
    extractions: list[tuple[Member, str, Extraction]],
) -> dict[str, Any]:
    exercised = {
        member.component for member, _, e in extractions
        if member.component != SYSTEM_ROW and any(m.status == "measured" for m in e.measurements)
    }
    components = [_observed_component(c, inventory, exercised) for c in manifest.components]
    findings = [f"{c['id']} is declared and was not found: {c['basis']}"
                for c in components if c["observed"] is False]
    findings += _undeclared(manifest, inventory)
    flows = [_observed_flow(f, manifest, swarm) for f in manifest.flows]
    findings += [f"Flow {f['from']} → {f['to']} is declared and was never used in the "
                 "analysed records." for f in flows if f["observed"] is False]
    for document in swarm:
        for edge in (document.get("topology") or {}).get("undeclared") or []:
            findings.append(f"Observed and not declared: {edge.get('from')} → {edge.get('to')} "
                            f"({edge.get('calls')} calls).")
    return {"components": components, "flows": flows, "findings": findings,
            "note": "Declared and observed are compared, never added (invariant 9)."}


def _observed_component(
    component: Any, inventory: dict[str, Any] | None, exercised: set[str]
) -> dict[str, Any]:
    entry = {"id": component.id, "kind": component.kind, "declared": True}
    if component.id in exercised:
        return {**entry, "observed": True, "basis": "exercised by this audit"}
    if inventory is None:
        return {**entry, "observed": None, "basis": "no inventory was taken"}
    if component.kind == "model":
        name = component.fields.get("model")
        for engine in inventory.get("engines") or []:
            if any(m.get("name") == name for m in engine.get("models") or []):
                return {**entry, "observed": True,
                        "basis": f"served by {engine.get('kind')} at {engine.get('endpoint')}"}
        return {**entry, "observed": False, "basis": f"no engine on the host serves {name}"}
    stack = inventory.get("agentStack") or {}
    names = {
        "mcp-server": {s.get("name") for c in stack.get("mcpConfigs") or []
                       for s in c.get("servers") or []},
        "tool": {t.get("name") for m in stack.get("toolManifests") or []
                 for t in m.get("tools") or []},
    }.get(component.kind)
    if names:
        target = component.fields.get("name") or component.id
        return {**entry, "observed": target in names,
                "basis": "listed in the named configuration" if target in names
                else "not in any named configuration"}
    return {**entry, "observed": None,
            "basis": f"discover cannot observe a {component.kind} on its own"}


def _undeclared(manifest: SystemManifest, inventory: dict[str, Any] | None) -> list[str]:
    if inventory is None:
        return []
    declared = {c.fields.get("model") for c in manifest.components if c.kind == "model"}
    findings = []
    for engine in inventory.get("engines") or []:
        for model in engine.get("models") or []:
            if model.get("name") not in declared:
                findings.append(
                    f"Observed and not declared: {engine.get('kind')} at {engine.get('endpoint')} "
                    f"serves {model.get('name')}."
                )
    return findings


def flow_edge(flow: dict[str, str], manifest: SystemManifest) -> dict[str, str]:
    """A declared flow as a swarm topology edge (role:id, as records name actors)."""
    return {"from": _node(manifest, flow["from"]), "to": _node(manifest, flow["to"])}


def _node(manifest: SystemManifest, component_id: str) -> str:
    component = manifest.component(component_id)
    if component is None:
        return f"*:{component_id}"
    if component.kind == "model":
        # A model call's target is the model's name, not the component's id.
        return f"model:{component.fields.get('model')}"
    role = {"agent": "agent", "tool": "tool", "mcp-server": "tool",
            "human-approver": "user"}.get(component.kind, component.kind)
    return f"{role}:{component.id}"


def _observed_flow(
    flow: dict[str, str], manifest: SystemManifest, swarm: list[dict[str, Any]]
) -> dict[str, Any]:
    entry = {**flow, "declared": True}
    if not swarm:
        return {**entry, "observed": None, "basis": "no records were analysed for flows"}
    edge = flow_edge(flow, manifest)
    unused = [e for d in swarm for e in (d.get("topology") or {}).get("declaredButUnused") or []]
    return {**entry, "observed": edge not in unused,
            "basis": "seen in the analysed records" if edge not in unused
            else "not seen in the analysed records"}


# ----------------------------------------------------------------- tables


def _step(outcome: StepOutcome) -> dict[str, Any]:
    out = {"step": outcome.step, "component": outcome.component, "status": outcome.status}
    if outcome.reason:
        out["reason"] = outcome.reason
    return out


def _environments(extractions: list[tuple[Member, str, Extraction]]) -> dict[str, Any]:
    """Whether the members were measured in one environment (N5)."""
    inventories = sorted({
        str(e.environment.get("inventoryHash"))
        for _, _, e in extractions if e.environment and e.environment.get("inventoryHash")
    })
    unbound = sorted({member.path.name for member, _, e in extractions
                      if e.kind not in ("inventory", "deployment-manifest")
                      and not (e.environment or {}).get("bound")})
    return {"inventories": inventories, "unbound": unbound,
            "consistent": len(inventories) <= 1 and not unbound}


def _coverage(rows: dict[str, str], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per row and dimension: measured if any cell is, else why not. Never how well."""
    coverage: dict[str, dict[str, str]] = {row: {} for row in rows}
    rank = {"measured": 0, "not-measured": 1, "not-applicable": 2}
    for record in records:
        cell = coverage[record["component"]]
        current = cell.get(record["dimension"])
        if current is None or rank[record["status"]] < rank[current]:
            cell[record["dimension"]] = record["status"]
    return coverage


def _card(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Q1–Q6 answered from the auditability cells, each citing its component and source."""
    card = {}
    for qid, dimension in QUESTIONS.items():
        cells = [r for r in records if r["axis"] == "auditability" and r["dimension"] == dimension]
        card[qid] = {
            "dimension": dimension,
            "answers": [_cite(r) for r in cells if r["status"] == "measured"],
            "gaps": [_cite(r) for r in cells if r["status"] == "not-measured"],
            "notApplicable": sorted({r["component"] for r in cells
                                     if r["status"] == "not-applicable"}),
        }
    return card


def _cite(record: dict[str, Any]) -> dict[str, Any]:
    out = {"component": record["component"], "aspect": record["aspect"],
           "source": record["source"]}
    if "value" in record:
        out["value"] = record["value"]
    if "reason" in record:
        out["reason"] = record["reason"]
    return out


def _compliance(
    manifest: SystemManifest,
    records: list[dict[str, Any]],
    declared_items: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Block E at system level: the purpose, what the operator declares, what was probed."""
    return {
        "purpose": manifest.purpose,
        "declared": declared_items if declared_items is not None else [],
        "declarationSupplied": declared_items is not None,
        "probed": [_cite(r) for r in records
                   if r["dimension"] == "compliance" and r["status"] == "measured"],
        "note": "Declared and probed are listed apart and never added (invariant 9). The risk "
                "classification follows the system's purpose, not a model's.",
    }


__all__ = [
    "APPLICABLE",
    "QUESTIONS",
    "SYSTEM_ROW",
    "Member",
    "StepOutcome",
    "SystemReportError",
    "build_report",
    "flow_edge",
    "load_thresholds",
]
