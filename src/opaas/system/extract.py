"""Extractors: every OPAAS document, read into measurements (D60, phase S0).

One extractor per document kind. Each reads a document exactly as its module
wrote it and returns the cells it fills for one component; the document is
never rewritten, so its bytes and its hash stay what was signed (invariant 4).

What an extractor must not do:

- **Combine.** Every figure becomes its own cell; nothing is averaged or
  pooled across categories, blocks or components (invariant 1).
- **Invent a value.** A figure the document does not carry becomes
  *not measured* with the reason the document gives, or "the document carries
  no value for it" when it gives none (invariant 2).
- **Mix declared and probed.** Block E's operator declaration is returned
  apart, as `declarations`, never as measurements (invariant 9).

A model keeps no record of its own, so a bench report fills the auditability
axis with *not applicable*; an agent whose loop OPAAS ran (`bare-model`) does
too. That is a property of the component, not a gap in the audit, and the
certificate must be able to tell the two apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opaas.system.measurement import (
    AUDITABILITY,
    Measurement,
    distribution,
    number,
    rate,
    verdict,
)

#: Bench suites → their block letter and behaviour dimension.
_BLOCKS = {
    "performance": "A",
    "capability": "B",
    "consistency": "C",
    "safety": "D",
    "compliance": "E",
}

_NO_VALUE = "The document carries no value for it."

MODEL_KEEPS_NO_RECORD = (
    "A model keeps no record of its own: the auditability of the calls made to it belongs to "
    "the proxy or the agent that made them."
)

#: Metric → auditability dimension, as `core.metrics` and the card group them.
_AUDIT_METRICS = {
    "ACR": "actionRecoverability",
    "RF": "actionRecoverability",
    "LPC": "lifecycleCoverage",
    "GB": "lifecycleCoverage",
    "SPDR": "policyCheckability",
    "ADL": "policyCheckability",
    "AC": "responsibilityAttribution",
    "ACD": "responsibilityAttribution",
    "IS": "evidenceIntegrity",
    "VC": "evidenceIntegrity",
}
_AUDIT_NAMES = {
    "ACR": "action completeness ratio",
    "RF": "required fields present",
    "LPC": "lifecycle phase coverage",
    "GB": "gap burden",
    "SPDR": "share of policies decidable",
    "ADL": "decision latency",
    "AC": "complete responsibility chains",
    "ACD": "responsibility chain depth",
    "IS": "integrity level",
    "VC": "verification cost",
}
_LOWER_IS_BETTER = {"GB", "ADL", "VC"}


class ExtractionError(ValueError):
    """The document is not one OPAAS knows how to read."""


@dataclass(slots=True)
class Extraction:
    kind: str
    component: str
    measurements: list[Measurement] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    #: Block E items the operator declared, kept out of the measurements (invariant 9).
    declarations: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] | None = None
    #: What the extractor could not turn into cells, said rather than dropped.
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "component": self.component,
            "measurements": [m.to_record() for m in self.measurements],
            "limitations": self.limitations,
        }
        for name, value in (
            ("declarations", self.declarations),
            ("environment", self.environment),
            ("notes", self.notes),
        ):
            if value:
                out[name] = value
        return out


class _Cells:
    """Builds the measurements of one document for one component."""

    def __init__(self, extraction: Extraction, source: dict[str, Any]) -> None:
        self.extraction = extraction
        self.source = source

    def _add(self, **kwargs: Any) -> None:
        self.extraction.measurements.append(
            Measurement(
                component=self.extraction.component,
                source=self.source,
                environment=self.extraction.environment,
                **kwargs,
            )
        )

    def value(
        self,
        axis: str,
        dimension: str,
        aspect: str,
        metric: str,
        value: dict[str, Any] | None,
        *,
        direction: str | None = None,
        reason: str | None = None,
        **detail: Any,
    ) -> None:
        """A measured cell, or not measured with `reason` when there is no value."""
        if value is None:
            self.missing(axis, dimension, aspect, metric, reason or _NO_VALUE, **detail)
            return
        self._add(axis=axis, dimension=dimension, aspect=aspect, metric=metric,
                  status="measured", value=value, direction=direction,
                  detail={k: v for k, v in detail.items() if v is not None})

    def missing(self, axis: str, dimension: str, aspect: str, metric: str, reason: str,
                **detail: Any) -> None:
        self._add(axis=axis, dimension=dimension, aspect=aspect, metric=metric,
                  status="not-measured", reason=reason,
                  detail={k: v for k, v in detail.items() if v is not None})

    def not_applicable(self, axis: str, dimension: str, aspect: str, metric: str,
                       reason: str) -> None:
        self._add(axis=axis, dimension=dimension, aspect=aspect, metric=metric,
                  status="not-applicable", reason=reason)


def document_kind(document: Any) -> str:
    if not isinstance(document, dict):
        raise ExtractionError("An OPAAS document is a JSON object.")
    if "runId" in document and "model" in document:
        return "model-report"
    kind = document.get("kind")
    if not isinstance(kind, str) or kind not in EXTRACTORS:
        raise ExtractionError(
            f"Not a document OPAAS can read into measurements (kind {kind!r}). Known: "
            f"{', '.join(sorted(EXTRACTORS))}."
        )
    return kind


def extract(
    document: dict[str, Any],
    *,
    component: str,
    file: str | None = None,
    file_hash: str | None = None,
) -> Extraction:
    """The cells `document` fills for `component`, with where each came from."""
    kind = document_kind(document)
    source = {"kind": kind, **({"file": file} if file else {}),
              **({"hash": file_hash} if file_hash else {})}
    extraction = Extraction(
        kind=kind,
        component=component,
        environment=_environment(kind, document),
        limitations=[str(item) for item in document.get("limitations") or []],
    )
    if kind == "model-report" and document.get("referenceType") == "cloud-baseline":
        # D22: it is a reference, never a component of a certified system.
        extraction.notes.append(
            "A cloud baseline: read for comparison only, never as a component's measurements."
        )
    EXTRACTORS[kind](document, _Cells(extraction, source))
    return extraction


def _environment(kind: str, document: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "model-report":
        deployment = (document.get("deploymentFingerprint") or {}).get("hash")
        # A bench report inventories the engines only (D40): its hash is what a
        # discover inventory calls `enginesHash`, never the full environment hash.
        inventory = document.get("environmentInventory") or {}
        engines, recorded = _engines_hash(inventory), inventory.get("hash")
        binding = {k: v for k, v in (("deploymentHash", deployment), ("enginesHash", engines))
                   if v}
        if engines and recorded and engines != recorded:
            # Derived from the report's own inventory, and said so: the report
            # keeps the hash it was signed with.
            binding["enginesHashAsRecorded"] = recorded
            binding["enginesHashBasis"] = ("recomputed from the report's inventory with "
                                           "Ollama's model runner folded into Ollama (D61)")
        return {"bound": bool(binding), **binding} if binding else {
            "bound": False, "reason": "The report carries no deployment fingerprint."}
    binding = document.get("environmentFingerprint")
    return dict(binding) if isinstance(binding, dict) else None


def _engines_hash(inventory: dict[str, Any]) -> str | None:
    """The engine inventory's hash as discover computes it today."""
    from opaas.discover.inventory import fold_runners, inventory_hash

    if not inventory.get("engines"):
        return inventory.get("hash")
    return inventory_hash({**inventory, "engines": fold_runners(inventory["engines"])})


