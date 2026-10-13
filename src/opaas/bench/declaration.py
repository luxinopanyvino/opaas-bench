"""Compliance declaration: what the operator claims, with its evidence.

Block E is largely documentary and cannot be executed. What can be done — and
what gives a certificate its value — is to **require every claim to come with
evidence and to keep it separate from what the tool verifies by itself**.

A report that mixes "the provider documents its training data" (claimed by
whoever requests the certificate) with "the engine exposes logprobs" (checked
by OPAAS) is worthless. Here they are never mixed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Values allowed in a declaration.
STATUSES = ("yes", "partial", "no", "unknown")


class DeclarationError(ValueError):
    """The declaration does not exist, is malformed, or is missing requirements."""


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    title: str
    #: 'declared' is claimed by the operator; 'probed' is verified by OPAAS.
    kind: str
    frameworks: tuple[str, ...]
    why: str
    #: If True, the item cannot be recorded as met without evidence.
    evidence_required: bool = True


#: Block E requirements, derived from the design document. Each reference is
#: cited to the clause and checked against the official text (D30); the wider
#: mapping, including ISO/IEC 42001 and the NIST AI RMF, is in
#: docs/standards-mapping.md.
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        id="trainingDataDocumented",
        title="The provider documents the training data",
        kind="declared",
        frameworks=("AI Act art. 53(1)(d)", "AI Act annex XII 2(c)"),
        why="Without data provenance there is no way to assess bias, licensing or leakage.",
    ),
    Requirement(
        id="licenseDocumented",
        title="Model license explicit and compatible with the intended use",
        kind="declared",
        frameworks=("AI Act art. 53(1)(b)", "AI Act annex XII 1(h)"),
        why="Many open weights restrict commercial use or redistribution.",
    ),
    Requirement(
        id="provenanceDocumented",
        title="Verifiable provenance of the weights (repository, hash, version)",
        kind="declared",
        frameworks=("AI Act art. 53(1)(b)", "ENS op.exp.1"),
        why="It is what allows claiming that the deployed model is the certified one.",
    ),
    Requirement(
        id="riskClassification",
        title="Applicable risk classification under the AI Act",
        kind="declared",
        frameworks=("AI Act art. 6", "AI Act annex III"),
        why="It determines which obligations apply to the deployment, not to the model "
        "in the abstract.",
    ),
    Requirement(
        id="technicalDocumentation",
        title="Technical documentation available for audit",
        kind="declared",
        frameworks=("AI Act art. 11", "AI Act annex IV"),
        why="It is the first piece of evidence an auditor asks for.",
    ),
    Requirement(
        id="nativeLogging",
        title="The deployment logs inferences in an auditable way",
        kind="declared",
        frameworks=("AI Act art. 12", "AI Act art. 26(6)", "ENS op.exp.8"),
        why="Without traces there is no way to reconstruct what the system answered and when.",
    ),
    Requirement(
        id="airGappedDeployment",
        title="The deployment can operate without internet access",
        kind="declared",
        frameworks=("ENS mp.com.1",),
        why="It is the reason a customer chooses on-prem. It is cross-checked against the "
        "network probe.",
    ),
    Requirement(
        id="logprobsAvailable",
        title="The engine exposes log-probabilities",
        kind="probed",
        frameworks=("AI Act art. 13(3)(b)(iv)",),
        why="Minimum explainability: it allows measuring the model's confidence in its output.",
        evidence_required=False,
    ),
    Requirement(
        id="reasoningExposed",
        title="The engine can expose the model's reasoning",
        kind="probed",
        frameworks=("AI Act art. 13(3)(b)(iv)",),
        why="Alternative minimum explainability when there are no logprobs.",
        evidence_required=False,
    ),
    Requirement(
        id="noExternalEgress",
        title="The engine opens no external connections during inference",
        kind="probed",
        frameworks=("ENS mp.com.1",),
        why="Cross-checks the air-gapped claim: telemetry or downloads would break it.",
        evidence_required=False,
    ),
)

DECLARED_REQUIREMENTS = tuple(r for r in REQUIREMENTS if r.kind == "declared")
PROBED_REQUIREMENTS = tuple(r for r in REQUIREMENTS if r.kind == "probed")


@dataclass(slots=True)
class Declaration:
    provider: str | None = None
    model: str | None = None
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None

    def get(self, requirement_id: str) -> dict[str, Any]:
        return self.items.get(requirement_id, {"status": "unknown", "evidence": None})


def template() -> dict[str, Any]:
    """Blank declaration, ready to fill in."""
    return {
        "provider": "",
        "model": "",
        "items": {
            requirement.id: {
                "status": "unknown",
                "evidence": "",
                "note": requirement.why,
            }
            for requirement in DECLARED_REQUIREMENTS
        },
    }


def load_declaration(path: Path) -> Declaration:
    try:
        # utf-8-sig: the declaration is filled in by a person, often on Windows.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise DeclarationError(f"Declaration does not exist: {path}") from None
    except json.JSONDecodeError as exc:
        raise DeclarationError(f"Invalid JSON in {path}: {exc}") from None

    items = payload.get("items")
    if not isinstance(items, dict):
        raise DeclarationError(f"{path}: missing the 'items' object")

    known = {requirement.id for requirement in DECLARED_REQUIREMENTS}
    for item_id, value in items.items():
        if item_id not in known:
            raise DeclarationError(
                f"{path}: unknown requirement {item_id!r}. "
                f"Valid ones: {', '.join(sorted(known))}"
            )
        if not isinstance(value, dict):
            raise DeclarationError(f"{path}: requirement {item_id!r} must be an object")
        status = value.get("status")
        if status not in STATUSES:
            raise DeclarationError(
                f"{path}: {item_id!r} has status {status!r}; "
                f"allowed: {', '.join(STATUSES)}"
            )

    return Declaration(
        provider=payload.get("provider") or None,
        model=payload.get("model") or None,
        items=items,
        path=path,
    )
