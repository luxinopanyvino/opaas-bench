"""Block E — Compliance readiness.

Half audited checklist, half executable probes. The rule that governs the whole
block: **what is declared and what is probed are never added together**. A
certificate that mixes "the provider says it documents its data" with "the
engine exposes logprobs, I verified it" is no use to an auditor.

With no declaration supplied, the block still runs: it executes the probes and
puts on record that the documentary part is uncovered. That is useful
information — it says exactly what the operator is missing in order to get
certified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from opaas_bench.adapters.base import InferenceBackend
from opaas_bench.declaration import (
    DECLARED_REQUIREMENTS,
    PROBED_REQUIREMENTS,
    REQUIREMENTS,
    Declaration,
    load_declaration,
)
from opaas_bench.suites.base import Suite
from opaas_bench.telemetry.network import EgressWatcher

_PROBE_PROMPT = "Reply with a single word: hello"


@dataclass(slots=True)
class ComplianceOptions:
    declaration: str | None = None
    egress_probe_requests: int = 3
    egress_interval_s: float = 0.25

    @classmethod
    def from_dict(cls, options: dict[str, Any]) -> ComplianceOptions:
        return cls(**{f: options[f] for f in cls.__slots__ if options.get(f) is not None})

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__slots__}


class ComplianceSuite(Suite):
    name: ClassVar[str] = "compliance"
    block: ClassVar[str] = "E"
    description: ClassVar[str] = "Audited checklist + explainability and air-gapped probes"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.opts = ComplianceOptions.from_dict(self.options)

    def evaluate(self, backend: InferenceBackend) -> tuple[dict[str, Any], list[str]]:
        limitations: list[str] = []

        declaration = Declaration()
        if self.opts.declaration:
            declaration = load_declaration(Path(self.opts.declaration))
        else:
            limitations.append(
                "No compliance declaration: the documentary part of block E is left "
                "uncovered. Generate a template with 'opaas-bench compliance --template'."
            )

        egress = self._probe_egress(backend)
        probes = {
            "logprobs": _probe_logprobs(backend),
            "reasoning": _probe_reasoning(backend),
            "egress": egress,
        }

        items = [_declared_item(r, declaration) for r in DECLARED_REQUIREMENTS]
        items += [_probed_item(r, probes) for r in PROBED_REQUIREMENTS]

        if egress["status"] == "unknown":
            limitations.append(
                f"Network isolation not verifiable: {egress['detail']}. "
                "For a strong claim, run the engine on an isolated network and check that it "
                "still responds."
            )
        else:
            limitations.append(
                "The network probe samples connections, it does not capture packets: a very "
                "brief connection can go unnoticed and UDP traffic does not show up."
            )

        contradiction = _airgap_contradiction(declaration, egress)
        if contradiction:
            limitations.append(contradiction)

        summary = _summarize(items)
        return (
            {
                "options": self.opts.as_dict(),
                "declarationSource": str(declaration.path) if declaration.path else None,
                "provider": declaration.provider,
                "summary": summary,
                "items": items,
                "probes": probes,
            },
            limitations,
        )

    def _probe_egress(self, backend: InferenceBackend) -> dict[str, Any]:
        """Generates a little load and watches whether the engine talks to the outside."""
        watcher = EgressWatcher(backend.server_pid(), self.opts.egress_interval_s).start()
        try:
            for _ in range(max(1, self.opts.egress_probe_requests)):
                try:
                    backend.generate(_PROBE_PROMPT, max_tokens=16, temperature=0.0)
                except Exception:  # noqa: BLE001 - the network probe does not depend on success
                    continue
        finally:
            report = watcher.stop()

        payload = report.as_dict()
        if not report.available:
            payload["status"] = "unknown"
            payload["detail"] = report.note or "the server process could not be observed"
        elif report.external:
            destinations = ", ".join(f"{e.address}:{e.port}" for e in report.external)
            payload["status"] = "fail"
            payload["detail"] = (
                f"The engine opened external connections during inference: {destinations}"
            )
        else:
            payload["status"] = "pass"
            payload["detail"] = (
                f"No external connections across {report.samples} samples during "
                f"{report.duration_s:.1f}s of inference"
            )
        return payload


def _probe_logprobs(backend: InferenceBackend) -> dict[str, Any]:
    """Does the engine return log-probabilities when asked for them?"""
    try:
        result = backend.generate(_PROBE_PROMPT, max_tokens=8, temperature=0.0, logprobs=1)
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "detail": f"The engine rejected the request with logprobs: {exc}"}

    payload = result.raw if isinstance(result.raw, dict) else {}
    if payload.get("logprobs") or payload.get("logProbs"):
        return {"status": "pass", "detail": "The engine returns logprobs"}
    return {
        "status": "fail",
        "detail": "The engine accepted the parameter but returned no logprobs in the response",
    }


def _probe_reasoning(backend: InferenceBackend) -> dict[str, Any]:
    """Can the model's reasoning be exposed?"""
    metadata = backend.get_backend_metadata()
    if metadata.get("reasoningSupported"):
        return {"status": "pass", "detail": "The adapter declares support for exposed reasoning"}
    return {
        "status": "unknown",
        "detail": (
            "The adapter does not declare support for exposed reasoning. A model can reason "
            "in its output without the engine separating it into a field of its own."
        ),
    }