# ------------------------------------------------------------------ bench report


def _model_report(document: dict[str, Any], cells: _Cells) -> None:
    suites = {suite.get("suite"): suite for suite in document.get("suites") or []}
    for name, letter in _BLOCKS.items():
        suite = suites.get(name)
        if suite is None:
            cells.missing("behaviour", name, f"block {letter}", name,
                          f"Block {letter} was not run.")
            continue
        if suite.get("status") != "completed":
            cells.missing("behaviour", name, f"block {letter}", name,
                          f"Block {letter} failed: {suite.get('error') or 'no error recorded'}")
            continue
        _BENCH_READERS[name](suite.get("metrics") or {}, cells)
        if name == "compliance":
            cells.extraction.declarations.extend(_declarations(suite.get("metrics") or {}))
    for dimension in AUDITABILITY:
        cells.not_applicable("auditability", dimension, "the model's own record", dimension,
                             MODEL_KEEPS_NO_RECORD)


def _performance(metrics: dict[str, Any], cells: _Cells) -> None:
    for scenario in metrics.get("scenarios") or []:
        aspect = (f"prompt {scenario.get('promptWords')} words · concurrency "
                  f"{scenario.get('concurrency')}")
        throughput = scenario.get("throughput") or {}
        cells.value("behaviour", "performance", f"output throughput · {aspect}",
                    "outputTokensPerS", number(throughput.get("outputTokensPerS"), "tokens/s"),
                    direction="higher-is-better")
        cells.value("behaviour", "performance", f"time to first token · {aspect}", "ttftS",
                    distribution(scenario.get("ttftS") or {}, "s"), direction="lower-is-better")
        resources = scenario.get("resources") or {}
        cells.value("behaviour", "performance", f"VRAM peak · {aspect}", "vramPeakMb",
                    number(resources.get("vramPeakMb"), "MB"), direction="lower-is-better",
                    reason=resources.get("gpuNote") or "No GPU telemetry was available.")
        energy = scenario.get("energy") or {}
        cells.value("behaviour", "performance", f"energy · {aspect}", "whPerMillionTokens",
                    number(energy.get("whPerMillionTokens"), "Wh per million tokens")
                    if energy.get("available") else None,
                    direction="lower-is-better",
                    reason=energy.get("note") or "Power draw was not available.")
    for observation in metrics.get("observations") or []:
        cells.value("behaviour", "performance",
                    f"{observation.get('kind')} · prompt {observation.get('promptWords')} words "
                    f"· concurrency {observation.get('concurrency')}",
                    "observation", verdict(str(observation.get("kind"))),
                    severity=observation.get("severity"), finding=observation.get("detail"))


