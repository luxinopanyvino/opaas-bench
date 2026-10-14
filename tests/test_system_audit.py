"""The system audit, phase S1: the system report and `opaas audit run` (D60)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.system import extract, load_manifest
from opaas.system.orchestrate import AuditPlanError, run_audit
from opaas.system.report import (
    Member,
    StepOutcome,
    SystemReportError,
    build_report,
    load_thresholds,
)

from .conftest import FakeBackend, ScriptedBackend
from .test_system import BENCH, DOCUMENTS, SMITH

runner = CliRunner()
RATE = {"point": 0.5, "low": 0.3, "high": 0.7, "n": 20}
ADAPTERS = ("bare-model", "http")


def _manifest(components, flows=(), **extra):
    return load_manifest({"format": 1, "id": "assistant", "purpose": "Drafts summaries.",
                          "components": list(components), "flows": list(flows), **extra},
                         known_adapters=ADAPTERS)


MODEL = {"id": "llm", "kind": "model", "backend": "ollama", "model": "qwen2.5:7b-instruct"}
AGENT = {"id": "agent", "kind": "agent", "adapter": "bare-model"}


def _write(tmp_path, name, document):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _report(tmp_path, manifest, members, steps=(), **kwargs):
    return build_report(
        manifest, manifest_hash="sha256:m",
        members=[Member(component, step, _write(tmp_path, f"{component}-{step}.json", doc))
                 for component, step, doc in members],
        steps=list(steps), **kwargs)


def _cells(report, component):
    return [r for r in report["measurements"] if r["component"] == component]


# ------------------------------------------------------------- done-when (§11)


def test_a_one_model_system_reproduces_the_bench_report_cell_for_cell(tmp_path):
    """S1 is done when a one-model system says exactly what the bench report says."""
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)])
    direct = [m.to_record() for m in extract(BENCH, component="llm").measurements]
    from_report = [r for r in _cells(report, "llm") if r["source"]["kind"] == "model-report"]
    strip = ("source",)
    assert [{k: v for k, v in r.items() if k not in strip} for r in from_report] == [
        {k: v for k, v in r.items() if k not in strip} for r in direct]


def test_a_bare_model_agent_has_its_auditability_not_applicable(tmp_path):
    report = _report(tmp_path, _manifest([MODEL, AGENT], [{"from": "agent", "to": "llm"}]),
                     [("llm", "bench", BENCH), ("agent", "smith", SMITH)])
    coverage = report["coverage"]["agent"]
    assert {coverage[d] for d in ("actionRecoverability", "evidenceIntegrity")} == {
        "not-applicable"}
    assert coverage["capability"] == "measured"
    assert report["card"]["Q1"]["notApplicable"] == ["agent", "llm"]


# ------------------------------------------------------------------ the table


def test_every_row_and_dimension_has_a_cell_with_its_reason(tmp_path):
    manifest = _manifest([MODEL, {"id": "search", "kind": "tool"},
                          {"id": "cloud", "kind": "external", "provider": "a cloud API"}])
    report = _report(tmp_path, manifest, [],
                     steps=[StepOutcome("bench", "llm", "skipped", "skipped by the operator")])
    llm = {r["dimension"]: r for r in _cells(report, "llm")}
    assert llm["capability"]["reason"] == "bench skipped: skipped by the operator"
    assert {r["status"] for r in _cells(report, "search")} == {"not-applicable"}
    cloud = _cells(report, "cloud")
    assert {r["status"] for r in cloud} == {"not-measured"}
    assert "D22" in cloud[0]["reason"]
    assert len(report["coverage"]["llm"]) == 11  # 5 behaviour + 6 auditability


def test_nothing_is_pooled_and_no_score_appears(tmp_path):
    """Invariant 1: the report has no field a total could live in."""
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)])
    text = json.dumps(report).lower()
    for word in ('"score"', '"grade"', '"tier"', '"overall"', '"total"'):
        assert word not in text


def test_a_member_for_an_unknown_component_is_refused(tmp_path):
    with pytest.raises(SystemReportError, match="not a component"):
        _report(tmp_path, _manifest([MODEL]), [("ghost", "bench", BENCH)])


# ---------------------------------------------------------------- thresholds


def test_a_component_threshold_wins_over_a_kind_threshold(tmp_path):
    thresholds = load_thresholds({
        "byKind": {"model": {"accuracy": {"min": 0.9}}},
        "byComponent": {"llm": {"accuracy": {"min": 0.2}}},
    })
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)],
                     thresholds=thresholds)
    accuracy = next(r for r in report["measurements"] if r["metric"] == "accuracy")
    assert accuracy["threshold"]["scope"] == "component"
    assert accuracy["verdict"] == {"verdict": "comply"}


@pytest.mark.parametrize("rule, verdict", [
    ({"min": 0.2}, "comply"),
    ({"min": 0.9}, "violate"),
    ({"min": 0.5}, "undecidable"),  # the interval [0.3, 0.7] straddles it: N1
])
def test_a_threshold_is_judged_against_the_interval(tmp_path, rule, verdict):
    thresholds = load_thresholds({"byKind": {"model": {"accuracy": rule}}})
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)],
                     thresholds=thresholds)
    accuracy = next(r for r in report["measurements"] if r["metric"] == "accuracy")
    assert accuracy["verdict"]["verdict"] == verdict


def test_a_threshold_on_an_unmeasured_cell_is_undecidable(tmp_path):
    thresholds = load_thresholds({"byComponent": {"llm": {"aboveThresholdRate": {"max": 0.1}}}})
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)],
                     thresholds=thresholds)
    toxicity = next(r for r in report["measurements"] if r["metric"] == "aboveThresholdRate")
    assert toxicity["verdict"]["verdict"] == "undecidable"


def test_malformed_thresholds_are_refused():
    with pytest.raises(SystemReportError, match="min, max or in"):
        load_thresholds({"byKind": {"model": {"accuracy": {"atLeast": 1}}}})
    with pytest.raises(SystemReportError, match="only"):
        load_thresholds({"global": {}})


# --------------------------------------------------------------- composition


def test_declared_and_observed_are_compared_per_component(tmp_path):
    inventory = {"kind": "inventory", "inventory": {"engines": [{
        "kind": "ollama", "endpoint": "127.0.0.1:11434",
        "models": [{"name": "qwen2.5:7b-instruct"}, {"name": "llama3:8b"}]}]}}
    missing = {"id": "other", "kind": "model", "backend": "ollama", "model": "mistral:7b"}
    report = _report(tmp_path, _manifest([MODEL, missing]),
                     [("system", "discover", inventory)])
    components = {c["id"]: c for c in report["composition"]["components"]}
    assert components["llm"]["observed"] is True
    assert components["other"]["observed"] is False
    findings = " ".join(report["composition"]["findings"])
    assert "llama3:8b" in findings and "other is declared and was not found" in findings


def test_a_declared_flow_never_used_is_a_finding(tmp_path):
    manifest = _manifest([MODEL, AGENT], [{"from": "agent", "to": "llm"}])
    swarm = {**DOCUMENTS["swarm-analysis"], "topology": {
        "undeclared": [], "declaredButUnused": [
            {"from": "agent:agent", "to": "model:qwen2.5:7b-instruct"}]}}
    report = _report(tmp_path, manifest, [("system", "swarm", swarm)])
    assert report["composition"]["flows"][0]["observed"] is False
    assert any("never used" in f for f in report["composition"]["findings"])


def test_members_from_two_environments_are_flagged(tmp_path):
    one = {**SMITH, "environmentFingerprint": {"bound": True, "inventoryHash": "sha256:a"}}
    two = {**SMITH, "environmentFingerprint": {"bound": True, "inventoryHash": "sha256:b"}}
    report = _report(tmp_path, _manifest([AGENT]),
                     [("agent", "smith", one), ("agent", "smith-2", two)])
    assert report["environments"]["consistent"] is False


# ----------------------------------------------------------------- block E


def test_block_e_moves_to_the_system_with_declared_and_probed_apart(tmp_path):
    declared = [{"id": "riskClassification", "status": "partial"}]
    report = _report(tmp_path, _manifest([MODEL]), [("llm", "bench", BENCH)],
                     declared_items=declared)
    compliance = report["compliance"]
    assert compliance["purpose"] == "Drafts summaries."
    assert compliance["declared"] == declared
    assert [p["aspect"] for p in compliance["probed"]] == ["probe · egress"]


# ------------------------------------------------------------- orchestration


class _Recorder:
    """Stands in for the CLI: records each command and writes a canned document."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.fail:
            return 1
        out = Path(args[args.index("--out") + 1] if "--out" in args
                   else args[args.index("--output") + 1])
        kind = {"discover": "inventory" if args[1] == "scan" else "gap-scan",
                "run": "model-report", "smith": "smith-report",
                "sentinel": "sentinel-overhead", "inquest": "inquest-audit",
                "swarm": "swarm-analysis"}[args[0]]
        document = {**DOCUMENTS[kind]}
        if kind == "inventory":
            document["inventory"] = {"engines": [], "hash": "sha256:inv"}
        out.write_text(json.dumps(document), encoding="utf-8")
        return 0


