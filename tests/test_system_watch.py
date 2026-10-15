"""The system audit, phase S3: drafting the manifest and watching every component (D60)."""

import json

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.core.evidence import envelope, write_document
from opaas.system import load_manifest
from opaas.system.draft import PURPOSE_PLACEHOLDER, draft_manifest
from opaas.system.report import Member, build_report
from opaas.system.watch import watch_system

from .conftest import FakeBackend
from .test_cli import _sign_flow

runner = CliRunner()
PASSPHRASE = "test-pass-phrase-value"

INVENTORY = {
    "engines": [
        {"kind": "ollama", "endpoint": "127.0.0.1:11434",
         "models": [{"name": "qwen2.5:7b-instruct"}, {"name": "llama3:8b"}]},
        {"kind": "vllm", "endpoint": "10.0.0.5:8000", "models": [{"name": "qwen2.5:7b-instruct"}]},
        {"kind": "mystery", "endpoint": "127.0.0.1:9999", "models": [{"name": "x"}]},
    ],
    "agentStack": {
        "mcpConfigs": [{"servers": [{"name": "docs"}]}],
        "toolManifests": [{"tools": [{"name": "search"}]}],
        "agentRuntimes": [{"name": "langgraph"}],
    },
    "unidentified": [{"port": 5000, "process": "llama-server"}],
    "hash": "sha256:inv", "enginesHash": "sha256:eng",
}


@pytest.fixture(autouse=True)
def _register_fake():
    register_backend(FakeBackend)


# --------------------------------------------------------------------- draft


def test_a_draft_declares_what_the_host_serves_and_nothing_else():
    manifest, notes = draft_manifest(INVENTORY, system_id="Support Desk")
    kinds = [(c["kind"], c.get("model") or c.get("name")) for c in manifest["components"]]
    assert kinds == [("model", "qwen2.5:7b-instruct"), ("model", "llama3:8b"),
                     ("model", "qwen2.5:7b-instruct"), ("mcp-server", "docs"),
                     ("tool", "search")]
    ids = [c["id"] for c in manifest["components"]]
    assert len(ids) == len(set(ids)), "the same model on two engines gets two ids"
    assert manifest["id"] == "support-desk" and manifest["purpose"] == PURPOSE_PLACEHOLDER
    joined = " ".join(notes)
    assert "mystery" in joined and "langgraph" in joined and "Port 5000" in joined
    load_manifest(manifest, known_adapters=("bare-model", "http"))


def test_a_drafted_manifest_round_trips_through_the_audit_unchanged(tmp_path):
    """S3 is done when auditing the draft on the same host finds nothing to add or remove."""
    reachable = {**INVENTORY, "engines": INVENTORY["engines"][:2]}
    manifest, _ = draft_manifest(reachable, purpose="Answers support tickets.")
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"kind": "inventory", "inventory": reachable}), encoding="utf-8")
    report = build_report(load_manifest(manifest), manifest_hash="sha256:m",
                          members=[Member("system", "discover", path)], steps=[])
    assert report["composition"]["findings"] == []
    assert all(c["observed"] is True for c in report["composition"]["components"])

    # An engine no adapter reaches cannot be drafted, and the audit still says it is there.
    manifest, _ = draft_manifest(INVENTORY, purpose="Answers support tickets.")
    path.write_text(json.dumps({"kind": "inventory", "inventory": INVENTORY}), encoding="utf-8")
    report = build_report(load_manifest(manifest), manifest_hash="sha256:m",
                          members=[Member("system", "discover", path)], steps=[])
    assert report["composition"]["findings"] == [
        "Observed and not declared: mystery at 127.0.0.1:9999 serves x."]


def test_the_drafted_purpose_marker_keeps_the_system_uncertifiable(tmp_path):
    manifest, _ = draft_manifest(INVENTORY)
    report = build_report(load_manifest(manifest), manifest_hash="sha256:m", members=[],
                          steps=[])
    assert "drafted marker" in " ".join(report["certifiability"]["reasons"])