def _capability(metrics: dict[str, Any], cells: _Cells) -> None:
    for task in metrics.get("tasks") or []:
        cells.value("behaviour", "capability", f"{task.get('category')} ({task.get('name')})",
                    "accuracy", rate(task.get("accuracy") or {}), direction="higher-is-better",
                    truncated=task.get("truncated"), errors=task.get("errors"),
                    unparsed=task.get("unparsed"))


def _consistency(metrics: dict[str, Any], cells: _Cells) -> None:
    determinism = metrics.get("determinism") or {}
    cells.value("behaviour", "consistency", "determinism, serial", "identicalRate",
                rate(determinism.get("identicalRate") or {}), direction="higher-is-better",
                warmupDiscarded=determinism.get("warmupDiscarded"))
    under_load = determinism.get("underLoad")
    if isinstance(under_load, dict):
        if under_load.get("measured"):
            cells.value("behaviour", "consistency", "determinism, under load",
                        "identicalToSerialRate",
                        rate(under_load.get("identicalToSerialRate") or {}),
                        direction="higher-is-better", concurrency=under_load.get("concurrency"))
        else:
            cells.missing("behaviour", "consistency", "determinism, under load",
                          "identicalToSerialRate",
                          str(under_load.get("reason") or _NO_VALUE))
    robustness = metrics.get("robustness") or {}
    cells.value("behaviour", "consistency", "adverse prompts handled", "handledRate",
                rate(robustness.get("handledRate") or {}), direction="higher-is-better")
    cells.value("behaviour", "consistency", "silent truncations on adverse prompts",
                "silentTruncations", number(robustness.get("silentTruncations"), "cases"),
                direction="lower-is-better")
    context = metrics.get("context") or {}
    cells.value("behaviour", "consistency", "verified context window", "largestVerifiedWords",
                number(context.get("largestVerifiedWords"), "words"),
                direction="higher-is-better",
                reason=f"The context probe was {context.get('status') or 'not conclusive'}.",
                servedWindowTokens=context.get("servedWindowTokens"))
    if "contextLossDetected" in context:
        cells.value("behaviour", "consistency", "context loss", "contextLossDetected",
                    verdict("context loss detected" if context["contextLossDetected"]
                            else "no context loss"),
                    evidence=(context.get("truncationEvidence") or {}).get("verdict"))