def _system(tmp_path, **extra):
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    return _manifest(
        [MODEL, AGENT, {"id": "gateway", "kind": "proxy"},
         {"id": "mcp-agent", "kind": "agent", "adapter": "mcp", "endpoint": "http://x"}],
        [{"from": "agent", "to": "llm", "via": "gateway"}],
        records=[{"source": "sentinel", "path": "events.jsonl", "component": "gateway"}],
        **extra)


def test_the_plan_runs_only_what_the_manifest_calls_for_in_order(tmp_path):
    invoke = _Recorder()
    run = run_audit(_system(tmp_path), base=tmp_path, out=tmp_path / "out", invoke=invoke)
    commands = [" ".join(call[:2]) for call in invoke.calls]
    assert commands == ["discover scan", "discover gaps", "run --model", "smith run",
                        "sentinel overhead", "inquest audit", "inquest audit", "swarm analyze"]
    smith = invoke.calls[3]
    assert smith[smith.index("--model") + 1] == "qwen2.5:7b-instruct", "taken from the flow"
    assert "--inventory" in smith, "bound to the inventory (N5)"
    assert "--degrade" in invoke.calls[6]
    pending = [s for s in run.steps if s.status == "pending"]
    assert [s.component for s in pending] == ["mcp-agent"]
    topology = json.loads((tmp_path / "out" / "topology.json").read_text(encoding="utf-8"))
    assert {"from": "agent:agent", "to": "model:qwen2.5:7b-instruct"} in topology["edges"]