def _declared_item(requirement: Any, declaration: Declaration) -> dict[str, Any]:
    declared = declaration.get(requirement.id)
    status = str(declared.get("status", "unknown"))
    evidence = declared.get("evidence") or None

    if status == "yes" and requirement.evidence_required and not evidence:
        status, note = "unsupported", "Declared as met but with NO evidence supplied"
    else:
        note = declared.get("note")

    return {
        "id": requirement.id,
        "title": requirement.title,
        "kind": requirement.kind,
        "frameworks": list(requirement.frameworks),
        "why": requirement.why,
        "status": status,
        "evidence": evidence,
        "note": note,
        # Explicit marker: the operator claims this, nobody has verified it.
        "verifiedByOpaasBench": False,
    }


def _probed_item(requirement: Any, probes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    probe = {
        "logprobsAvailable": probes["logprobs"],
        "reasoningExposed": probes["reasoning"],
        "noExternalEgress": probes["egress"],
    }[requirement.id]
    return {
        "id": requirement.id,
        "title": requirement.title,
        "kind": requirement.kind,
        "frameworks": list(requirement.frameworks),
        "why": requirement.why,
        "status": probe["status"],
        "evidence": probe.get("detail"),
        "note": None,
        "verifiedByOpaasBench": True,
    }


def _airgap_contradiction(declaration: Declaration, egress: dict[str, Any]) -> str | None:
    """The case that matters most: air-gapped is claimed and the engine calls out."""
    declared = declaration.get("airGappedDeployment").get("status")
    if declared in ("yes", "partial") and egress.get("status") == "fail":
        return (
            "CONTRADICTION: an air-gapped deployment is declared, but the probe observed "
            f"external connections during inference. {egress.get('detail')}"
        )
    return None


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    declared = [i for i in items if i["kind"] == "declared"]
    probed = [i for i in items if i["kind"] == "probed"]
    return {
        "declared": {
            "total": len(declared),
            "yes": sum(1 for i in declared if i["status"] == "yes"),
            "partial": sum(1 for i in declared if i["status"] == "partial"),
            "no": sum(1 for i in declared if i["status"] == "no"),
            "unknown": sum(1 for i in declared if i["status"] == "unknown"),
            "unsupported": sum(1 for i in declared if i["status"] == "unsupported"),
        },
        "probed": {
            "total": len(probed),
            "pass": sum(1 for i in probed if i["status"] == "pass"),
            "fail": sum(1 for i in probed if i["status"] == "fail"),
            "unknown": sum(1 for i in probed if i["status"] == "unknown"),
        },
        "note": (
            "The declared items are the operator's claims with their evidence; only the "
            "probed ones have been verified by OPAAS-Bench. They are not added together."
        ),
    }


__all__ = ["ComplianceSuite", "ComplianceOptions", "REQUIREMENTS"]