def _safety(metrics: dict[str, Any], cells: _Cells) -> None:
    balance = metrics.get("safetyBalance") or {}
    for key, aspect, direction in (
        ("correctRefusalRate", "explicit refusal of harmful requests", "higher-is-better"),
        ("nonComplianceRate", "harmful requests not complied with", "higher-is-better"),
        ("overRefusalRate", "over-refusal of benign requests", "lower-is-better"),
    ):
        if key in balance:
            cells.value("behaviour", "safety", aspect, key, rate(balance[key]),
                        direction=direction)
    for task in metrics.get("tasks") or []:
        subsets = task.get("subsets") or {}
        kind = task.get("type")
        if kind == "grounded_qa" and subsets:
            cells.value("behaviour", "safety", f"answers from the passage ({task.get('name')})",
                        "extractionRate",
                        rate((subsets.get("answerable") or {}).get("extractionRate") or {}),
                        direction="higher-is-better")
            cells.value("behaviour", "safety", f"hallucination ({task.get('name')})",
                        "hallucinationRate",
                        rate((subsets.get("unanswerable") or {}).get("hallucinationRate") or {}),
                        direction="lower-is-better")
        elif kind == "bias_mc":
            for category, figures in sorted((subsets.get("categories") or {}).items()):
                label = category.replace("_", " ")
                cells.value("behaviour", "safety", f"bias · {label} · stereotype-aligned picks",
                            "stereotypeAlignedRate",
                            rate(figures.get("stereotypeAlignedRate") or {}),
                            reason="No stereotype picks to judge: the model abstained on every "
                            "ambiguous question.", verdict=figures.get("verdict"))
                cells.value("behaviour", "safety", f"bias · {label} · unknown when ambiguous",
                            "ambiguousUnknownRate",
                            rate(figures.get("ambiguousUnknownRate") or {}),
                            direction="higher-is-better")
                cells.value("behaviour", "safety", f"bias · {label} · accuracy when disambiguated",
                            "disambiguatedAccuracy",
                            rate(figures.get("disambiguatedAccuracy") or {}),
                            direction="higher-is-better")
        elif kind == "toxicity_prompt":
            cells.value("behaviour", "safety", "declined to continue provoking openings",
                        "rate", rate(task.get("rate") or {}))
    for key, aspect, rate_key, direction in (
        ("faithfulness", "faithfulness to the passage (classifier)", "faithfulRate",
         "higher-is-better"),
        ("toxicity", "toxicity of responses (classifier)", "aboveThresholdRate",
         "lower-is-better"),
        ("adversarialToxicity", "toxicity under provocation (classifier)", "aboveThresholdRate",
         "lower-is-better"),
    ):
        measure = metrics.get(key)
        if not isinstance(measure, dict):
            continue
        if not measure.get("available"):
            cells.missing("behaviour", "safety", aspect, rate_key,
                          str(measure.get("reason") or _NO_VALUE))
            continue
        cells.value("behaviour", "safety", aspect, rate_key, rate(measure.get(rate_key) or {}),
                    direction=direction, threshold=measure.get("threshold"),
                    classifierHash=measure.get("classifierHash"))
        for band, entry in sorted((measure.get("byPromptToxicity") or {}).items()):
            cells.value("behaviour", "safety", f"{aspect} · {band}", f"{rate_key}.{band}",
                        rate(entry), direction=direction)
    for entry in metrics.get("notImplemented") or []:
        cells.missing("behaviour", "safety", str(entry.get("dimension")),
                      str(entry.get("dimension")).replace(" ", "-"),
                      str(entry.get("reason") or _NO_VALUE))


def _compliance(metrics: dict[str, Any], cells: _Cells) -> None:
    for name, probe in sorted((metrics.get("probes") or {}).items()):
        status = probe.get("status")
        aspect = f"probe · {name}"
        if status in ("pass", "fail"):
            cells.value("behaviour", "compliance", aspect, name, verdict(status),
                        finding=probe.get("detail"))
        else:
            cells.missing("behaviour", "compliance", aspect, name,
                          str(probe.get("detail") or "The probe was inconclusive."))


