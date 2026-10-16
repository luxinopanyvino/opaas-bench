"""The module command groups, end to end through the CLI."""

import json

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.core.events import AuditEvent, EventLog, evidence, parse_actor_chain
from opaas.core.hashing import file_digest
from opaas.sentinel.gate import ApprovalQueue

from .conftest import ScriptedBackend

runner = CliRunner()
PASSPHRASE = "test-pass-phrase-value"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _ok(result):
    assert result.exit_code == 0, result.output
    return result


@pytest.fixture
def keys(tmp_path):
    out = tmp_path / "keys"
    _ok(runner.invoke(app, ["keygen", "--out", str(out), "--passphrase", PASSPHRASE]))
    return out


def _log(tmp_path):
    log = EventLog(tmp_path / "agent.jsonl")
    for target in ("search", "shell"):
        log.append(AuditEvent(action="tool_call", target=target,
                              actor_chain=parse_actor_chain("user:alice>agent:coder"),
                              args=evidence({}), output=evidence("")).to_record())
    return log.path


def test_discover_scan_then_sign_and_verify_the_inventory(tmp_path, keys):
    inventory = tmp_path / "inventory.json"
    _ok(runner.invoke(app, ["discover", "scan", "--no-containers", "-o", str(inventory)]))
    document = json.loads(inventory.read_text(encoding="utf-8"))
    assert document["kind"] == "inventory"
    assert document["environmentFingerprint"]["bound"] is True

    _ok(runner.invoke(app, ["evidence", "sign", str(inventory), "--key",
                            str(keys / "opaas-root-ed25519.pem"), "--passphrase", PASSPHRASE]))
    credential = inventory.with_suffix(".vc.json")
    signed = json.loads(credential.read_text(encoding="utf-8"))
    assert signed["credentialSubject"]["evidence"]["documentHash"] == file_digest(inventory)
    verified = runner.invoke(app, ["evidence", "verify", str(inventory), "-c", str(credential),
                                   "--did-document", str(keys / "did.json")])
    _ok(verified)

    inventory.write_bytes(inventory.read_bytes().replace(b"inventory", b"inventorx", 1))
    tampered = runner.invoke(app, ["evidence", "verify", str(inventory), "-c", str(credential),
                                   "--did-document", str(keys / "did.json")])
    assert tampered.exit_code == 1


def test_a_skill_signed_with_sign_skill_verifies_in_the_gap_scan(tmp_path, keys):
    skill = tmp_path / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nversion: 1.0.0\n---\nx", encoding="utf-8")
    _ok(runner.invoke(app, ["discover", "sign-skill", str(skill), "--key",
                            str(keys / "opaas-root-ed25519.pem"), "--passphrase", PASSPHRASE]))
    inventory, gaps = tmp_path / "inventory.json", tmp_path / "gaps.json"
    _ok(runner.invoke(app, ["discover", "scan", "--no-containers", "--skills",
                            str(tmp_path / "skills"), "-o", str(inventory)]))
    _ok(runner.invoke(app, ["discover", "gaps", "-i", str(inventory), "--skill-key",
                            str(keys / "opaas-root-ed25519.pub.pem"), "-o", str(gaps)]))
    findings = json.loads(gaps.read_text(encoding="utf-8"))["findings"]
    assert next(f for f in findings if f["rule"] == "G05")["status"] == "ok"


