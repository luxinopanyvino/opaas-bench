"""The system audit's formats (D60, phase S0): manifest, measurement, extractors."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opaas.cli.main import app
from opaas.system import (
    EXTRACTORS,
    ExtractionError,
    MeasurementError,
    SystemManifestError,
    check,
    extract,
    load_manifest,
)

runner = CliRunner()
DOCS = Path(__file__).resolve().parent.parent / "docs" / "system-audit.md"
RATE = {"point": 0.5, "low": 0.3, "high": 0.7, "n": 20}


def _design_example(language: str, after: str) -> dict:
    """The first `language` block after the heading `after` in system-audit.md."""
    text = DOCS.read_text(encoding="utf-8")
    block = re.search(rf"```{language}\n(.*?)```", text[text.index(after):], re.S)
    return json.loads(block.group(1))


# --------------------------------------------------------------- measurement


def _record(**overrides):
    record = {
        "component": "llm", "axis": "behaviour", "dimension": "safety",
        "aspect": "hallucination", "metric": "hallucinationRate", "status": "measured",
        "value": {"type": "rate", **RATE}, "source": {"kind": "model-report"},
    }
    record.update(overrides)
    return {k: v for k, v in record.items() if v is not None}


def test_the_design_example_is_a_valid_measurement():
    check(_design_example("json", "## 4. One result format"))


def test_a_measured_cell_needs_a_value_and_an_unmeasured_one_a_reason():
    check(_record())
    with pytest.raises(MeasurementError, match="must say why"):
        check(_record(status="not-measured", value=None))
    check(_record(status="not-applicable", value=None, reason="A model keeps no record."))


def test_an_unmeasured_cell_cannot_carry_a_zero():
    """Invariant 2: a zero next to 'not measured' reads as measured."""
    with pytest.raises(MeasurementError, match="carries no value"):
        check(_record(status="not-measured", reason="no classifier",
                      value={"type": "rate", "point": 0, "low": 0, "high": 0, "n": 0}))


def test_a_rate_travels_with_its_interval():
    """Invariant 11 compares intervals: a bare point cannot be compared."""
    with pytest.raises(MeasurementError, match="point, low, high and n"):
        check(_record(value={"type": "rate", "point": 0.5}))


def test_a_dimension_belongs_to_its_axis():
    with pytest.raises(MeasurementError, match="not a dimension of the behaviour axis"):
        check(_record(dimension="evidenceIntegrity"))


# ------------------------------------------------------------------ manifest


def _manifest(**overrides):
    document = {
        "format": 1, "id": "assistant", "purpose": "Drafts summaries.",
        "components": [
            {"id": "llm", "kind": "model", "backend": "ollama", "model": "qwen2.5:7b-instruct"},
            {"id": "agent", "kind": "agent", "adapter": "bare-model"},
        ],
        "flows": [{"from": "agent", "to": "llm"}],
    }
    document.update(overrides)
    return document


def test_the_design_example_manifest_validates():
    manifest = load_manifest(_design_example("json", "## 2. The subject"),
                             known_adapters=("bare-model", "http"))
    assert manifest.component("llm").kind == "model"
    assert [f["via"] for f in manifest.flows if "via" in f] == ["gateway"]


def test_an_unknown_key_is_refused_not_ignored():
    """A misspelt 'scenario' silently dropped would leave every agent uncertifiable."""
    with pytest.raises(SystemManifestError, match="Unknown key"):
        load_manifest(_manifest(scenario=["s.json"]))


def test_the_purpose_is_required():
    with pytest.raises(SystemManifestError, match="intended use"):
        load_manifest(_manifest(purpose=" "))


@pytest.mark.parametrize("component, message", [
    ({"id": "llm", "kind": "model", "backend": "ollama"}, "needs model"),
    ({"id": "x", "kind": "robot"}, "kind must be one of"),
    ({"id": "web", "kind": "agent", "adapter": "http"}, "needs an 'endpoint'"),
    ({"id": "cloud", "kind": "external"}, "needs provider"),
    ({"id": "Bad Id", "kind": "tool"}, "lowercase"),
])
def test_a_component_must_name_what_reaches_it(component, message):
    with pytest.raises(SystemManifestError, match=message):
        load_manifest(_manifest(components=[component]))


def test_ids_are_unique_and_flows_name_components():
    twice = [{"id": "a", "kind": "tool"}, {"id": "a", "kind": "tool"}]
    with pytest.raises(SystemManifestError, match="used twice"):
        load_manifest(_manifest(components=twice, flows=[]))
    with pytest.raises(SystemManifestError, match="not a component"):
        load_manifest(_manifest(flows=[{"from": "agent", "to": "nowhere"}]))


def test_an_adapter_is_accepted_before_it_exists_and_mcp_now_does(monkeypatch):
    """The manifest must not change the day an adapter lands (§10.5): MCP landed in S4."""
    import opaas.system.manifest as manifest_module

    components = [{"id": "agent", "kind": "agent", "adapter": "mcp", "endpoint": "http://x"}]
    manifest = load_manifest(_manifest(components=components, flows=[]),
                             known_adapters=("bare-model", "http", "mcp"))
    assert manifest.pending() == []
    monkeypatch.setattr(manifest_module, "PENDING_ADAPTERS", frozenset({"a2a"}))
    future = [{"id": "agent", "kind": "agent", "adapter": "a2a"}]
    manifest = load_manifest(_manifest(components=future, flows=[]),
                             known_adapters=("bare-model", "http", "mcp"))
    assert manifest.pending() == [{"component": "agent", "adapter": "a2a"}]
    with pytest.raises(SystemManifestError, match="adapter must be one of"):
        load_manifest(_manifest(components=[{"id": "agent", "kind": "agent",
                                             "adapter": "langchain"}], flows=[]),
                      known_adapters=("bare-model", "http"))


# ---------------------------------------------------------------- extractors


def _suite(name, metrics, status="completed"):
    return {"suite": name, "status": status, "metrics": metrics, "error": None}


BENCH = {
    "runId": "20261014-001", "model": {"name": "m"}, "limitations": ["a limitation"],
    "deploymentFingerprint": {"hash": "sha256:dep"},
    "suites": [
        _suite("capability", {"tasks": [{"name": "ifeval", "category": "instruction-following",
                                         "accuracy": RATE}]}),
        _suite("consistency", {}, status="failed") | {"error": "engine down"},
        _suite("safety", {
            "safetyBalance": {"correctRefusalRate": RATE},
            "toxicity": {"available": False, "task": "toxicity", "reason": "No classifier."},
            "tasks": [{"type": "bias_mc", "name": "bbq", "subsets": {"categories": {
                "Age": {"stereotypeAlignedRate": RATE, "ambiguousUnknownRate": RATE,
                        "disambiguatedAccuracy": RATE, "verdict": "no difference detected"},
                "SES": {"stereotypeAlignedRate": {"n": 0}, "ambiguousUnknownRate": RATE,
                        "disambiguatedAccuracy": RATE}}}}],
            "notImplemented": [{"dimension": "adversarial toxicity", "reason": "No rtp."}],
        }),
        _suite("compliance", {
            "probes": {"egress": {"status": "pass"}, "reasoning": {"status": "unknown",
                                                                   "detail": "undeclared"}},
            "items": [{"id": "licenseDocumented", "kind": "declared", "status": "yes"},
                      {"id": "logprobsAvailable", "kind": "probed", "status": "pass"}],
        }),
    ],
}
SMITH = {
    "kind": "smith-report", "agent": {"adapter": "bare-model"},
    "behaviour": {"taskCompleted": {**RATE, "measured": True},
                  "injectionResisted": {"measured": False, "reason": "No injection scenario."}},
    "instability": {"measured": True, **RATE},
    "auditability": {"ACR": {"measured": False, "reason": "OPAAS ran the loop."}},
}
DOCUMENTS = {
    "model-report": BENCH,
    "smith-report": SMITH,
    "inquest-audit": {
        "kind": "inquest-audit", "environmentFingerprint": {"bound": False, "reason": "none"},
        "dimensions": {
            "actionRecoverability": {"ACR": {"measured": False, "reason": "No ground truth."},
                                     "RF": {"measured": True, "point": 1, "n": 4}},
            "responsibilityAttribution": {"AC": RATE, "ACD": {"p50": 2, "p95": 2}},
            "evidenceIntegrity": {"IS": "signed", "VC": {"ms": 2.8, "bytes": 10}},
        },
        "verdicts": [{"policy": "no-shell", "verdict": "undecidable", "missing": ["target"]}],
    },
    "swarm-analysis": {
        "kind": "swarm-analysis",
        "attribution": {"allActions": {"measured": True, "AC": RATE, "ACD": {"p50": 2}},
                        "delegations": {"measured": False, "reason": "No delegation."}},
        "edges": [{"from": "agent:a", "to": "tool:t", "calls": 3,
                   "latencyMs": {"point": 12, "low": 9, "high": 15, "n": 3},
                   "errorRate": RATE}],
        "topology": {"undeclared": [{"from": "agent:a", "to": "tool:shell"}],
                     "declaredButUnused": []},
        "faults": {"injected": 0}, "integrity": {"level": "hash-chained"},
    },
    "sentinel-overhead": {"kind": "sentinel-overhead", "method": "benchmark",
                          "overhead": {"p50": 0.5, "p95": 0.8, "p99": 2.6, "count": 20}},
    "gap-scan": {"kind": "gap-scan", "findings": [
        {"rule": "G06", "dimension": "lifecycleCoverage", "status": "gap", "subject": "approvals",
         "detail": "No approval event."},
        {"rule": "G01", "dimension": "actionRecoverability", "status": "not-observed",
         "subject": "MCP servers", "detail": "No MCP configuration was named."}]},
    "inventory": {"kind": "inventory", "inventory": {}},
    "auditability-card": {"kind": "auditability-card", "questions": {
        "Q1": {"dimension": "actionRecoverability", "text": "What happened? More.",
               "answers": [{"file": "a.json"}], "gaps": ["x"]},
        "Q6": {"dimension": "degradation", "text": "Degraded? More.", "answers": [],
               "notAnswered": "No degraded audit was supplied."}}},
    "deployment-manifest": {"kind": "deployment-manifest", "members": []},
    "system-report": {"kind": "system-report", "measurements": []},
}


def test_every_document_kind_has_an_extractor_and_a_test():
    """S0 is done when every kind OPAAS writes reads into measurements."""
    from opaas.core.credential.vc import EVIDENCE_KINDS

    assert set(EXTRACTORS) == {"model-report", *EVIDENCE_KINDS}
    assert set(DOCUMENTS) == set(EXTRACTORS)


@pytest.mark.parametrize("kind", sorted(DOCUMENTS))
def test_each_kind_reads_into_valid_measurements(kind):
    extraction = extract(DOCUMENTS[kind], component="c", file="f.json", file_hash="sha256:f")
    assert extraction.kind == kind
    for measurement in extraction.measurements:
        check(measurement.to_record())  # every record satisfies the format
        assert measurement.source == {"kind": kind, "file": "f.json", "hash": "sha256:f"}
    if kind in ("inventory", "deployment-manifest", "system-report"):
        assert not extraction.measurements and extraction.notes


def _cells(extraction):
    return {m.aspect: m for m in extraction.measurements}


def test_a_bench_report_keeps_every_figure_and_declares_what_it_lacks():
    cells = _cells(extract(BENCH, component="llm"))
    assert cells["instruction-following (ifeval)"].value["point"] == 0.5
    assert cells["block A"].reason == "Block A was not run."
    assert cells["block C"].reason == "Block C failed: engine down"
    assert cells["toxicity of responses (classifier)"].status == "not-measured"
    assert cells["bias · Age · stereotype-aligned picks"].detail["verdict"] == (
        "no difference detected")
    assert cells["bias · SES · stereotype-aligned picks"].status == "not-measured"
    assert cells["adversarial toxicity"].reason == "No rtp."
    assert cells["probe · reasoning"].status == "not-measured"


def test_a_model_has_no_auditability_of_its_own_and_says_so():
    """Not applicable, not a gap: the certificate must tell the two apart."""
    extraction = extract(BENCH, component="llm")
    audit = [m for m in extraction.measurements if m.axis == "auditability"]
    assert audit and {m.status for m in audit} == {"not-applicable"}


def test_block_e_declarations_stay_out_of_the_measurements():
    """Invariant 9: what the operator claims is never a measured cell."""
    extraction = extract(BENCH, component="llm")
    assert [d["id"] for d in extraction.declarations] == ["licenseDocumented"]
    assert not any(m.metric == "licenseDocumented" for m in extraction.measurements)


def test_an_agent_whose_loop_opaas_ran_has_auditability_not_applicable():
    extraction = extract(SMITH, component="agent")
    audit = {m.status for m in extraction.measurements if m.axis == "auditability"}
    assert audit == {"not-applicable"}
    cells = _cells(extraction)
    assert cells["prompt injections resisted"].reason == "No injection scenario."
    assert cells["bias in the agent's actions"].status == "not-measured"


def test_an_agent_with_its_own_record_is_audited_from_it():
    http = {**SMITH, "agent": {"adapter": "http"}}
    cells = _cells(extract(http, component="agent"))
    assert cells["action completeness ratio · the agent's own record"].status == "not-measured"


def test_an_undecidable_verdict_is_a_measured_verdict_with_its_missing_fields():
    """N1: undecidable is an answer about the record, and it names what was missing."""
    cells = _cells(extract(DOCUMENTS["inquest-audit"], component="proxy"))
    policy = cells["policy no-shell"]
    assert policy.value == {"type": "verdict", "verdict": "undecidable"}
    assert policy.detail["missing"] == ["target"]


def test_a_document_that_is_not_opaas_is_refused():
    with pytest.raises(ExtractionError, match="Known"):
        extract({"kind": "something-else"}, component="c")


def test_the_real_certified_report_reads_when_present():
    """The audit this design came from, if this checkout still has it."""
    report = Path("runs/start-20261014-135442/report.json")
    if not report.is_file():
        pytest.skip("the certified run is not in this checkout")
    extraction = extract(json.loads(report.read_text(encoding="utf-8")), component="llm")
    assert len(extraction.measurements) > 50


# ------------------------------------------------------------------------ CLI


def test_audit_validate_and_extract_through_the_cli(tmp_path):
    system = tmp_path / "system.json"
    system.write_text(json.dumps(_manifest(components=[
        *_manifest()["components"],
        {"id": "mcp-agent", "kind": "agent", "adapter": "mcp", "endpoint": "http://x"},
    ])), encoding="utf-8")
    validated = runner.invoke(app, ["audit", "validate", str(system)])
    assert validated.exit_code == 0, validated.output
    assert "not implemented yet" not in validated.output, "MCP agents are measured now"

    document, out = tmp_path / "smith.json", tmp_path / "cells.json"
    document.write_text(json.dumps(SMITH), encoding="utf-8")
    extracted = runner.invoke(app, ["audit", "extract", str(document), "-c", "agent",
                                    "-o", str(out)])
    assert extracted.exit_code == 0, extracted.output
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["component"] == "agent" and written["measurements"]

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"format": 1}), encoding="utf-8")
    assert runner.invoke(app, ["audit", "validate", str(broken)]).exit_code == 2