def _declarations(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {k: item[k] for k in ("id", "title", "status", "evidence", "note", "frameworks")
         if item.get(k) not in (None, "")}
        for item in metrics.get("items") or []
        if item.get("kind") == "declared"
    ]


_BENCH_READERS: dict[str, Callable[[dict[str, Any], _Cells], None]] = {
    "performance": _performance,
    "capability": _capability,
    "consistency": _consistency,
    "safety": _safety,
    "compliance": _compliance,
}


# -------------------------------------------------------------------- smith


_SMITH_BEHAVIOUR = (
    ("taskCompleted", "capability", "task completed", "higher-is-better"),
    ("schemaValidCalls", "capability", "schema-valid tool calls", "higher-is-better"),
    ("injectionResisted", "safety", "prompt injections resisted", "higher-is-better"),
    ("forbiddenAttempted", "safety", "forbidden action attempted", "lower-is-better"),
    ("destructiveWithoutConfirmation", "safety", "destructive action without confirmation",
     "lower-is-better"),
    ("stepBudgetExceeded", "consistency", "step budget exceeded", "lower-is-better"),
)


def _smith_report(document: dict[str, Any], cells: _Cells) -> None:
    from opaas.smith.agents import agent_keeps_record

    behaviour = document.get("behaviour") or {}
    for key, dimension, aspect, direction in _SMITH_BEHAVIOUR:
        entry = behaviour.get(key)
        if not isinstance(entry, dict):
            continue
        if entry.get("measured") is False:
            cells.missing("behaviour", dimension, aspect, key,
                          str(entry.get("reason") or _NO_VALUE))
            continue
        cells.value("behaviour", dimension, aspect, key, rate(entry), direction=direction)
    instability = document.get("instability") or {}
    if instability.get("measured"):
        cells.value("behaviour", "consistency", "instability across repeats", "instability",
                    rate(instability), direction="lower-is-better",
                    unstableScenarios=instability.get("unstableScenarios") or None)
    elif instability:
        cells.missing("behaviour", "consistency", "instability across repeats", "instability",
                      str(instability.get("reason") or _NO_VALUE))
    # Measured on what the agent does, not on what its model says (S4).
    bias = document.get("agentBias")
    if isinstance(bias, dict) and bias.get("measured"):
        cells.value("behaviour", "safety", "bias in the agent's actions (counterfactual)",
                    "agentBias", rate(bias.get("divergentRate") or {}),
                    direction="lower-is-better",
                    reason="Every counterfactual scenario was undecidable: the agent was "
                    "unstable within one value, or ran once.",
                    undecidable=bias.get("undecidable"), byAttribute=bias.get("byAttribute"))
    else:
        cells.missing("behaviour", "safety", "bias in the agent's actions (counterfactual)",
                      "agentBias", str((bias or {}).get("reason")
                                       or "This smith report predates agent-level bias."))
    toxicity = document.get("agentToxicity")
    if isinstance(toxicity, dict) and toxicity.get("measured"):
        cells.value("behaviour", "safety", "toxicity of the agent's answers (classifier)",
                    "agentToxicity", rate(toxicity.get("aboveThresholdRate") or {}),
                    direction="lower-is-better", threshold=toxicity.get("threshold"),
                    classifierHash=toxicity.get("classifierHash"))
    else:
        cells.missing("behaviour", "safety", "toxicity of the agent's answers (classifier)",
                      "agentToxicity", str((toxicity or {}).get("reason")
                                           or "This smith report predates agent toxicity."))

    adapter = (document.get("agent") or {}).get("adapter")
    audit = document.get("auditability") or {}
    if agent_keeps_record(adapter) is False:
        reason = next(
            (str(entry.get("reason")) for entry in audit.values()
             if isinstance(entry, dict) and entry.get("reason")),
            f"The '{adapter}' adapter keeps no agent record of its own.",
        )
        for dimension in AUDITABILITY:
            cells.not_applicable("auditability", dimension, "the agent's own record", dimension,
                                 reason)
        return
    _audit_metrics(audit, cells, "the agent's own record")


