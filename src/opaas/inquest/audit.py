"""The post-hoc audit: given the records that exist, what can be concluded (D48).

Integrity comes first. Before a single verdict, the audit establishes what
the evidence can be trusted for — the IS level the file demonstrates, and
what verifying it cost — because a verdict over a log anyone could have
rewritten is a verdict about the rewriter.

Then the five dimensions, each as far as the records allow, and the
operator's structural policies, **three-valued**: every policy complies, is
violated, or is undecidable, and every undecidable names the fields whose
absence made it so (Proposition 1). That list is the audit's most practical
output: it is what the operator has to start recording.

**Nothing is filled in.** What was not recorded is reported missing. ACR
needs the actions that happened, which a record cannot supply about itself,
so it is not measured here (smith measures it). LPC needs to know which
phases the system has; the operator declares them, and the result says the
basis is a declaration.

**Degradation (Q6).** `degrade` removes a field from every record, or drops
every k-th record, deterministically. Auditing a degraded copy next to the
original answers the Auditability Card's Q6 — what happens to the audit when
logs are missing — with measurements rather than assurances.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from typing import Any

from opaas.core.metrics import (
    action_completeness_rate,
    attribution_completeness,
    compare_thresholds,
    detection_latency,
    field_recoverability,
    lifecycle_coverage,
    not_measured,
    policy_decidability,
)
from opaas.core.policy import Policy, decide_all, policies_hash
from opaas.inquest.importers import Imported

DEGRADATIONS = ("drop-field", "drop-every", "digest-only")


class AuditError(ValueError):
    """The audit's inputs are inconsistent."""


def parse_degradation(spec: str) -> dict[str, Any]:
    """`drop-field:actorChain`, `drop-every:5`, `digest-only`."""
    kind, _, value = spec.partition(":")
    if kind not in DEGRADATIONS:
        raise AuditError(f"Unknown degradation '{kind}'. Known: {', '.join(DEGRADATIONS)}.")
    if kind == "drop-field" and not value:
        raise AuditError("drop-field needs a field: drop-field:actorChain.")
    if kind == "drop-every":
        if not value.isdigit() or int(value) < 2:
            raise AuditError("drop-every needs an integer of at least 2: drop-every:5.")
        return {"kind": kind, "every": int(value)}
    return {"kind": kind, "field": value} if value else {"kind": kind}


def degrade(records: Sequence[dict[str, Any]],
            degradations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [copy.deepcopy(r) for r in records]
    for step in degradations:
        if step["kind"] == "drop-every":
            out = [r for i, r in enumerate(out, 1) if i % step["every"]]
        elif step["kind"] == "drop-field":
            for record in out:
                _drop(record, step["field"].split("."))
        else:
            for record in out:
                for name in ("args", "output"):
                    if isinstance(record.get(name), dict):
                        record[name].pop("value", None)
    return out


def _drop(record: dict[str, Any], parts: list[str]) -> None:
    current: Any = record
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def audit(
    imported: Imported,
    *,
    policies: Sequence[Policy] = (),
    expected_phases: Iterable[str] | None = None,
    thresholds: dict[str, dict[str, Any]] | None = None,
    degradations: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    records = degrade(imported.records, degradations) if degradations else imported.records
    integrity = imported.integrity
    verdicts = decide_all(policies, records)
    attribution = attribution_completeness(records)

    dimensions = {
        "actionRecoverability": {
            "question": "Q1",
            "ACR": action_completeness_rate(None, records),
            "RF": field_recoverability(records),
        },
        "lifecycleCoverage": {
            "question": "Q2",
            "LPC": lifecycle_coverage(records, expected_phases=expected_phases),
            "GB": not_measured("Gap burden needs ground truth: run `opaas smith`."),
        },
        "policyCheckability": {
            "question": "Q3",
            "SPDR": policy_decidability(verdicts),
            "ADL": detection_latency(records),
        },
        "responsibilityAttribution": {
            "question": "Q4",
            "AC": attribution.get("AC") if attribution.get("measured") else attribution,
            "ACD": attribution.get("ACD") if attribution.get("measured") else attribution,
            "incompleteBecause": attribution.get("incompleteBecause", {}),
        },
        "evidenceIntegrity": {
            "question": "Q5",
            "IS": integrity.get("level"),
            "ISBasis": integrity.get("levelBasis", "observed in the file"),
            "VC": integrity.get("verificationCost"),
            "problems": integrity.get("problems", []),
        },
    }
    flat = {
        "RF": dimensions["actionRecoverability"]["RF"],
        "LPC": dimensions["lifecycleCoverage"]["LPC"],
        "SPDR": dimensions["policyCheckability"]["SPDR"],
        "AC": dimensions["responsibilityAttribution"]["AC"],
    }
    limitations = [
        "ACR is not measured: a record cannot count the actions it omitted.",
        "Gap burden is not measured: it needs ground truth.",
        "Only structural policies are decided (N2); semantic policies are out of scope.",
    ]
    if integrity.get("level") in (None, "none", "append-only"):
        limitations.insert(0, "The evidence demonstrates no chain: any record in it could "
                           "have been altered or removed, and every verdict below inherits "
                           "that.")
    return {
        "source": {"format": imported.format, "file": integrity.get("path"),
                   "records": len(records), "importedRecords": len(imported.records),
                   "excluded": imported.excluded, "skipped": imported.skipped,
                   "notes": imported.notes},
        "degradations": list(degradations),
        "integrity": integrity,
        "policies": {"hash": policies_hash(list(policies)), "count": len(policies)},
        "verdicts": [v.as_dict() for v in verdicts],
        "dimensions": dimensions,
        "thresholds": compare_thresholds(flat, thresholds or {}),
        "limitations": limitations,
    }


def compare_degraded(base: dict[str, Any], degraded: dict[str, Any]) -> dict[str, Any]:
    """Which verdicts a degradation turned undecidable, and how each dimension moved (Q6)."""
    before = {v["policy"]: v["verdict"] for v in base["verdicts"]}
    after = {v["policy"]: v for v in degraded["verdicts"]}
    lost = [{"policy": p, "was": before[p], "now": after[p]["verdict"],
             "missing": after[p].get("missing", [])}
            for p in sorted(before) if p in after and before[p] != after[p]["verdict"]]
    return {
        "degradations": degraded["degradations"],
        "verdictsChanged": lost,
        "SPDR": {"before": base["dimensions"]["policyCheckability"]["SPDR"].get("point"),
                 "after": degraded["dimensions"]["policyCheckability"]["SPDR"].get("point")},
        "RF": {"before": base["dimensions"]["actionRecoverability"]["RF"].get("point"),
               "after": degraded["dimensions"]["actionRecoverability"]["RF"].get("point")},
    }


__all__ = [
    "DEGRADATIONS",
    "AuditError",
    "audit",
    "compare_degraded",
    "degrade",
    "parse_degradation",
]