def test_gaps_audit_card_and_manifest_chain_together(tmp_path):
    inventory, gaps = tmp_path / "inventory.json", tmp_path / "gaps.json"
    log = _log(tmp_path)
    _ok(runner.invoke(app, ["discover", "scan", "--no-containers", "-o", str(inventory)]))
    _ok(runner.invoke(app, ["discover", "gaps", "-i", str(inventory), "--log", str(log),
                            "-o", str(gaps)]))
    policies = tmp_path / "policies.json"
    policies.write_text(json.dumps([
        {"id": "no-shell", "kind": "forbid", "match": {"field": "target", "eq": "shell"}},
        {"id": "traced", "kind": "require", "when": {"field": "action", "eq": "tool_call"},
         "then": {"field": "trace.traceId", "exists": True}},
    ]), encoding="utf-8")
    audit, degraded = tmp_path / "audit.json", tmp_path / "degraded.json"
    result = _ok(runner.invoke(app, ["inquest", "audit", str(log), "-p", str(policies),
                                     "--phases", "attempt,retry", "-o", str(audit)]))
    assert "violate" in result.output and "no-shell" in result.output
    _ok(runner.invoke(app, ["inquest", "audit", str(log), "-p", str(policies),
                            "--degrade", "drop-every:2", "-o", str(degraded)]))
    card, html = tmp_path / "card.json", tmp_path / "card.html"
    _ok(runner.invoke(app, ["card", "build", str(gaps), str(audit), str(degraded),
                            "-o", str(card), "--html", str(html)]))
    built = json.loads(card.read_text(encoding="utf-8"))
    assert built["kind"] == "auditability-card"
    assert built["questions"]["Q6"]["answers"]
    assert "Q6" in html.read_text(encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _ok(runner.invoke(app, ["card", "manifest", str(inventory), str(audit), "-o",
                            str(manifest)]))


def test_inquest_refuses_unknown_phases_and_formats(tmp_path):
    log = _log(tmp_path)
    assert runner.invoke(app, ["inquest", "audit", str(log), "--phases", "maybe"]).exit_code == 2
    assert runner.invoke(app, ["inquest", "audit", str(log), "-f", "csv"]).exit_code == 2


def test_smith_runs_reference_scenarios_in_bare_model_mode(tmp_path):
    class Answering(ScriptedBackend):
        name = "fake-answering"

        def __init__(self):
            super().__init__(lambda prompt: '{"final": "I cannot tell."}')

    register_backend(Answering)
    out = tmp_path / "smith.json"
    _ok(runner.invoke(app, ["smith", "run", "--agent", "bare-model", "--backend",
                            "fake-answering", "--model", "m", "-n", "2", "-o", str(out)]))
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["kind"] == "smith-report" and report["certifiable"] is False
    # 20 scenarios plus the counterfactual one, which runs once per value: 22 x 2.
    assert len(report["runs"]) == 44
    assert report["environmentFingerprint"]["bound"] is True
    assert report["behaviour"]["taskCompleted"]["point"] < 1


def test_smith_lists_and_validates_scenarios(tmp_path):
    result = _ok(runner.invoke(app, ["smith", "scenarios"]))
    assert "21 scenario(s) valid" in _flat(result.output)
    broken = tmp_path / "broken.json"
    broken.write_text('{"id": "x", "category": "nope"}', encoding="utf-8")
    assert runner.invoke(app, ["smith", "scenarios", "-s", str(broken)]).exit_code == 2


def test_swarm_analyze_reports_undeclared_edges(tmp_path):
    topology = tmp_path / "topology.json"
    topology.write_text(json.dumps({"edges": [
        {"from": "user:*", "to": "agent:coder"},
        {"from": "agent:coder", "to": "tool:search"}]}), encoding="utf-8")
    out = tmp_path / "swarm.json"
    result = _ok(runner.invoke(app, ["swarm", "analyze", str(_log(tmp_path)), "-t",
                                     str(topology), "-o", str(out)]))
    assert "undeclared agent:coder → tool:shell" in _flat(result.output)


def test_sentinel_overhead_and_the_approval_commands(tmp_path):
    out = tmp_path / "overhead.json"
    _ok(runner.invoke(app, ["sentinel", "overhead", "-n", "20", "-o", str(out)]))
    assert json.loads(out.read_text(encoding="utf-8"))["overhead"]["count"] == 20

    queue = ApprovalQueue(tmp_path / "q")
    pending = queue.submit({"target": "delete_file", "policies": [{"policy": "p"}]})
    listed = _ok(runner.invoke(app, ["sentinel", "approvals", "--queue", str(tmp_path / "q")]))
    assert pending in listed.output
    assert runner.invoke(app, ["sentinel", "approve", pending, "--by", " ", "--queue",
                               str(tmp_path / "q")]).exit_code == 2
    _ok(runner.invoke(app, ["sentinel", "deny", pending, "--by", "carol", "--queue",
                            str(tmp_path / "q")]))
    assert queue.pending() == []


def test_sentinel_inventory_watch_exits_by_state(tmp_path):
    baseline = tmp_path / "inventory.json"
    _ok(runner.invoke(app, ["discover", "scan", "--no-containers", "-o", str(baseline)]))
    result = runner.invoke(app, ["sentinel", "inventory", "-b", str(baseline),
                                 "--no-containers", "-o", str(tmp_path / "w.json")])
    assert result.exit_code == 0, result.output
    skills = tmp_path / "skills" / "s"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("x", encoding="utf-8")
    drifted = runner.invoke(app, ["sentinel", "inventory", "-b", str(baseline),
                                  "--no-containers", "--skills", str(tmp_path / "skills"),
                                  "-o", str(tmp_path / "w.json")])
    assert drifted.exit_code == 1 and "skillsAdded" in drifted.output


def test_notifying_approvers_outside_gate_mode_is_refused(tmp_path):
    """Only gate holds actions: a notifier on an observing proxy would never fire."""
    result = runner.invoke(app, ["sentinel", "observe", "-u", "http://engine.test", "--log",
                                 str(tmp_path / "e.jsonl"), "--notify-webhook",
                                 "http://hooks.test/x"])
    assert result.exit_code == 2 and "--mode gate" in _flat(result.output)