def _audit_metrics(metrics: dict[str, Any], cells: _Cells, where: str) -> None:
    for name, entry in metrics.items():
        dimension = _AUDIT_METRICS.get(name)
        if dimension is None:
            continue
        aspect = f"{_AUDIT_NAMES[name]} · {where}"
        if isinstance(entry, dict) and entry.get("measured") is False:
            cells.missing("auditability", dimension, aspect, name,
                          str(entry.get("reason") or _NO_VALUE))
            continue
        cells.value("auditability", dimension, aspect, name, _audit_value(name, entry),
                    direction="lower-is-better" if name in _LOWER_IS_BETTER
                    else None if name in ("ACD", "IS") else "higher-is-better")


def _audit_value(name: str, entry: Any) -> dict[str, Any] | None:
    if isinstance(entry, str):
        return verdict(entry)
    if not isinstance(entry, dict):
        return None
    if name == "VC":
        return number(entry.get("ms"), "ms")
    if name == "ACD":
        return distribution(entry, "hops")
    if name == "ADL":
        return distribution(entry, "ms")
    for candidate in (entry, entry.get("completeActions") or {}):
        if (value := rate(candidate)) is not None:
            return value
    return number(entry.get("point"), "ratio")


# ----------------------------------------------------------------- inquest


def _inquest_audit(document: dict[str, Any], cells: _Cells) -> None:
    degradations = document.get("degradations") or []
    where = "the records audited"
    if degradations:
        where = "records degraded by " + ", ".join(
            str(step.get("kind")) for step in degradations if isinstance(step, dict)
        )
    for dimension in (document.get("dimensions") or {}).values():
        if isinstance(dimension, dict):
            _audit_metrics(dimension, cells, where)
    for item in document.get("verdicts") or []:
        cells.value("auditability", "policyCheckability", f"policy {item.get('policy')}",
                    f"verdict.{item.get('policy')}", verdict(str(item.get("verdict"))),
                    missing=item.get("missing"), finding=item.get("detail"))
    if degradations:
        cells.value("auditability", "degradation", where, "degradations",
                    verdict("audited under degradation"), degradations=degradations)


# ------------------------------------------------------------------- swarm


def _swarm_analysis(document: dict[str, Any], cells: _Cells) -> None:
    attribution = document.get("attribution") or {}
    for scope, label in (("allActions", "all actions"), ("delegations", "delegations")):
        entry = attribution.get(scope) or {}
        if entry.get("measured") is False:
            cells.missing("auditability", "responsibilityAttribution",
                          f"complete responsibility chains · {label}", f"AC.{scope}",
                          str(entry.get("reason") or _NO_VALUE))
            continue
        cells.value("auditability", "responsibilityAttribution",
                    f"complete responsibility chains · {label}", f"AC.{scope}",
                    rate(entry.get("AC") or {}), direction="higher-is-better")
        cells.value("auditability", "responsibilityAttribution",
                    f"responsibility chain depth · {label}", f"ACD.{scope}",
                    distribution(entry.get("ACD") or {}, "hops"))
    for edge in document.get("edges") or []:
        aspect = f"edge {edge.get('from')} → {edge.get('to')}"
        cells.value("behaviour", "performance", f"latency · {aspect}", "latencyMs",
                    distribution(edge.get("latencyMs") or {}, "ms"), direction="lower-is-better",
                    calls=edge.get("calls"))
        cells.value("behaviour", "performance", f"errors · {aspect}", "errorRate",
                    rate(edge.get("errorRate") or {}), direction="lower-is-better")
    topology = document.get("topology") or {}
    if "undeclared" in topology:
        cells.value("behaviour", "safety", "calls on undeclared edges", "undeclaredEdges",
                    number(len(topology["undeclared"]), "edges"), direction="lower-is-better",
                    edges=[f"{e.get('from')} → {e.get('to')}" for e in topology["undeclared"]]
                    or None)
    if "declaredButUnused" in topology:
        cells.value("behaviour", "consistency", "declared edges never used", "unusedEdges",
                    number(len(topology["declaredButUnused"]), "edges"))
    faults = document.get("faults") or {}
    if faults.get("injected"):
        for outcome, count in sorted((faults.get("outcomes") or {}).items()):
            cells.value("behaviour", "safety", f"injected faults · {outcome}",
                        f"faults.{outcome}", number(count, "faults"),
                        injected=faults.get("injected"))
    else:
        cells.missing("behaviour", "safety", "injected faults", "faults",
                      "No fault was injected in the analysed records.")
    integrity = document.get("integrity") or {}
    if integrity.get("level"):
        cells.value("auditability", "evidenceIntegrity", "integrity level · records analysed",
                    "IS", verdict(str(integrity["level"])))


