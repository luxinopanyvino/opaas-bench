"""The Auditability Card, filled from evidence rather than written (D52).

*Auditable Agents* proposes a card of six questions, one per dimension plus
one on missing logs. Here every answer is assembled from evidence documents
OPAAS produced — `discover` gap scans, `smith` reports, `inquest` audits,
`swarm` analyses, the sentinel's overhead — and each answer cites the file
and the hash it rests on. A question no evidence answers says so; it is never
answered from a description of the system (invariant 2).

There is no overall rating, and no answer is turned into a pass or fail by
OPAAS (invariant 1, N6). Where the operator declared thresholds, they appear
next to the measurement, as declared.

**Q6 is answered by experiment.** An `inquest` audit of a deliberately
degraded copy of a log, next to the audit of the original, shows which
verdicts the missing data turns undecidable. Without such a pair, Q6 is not
answered.

**Retention (N4)** is a declaration: what the deployment keeps in clear, and
for how long. The card prints it as the operator's word.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opaas.core.evidence import read_document
from opaas.core.hashing import file_digest
from opaas.inquest.audit import compare_degraded

QUESTIONS: dict[str, dict[str, str]] = {
    "Q1": {"dimension": "actionRecoverability", "text": "What happened? Can every "
           "policy-relevant action be recovered from the record, with its fields?"},
    "Q2": {"dimension": "lifecycleCoverage", "text": "In what context? Are retries, "
           "fallbacks, blocks and approvals visible as phases of execution?"},
    "Q3": {"dimension": "policyCheckability", "text": "Did it comply? Which policies can be "
           "decided from the record, and how fast?"},
    "Q4": {"dimension": "responsibilityAttribution", "text": "Who was responsible? Does each "
           "action carry a complete chain from a principal to the actor?"},
    "Q5": {"dimension": "evidenceIntegrity", "text": "Can the evidence be trusted? What "
           "integrity does it demonstrate, and what does checking it cost?"},
    "Q6": {"dimension": "degradation", "text": "What happens to the audit when logs are "
           "missing or incomplete?"},
}

_ACCEPTED = ("gap-scan", "smith-report", "inquest-audit", "swarm-analysis",
             "sentinel-overhead", "inventory")


def _cite(item: dict[str, Any]) -> dict[str, Any]:
    return {"kind": item["kind"], "file": item["file"], "hash": item["hash"]}


def _pick(metric: Any) -> Any:
    """The parts of a metric a reader needs: point, interval, n, or why not measured."""
    if not isinstance(metric, dict):
        return metric
    if metric.get("measured") is False:
        return {"measured": False, "reason": metric.get("reason")}
    keys = ("point", "low", "high", "n", "p50", "p95", "p99", "count", "basis",
            "unobserved", "missingFieldsBlocking", "verdicts", "ms", "bytes", "requires")
    return {k: metric[k] for k in keys if k in metric}


def build_card(paths: list[Path], *, retention: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = []
    for path in paths:
        document = read_document(path, kind=_ACCEPTED)
        evidence.append({"kind": document["kind"], "file": path.name,
                         "hash": file_digest(path), "document": document})

    answers: dict[str, list[dict[str, Any]]] = {q: [] for q in QUESTIONS}
    gaps: dict[str, list[dict[str, Any]]] = {q: [] for q in QUESTIONS}

    for item in evidence:
        doc, cite = item["document"], _cite(item)
        if item["kind"] == "gap-scan":
            for finding in doc.get("findings", []):
                if finding["status"] in ("gap", "unsupported"):
                    gaps[finding["question"]].append({**cite, "rule": finding["rule"],
                                                      "status": finding["status"],
                                                      "subject": finding["subject"],
                                                      "detail": finding["detail"]})
        elif item["kind"] == "smith-report":
            audit = doc.get("auditability", {})
            ground = "agent record against mock-tool ground truth"
            answers["Q1"].append({**cite, "basis": ground, "ACR": _pick(audit.get("ACR")),
                                  "RF": _pick(audit.get("RF"))})
            answers["Q2"].append({**cite, "basis": ground, "LPC": _pick(audit.get("LPC")),
                                  "GB": _pick(audit.get("GB"))})
            answers["Q3"].append({**cite, "basis": ground, "SPDR": _pick(audit.get("SPDR")),
                                  "misleadingVerdicts": audit.get("misleadingVerdicts", [])})
            ac = audit.get("AC") or {}
            answers["Q4"].append({**cite, "basis": ground,
                                  "AC": _pick(ac.get("AC", ac)), "ACD": _pick(ac.get("ACD"))})
            answers["Q5"].append({**cite, "basis": ground, "IS": audit.get("IS")})
        elif item["kind"] == "inquest-audit":
            if doc.get("degradations"):
                continue  # Q6 only; paired below
            dims = doc.get("dimensions", {})
            basis = f"post-hoc audit of {doc.get('source', {}).get('format')} records"
            answers["Q1"].append({**cite, "basis": basis,
                                  "RF": _pick(dims["actionRecoverability"]["RF"]),
                                  "ACR": _pick(dims["actionRecoverability"]["ACR"])})
            answers["Q2"].append({**cite, "basis": basis,
                                  "LPC": _pick(dims["lifecycleCoverage"]["LPC"])})
            answers["Q3"].append({**cite, "basis": basis,
                                  "SPDR": _pick(dims["policyCheckability"]["SPDR"]),
                                  "ADL": _pick(dims["policyCheckability"]["ADL"]),
                                  "thresholds": doc.get("thresholds", [])})
            answers["Q4"].append({**cite, "basis": basis,
                                  "AC": _pick(dims["responsibilityAttribution"]["AC"]),
                                  "ACD": _pick(dims["responsibilityAttribution"]["ACD"])})
            answers["Q5"].append({**cite, "basis": basis,
                                  "IS": dims["evidenceIntegrity"]["IS"],
                                  "ISBasis": dims["evidenceIntegrity"]["ISBasis"],
                                  "VC": _pick(dims["evidenceIntegrity"]["VC"])})
        elif item["kind"] == "swarm-analysis":
            faults = doc.get("faults", {})
            answers["Q2"].append({**cite, "basis": "scripted fault injection",
                                  "faultOutcomes": faults.get("outcomes"),
                                  "recoveredButNotVisible":
                                  len(faults.get("recoveredButNotVisible", []))})
            delegations = (doc.get("attribution") or {}).get("delegations") or {}
            answers["Q4"].append({**cite, "basis": "delegation chains in a multi-agent flow",
                                  "AC": _pick(delegations.get("AC", delegations)),
                                  "undeclaredEdges": len(doc.get("topology", {})
                                                         .get("undeclared", []))})
        elif item["kind"] == "sentinel-overhead":
            answers["Q5"].append({**cite, "basis": "cost of the recording layer",
                                  "overheadMs": _pick(doc.get("overhead"))})

    answers["Q6"] = _q6(evidence)

    card = {}
    for qid, question in QUESTIONS.items():
        entry: dict[str, Any] = {**question, "answers": answers[qid], "gaps": gaps[qid]}
        if not answers[qid]:
            entry["notAnswered"] = (
                "No evidence answers this question. Q6 needs an inquest audit of a degraded "
                "log next to the audit of the original." if qid == "Q6"
                else "No evidence document given answers this question."
            )
        card[qid] = entry
    return {
        "questions": card,
        "evidence": [_cite(item) for item in evidence],
        "retention": {"declaredBy": "operator", **retention} if retention else {
            "declared": False,
            "detail": "The operator did not declare what is kept in clear or for how long (N4).",
        },
        "note": "No overall rating: each question is answered on its own evidence.",
    }


def _q6(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    audits = [e for e in evidence if e["kind"] == "inquest-audit"]
    bases = [e for e in audits if not e["document"].get("degradations")]
    out = []
    for degraded in (e for e in audits if e["document"].get("degradations")):
        source = degraded["document"].get("source", {}).get("file")
        policies = degraded["document"].get("policies", {}).get("hash")
        base = next((b for b in bases
                     if b["document"].get("source", {}).get("file") == source
                     and b["document"].get("policies", {}).get("hash") == policies), None)
        if base is None:
            continue
        out.append({**_cite(degraded), "comparedWith": _cite(base),
                    **compare_degraded(base["document"], degraded["document"])})
    return out


__all__ = ["QUESTIONS", "build_card"]