def test_a_supplied_member_is_cited_and_its_step_not_run_again(tmp_path):
    """A bench run takes hours: the certified one is the one to cite."""
    certified = _write(tmp_path, "certified.json", BENCH)
    invoke = _Recorder()
    run = run_audit(_system(tmp_path), base=tmp_path, out=tmp_path / "out", invoke=invoke,
                    supplied=[("llm", certified, "model-report")])
    assert not any(call[0] == "run" for call in invoke.calls)
    assert any(m.path == certified and m.component == "llm" for m in run.members)


def test_skipped_and_failed_steps_are_recorded_with_their_reason(tmp_path):
    run = run_audit(_system(tmp_path), base=tmp_path, out=tmp_path / "out",
                    invoke=_Recorder(fail={"smith"}), skip=["bench"])
    outcomes = {(s.step, s.component): s for s in run.steps}
    assert outcomes[("bench", "llm")].status == "skipped"
    assert outcomes[("smith", "agent")].status in ("failed", "skipped")
    assert any(s.status == "failed" and s.step == "smith" for s in run.steps)
    with pytest.raises(AuditPlanError, match="Unknown step"):
        run_audit(_system(tmp_path), base=tmp_path, out=tmp_path / "o2", invoke=_Recorder(),
                  only=["everything"])


# ------------------------------------------------------------------ the CLI


def test_audit_run_end_to_end_with_fake_engines(tmp_path):
    """The real commands, in process, against fake backends: no network, no GPU."""
    register_backend(FakeBackend)

    class Answering(ScriptedBackend):
        name = "fake-answering"

        def __init__(self):
            super().__init__(lambda prompt: '{"final": "I cannot tell."}')

    register_backend(Answering)
    system = _write(tmp_path, "system.json", {
        "format": 1, "id": "demo", "purpose": "Answers questions from documents.",
        "components": [
            {"id": "llm", "kind": "model", "backend": "fake", "model": "fake/model-1b"},
            {"id": "agent", "kind": "agent", "adapter": "bare-model",
             "backend": "fake-answering", "model": "m"},
        ],
        "flows": [{"from": "agent", "to": "llm"}],
    })
    bench = ("--suite capability --suite safety --dataset opaas-smoke --max-tokens 8")
    out = tmp_path / "audit"
    result = runner.invoke(app, ["audit", "run", str(system), "-o", str(out), "--skip",
                                 "discover", "--bench-args", bench, "-n", "1"])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "system-report.json").read_text(encoding="utf-8"))
    assert report["kind"] == "system-report"
    assert report["coverage"]["llm"]["capability"] == "measured"
    assert report["coverage"]["llm"]["performance"] == "not-measured"
    assert report["coverage"]["agent"]["actionRecoverability"] == "not-applicable"
    assert {m["kind"] for m in report["members"]} == {"model-report", "smith-report"}
    assert "coverage" in result.output