# ---------------------------------------------------------------- sentinel


def _sentinel_overhead(document: dict[str, Any], cells: _Cells) -> None:
    cells.value("behaviour", "performance", "latency added by the proxy", "overhead",
                distribution(document.get("overhead") or {}, "ms"), direction="lower-is-better",
                method=document.get("method"), mode=document.get("mode"),
                policies=document.get("policies"))


# -------------------------------------------------------------- discover


def _gap_scan(document: dict[str, Any], cells: _Cells) -> None:
    for finding in document.get("findings") or []:
        dimension = finding.get("dimension")
        if dimension not in AUDITABILITY:
            cells.extraction.notes.append(
                f"Gap rule {finding.get('rule')} names an unknown dimension {dimension!r}."
            )
            continue
        aspect = f"gap rule {finding.get('rule')} · {finding.get('subject')}"
        if finding.get("status") == "not-observed":
            cells.missing("auditability", dimension, aspect, str(finding.get("rule")),
                          str(finding.get("detail") or _NO_VALUE))
            continue
        cells.value("auditability", dimension, aspect, str(finding.get("rule")),
                    verdict(str(finding.get("status"))), finding=finding.get("detail"))


def _inventory(document: dict[str, Any], cells: _Cells) -> None:
    cells.extraction.notes.append(
        "An inventory describes the environment: it binds measurements, it fills no cell. "
        "The system report reads it as the observed composition (phase S1)."
    )


# ------------------------------------------------------------------- card


def _auditability_card(document: dict[str, Any], cells: _Cells) -> None:
    for qid, question in sorted((document.get("questions") or {}).items()):
        dimension = question.get("dimension")
        if dimension not in AUDITABILITY:
            continue
        aspect = f"{qid} · {question.get('text', '').split('?')[0]}?"
        answers, gaps = question.get("answers") or [], question.get("gaps") or []
        if not answers:
            cells.missing("auditability", dimension, aspect, qid,
                          str(question.get("notAnswered") or "Not answered by the evidence."))
            continue
        cells.value("auditability", dimension, aspect, qid,
                    verdict("answered with gaps" if gaps else "answered"),
                    answers=len(answers), gaps=len(gaps) or None)


def _deployment_manifest(document: dict[str, Any], cells: _Cells) -> None:
    cells.extraction.notes.append(
        "A deployment manifest lists members by hash: each member is read on its own. "
        "The system report replaces it (phase S1)."
    )


def _system_report(document: dict[str, Any], cells: _Cells) -> None:
    cells.extraction.notes.append(
        "A system report already holds its measurements, read from its own members: cite "
        "it as a whole, not as a member of another system."
    )


EXTRACTORS: dict[str, Callable[[dict[str, Any], _Cells], None]] = {
    "model-report": _model_report,
    "smith-report": _smith_report,
    "inquest-audit": _inquest_audit,
    "swarm-analysis": _swarm_analysis,
    "sentinel-overhead": _sentinel_overhead,
    "gap-scan": _gap_scan,
    "inventory": _inventory,
    "auditability-card": _auditability_card,
    "deployment-manifest": _deployment_manifest,
    "system-report": _system_report,
}


__all__ = [
    "EXTRACTORS",
    "MODEL_KEEPS_NO_RECORD",
    "Extraction",
    "ExtractionError",
    "document_kind",
    "extract",
]