def test_audit_draft_writes_a_valid_manifest_and_never_overwrites(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"kind": "inventory", "inventory": INVENTORY}),
                         encoding="utf-8")
    out = tmp_path / "system.json"
    result = runner.invoke(app, ["audit", "draft", "-i", str(inventory), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["components"]
    again = runner.invoke(app, ["audit", "draft", "-i", str(inventory), "-o", str(out)])
    assert again.exit_code == 2 and "never overwrites" in " ".join(again.output.split())


# --------------------------------------------------------------------- watch


def _report(components, flows=()):
    return {"composition": {"components": components, "flows": list(flows)}}


MODEL = {"id": "llm", "kind": "model", "model": "qwen"}
AGENT = {"id": "agent", "kind": "agent", "adapter": "bare-model"}


def _checker(states):
    return lambda component, report: {"state": states[component], "carriedBy": [],
                                      "checks": {}}


def test_every_component_is_rechecked_and_the_one_that_moved_is_named():
    report = _report([MODEL, {**MODEL, "id": "other"}, AGENT,
                      {"id": "gw", "kind": "proxy"}], [{"from": "agent", "to": "llm"}])
    body = watch_system(report, model_reports={"llm": {}, "other": {}},
                        check_model=_checker({"llm": "current", "other": "drifted"}))
    assert body["state"] == "drifted" and body["changed"] == ["other"]
    assert body["components"]["agent"]["state"] == "current"
    assert body["components"]["agent"]["follows"] == "llm"
    assert body["components"]["gw"]["state"] == "not-checked"


def test_a_model_that_cannot_be_checked_makes_the_system_inconclusive():
    body = watch_system(_report([MODEL]), model_reports={}, check_model=_checker({}))
    assert body["state"] == "inconclusive"
    assert "--member llm=" in body["components"]["llm"]["detail"]


def test_an_agent_that_is_not_bare_model_is_listed_never_assumed_current():
    http = {"id": "agent", "kind": "agent", "adapter": "http"}
    body = watch_system(_report([MODEL, http], [{"from": "agent", "to": "llm"}]),
                        model_reports={"llm": {}}, check_model=_checker({"llm": "current"}))
    assert body["components"]["agent"]["state"] == "not-checked"
    assert body["state"] == "current"


def test_a_model_served_since_the_audit_moves_the_environment():
    from opaas.sentinel.inventory import check_environment

    baseline = {"engines": [{"kind": "ollama", "port": 11434, "models": [{"name": "qwen"}]}]}
    current = {"engines": [{"kind": "ollama", "port": 11434,
                            "models": [{"name": "qwen"}, {"name": "new-model"}]}]}
    body = watch_system(_report([MODEL]), model_reports={"llm": {}},
                        check_model=_checker({"llm": "current"}),
                        baseline_inventory=baseline, current_inventory=current,
                        check_environment=check_environment)
    assert body["state"] == "drifted" and body["changed"] == ["environment"]
    unchanged = watch_system(_report([MODEL]), model_reports={"llm": {}},
                             check_model=_checker({"llm": "current"}),
                             baseline_inventory=baseline, current_inventory=baseline,
                             check_environment=check_environment)
    assert unchanged["state"] == "current"


def test_watch_on_a_system_certificate_end_to_end(tmp_path):
    """The real command: a signed system certificate, its model re-checked through the CLI."""
    bench, _, keys = _sign_flow(tmp_path)
    manifest = load_manifest({"format": 1, "id": "demo", "purpose": "Answers questions.",
                              "components": [{"id": "llm", "kind": "model", "backend": "fake",
                                              "model": "fake/model-1b"}]})
    body = build_report(manifest, manifest_hash="sha256:m",
                        members=[Member("llm", "bench", bench)], steps=[])
    report = tmp_path / "system" / "system-report.json"
    write_document(report, envelope("system-report", body, environment=None))
    credential = tmp_path / "system" / "credential.json"
    signed = runner.invoke(app, ["sign", "--input", str(report), "--key",
                                 str(keys / "opaas-root-ed25519.pem"), "--passphrase",
                                 PASSPHRASE, "--out", str(credential)])
    assert signed.exit_code == 0, signed.output

    out = tmp_path / "watch.json"
    watched = runner.invoke(app, ["watch", "--report", str(report), "--credential",
                                  str(credential), "--key",
                                  str(keys / "opaas-root-ed25519.pub.pem"),
                                  "--member", f"llm={bench}", "--out", str(out)])
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["components"]["llm"]["state"] == "current", watched.output
    assert watched.exit_code == 0

    tampered = tmp_path / "tampered.json"
    tampered.write_bytes(bench.read_bytes() + b" ")
    refused = runner.invoke(app, ["watch", "--report", str(report), "--credential",
                                  str(credential), "--key",
                                  str(keys / "opaas-root-ed25519.pub.pem"),
                                  "--member", f"llm={tampered}", "--out", str(out)])
    assert refused.exit_code == 2
    assert "not the member the system report certifies" in " ".join(refused.output.split())
