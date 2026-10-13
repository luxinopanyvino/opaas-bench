import pytest

from opaas.card import ManifestError, build_card, build_manifest, render_card
from opaas.core.events import AuditEvent, EventLog, evidence, parse_actor_chain
from opaas.core.evidence import envelope, write_document
from opaas.core.policy import load_policies
from opaas.discover import discover
from opaas.discover.gaps import scan_gaps
from opaas.inquest import audit, import_records, parse_degradation

POLICIES = load_policies([{"id": "only-alice", "kind": "require",
                           "when": {"field": "action", "eq": "tool_call"},
                           "then": {"field": "actorChain.0.id", "eq": "alice"}}])


def _evidence(tmp_path):
    log = EventLog(tmp_path / "agent.jsonl")
    log.append(AuditEvent(action="tool_call", target="search",
                          actor_chain=parse_actor_chain("user:alice>agent:a"),
                          args=evidence({}), output=evidence("")).to_record())
    inventory = discover(engines={"available": True, "engines": [], "hash": "sha256:e"},
                         listeners={}, containers=False)
    environment = {"inventory": inventory}
    paths = {}
    paths["inventory"] = tmp_path / "inventory.json"
    write_document(paths["inventory"], envelope("inventory", {"inventory": inventory},
                                                environment=environment))
    paths["gaps"] = tmp_path / "gaps.json"
    write_document(paths["gaps"], envelope("gap-scan", scan_gaps(inventory, logs=[log.path]),
                                           environment=environment))
    imported = import_records(log.path, "sentinel")
    paths["audit"] = tmp_path / "audit.json"
    write_document(paths["audit"], envelope("inquest-audit", audit(imported, policies=POLICIES),
                                            environment=environment))
    paths["degraded"] = tmp_path / "audit-degraded.json"
    write_document(paths["degraded"], envelope("inquest-audit", audit(
        imported, policies=POLICIES,
        degradations=[parse_degradation("drop-field:actorChain")]), environment=environment))
    return paths


def test_every_answer_cites_its_evidence_and_q6_is_measured(tmp_path):
    paths = _evidence(tmp_path)
    card = build_card(list(paths.values()))
    questions = card["questions"]
    assert set(questions) == {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6"}
    for entry in questions.values():
        for answer in entry["answers"]:
            assert answer["hash"].startswith("sha256:") and answer["file"]
    q6 = questions["Q6"]["answers"][0]
    assert q6["verdictsChanged"][0]["now"] == "undecidable"
    assert questions["Q5"]["answers"][0]["IS"] == "hash-chained"
    assert any(g["rule"] == "G01" for g in questions["Q1"]["gaps"]) is False  # not observed
    assert card["retention"]["declared"] is False
    assert "overall" not in str(card).lower().replace("no overall rating", "")


def test_a_question_without_evidence_says_so(tmp_path):
    paths = _evidence(tmp_path)
    card = build_card([paths["gaps"]], retention={"keptFields": ["model"], "retentionDays": 30})
    assert "notAnswered" in card["questions"]["Q6"]
    assert card["retention"]["declaredBy"] == "operator"
    html = render_card(card)
    assert "Not answered" in html and "<script" not in html


def test_the_manifest_lists_members_by_hash_and_checks_one_environment(tmp_path):
    paths = _evidence(tmp_path)
    manifest = build_manifest([paths["inventory"], paths["audit"]], name="prod")
    assert manifest["consistentEnvironment"] is True
    assert all(m["hash"].startswith("sha256:") for m in manifest["members"])

    other = tmp_path / "other.json"
    write_document(other, envelope("inquest-audit", {"x": 1},
                                   environment={"inventory": {"hash": "sha256:different"}}))
    split = build_manifest([paths["audit"], other])
    assert split["consistentEnvironment"] is False and split["findings"]


def test_a_cloud_baseline_cannot_join_a_deployment_manifest(tmp_path):
    import json

    baseline = tmp_path / "report.json"
    baseline.write_text(json.dumps({"runId": "r", "model": {"name": "gpt"},
                                    "referenceType": "cloud-baseline"}), encoding="utf-8")
    with pytest.raises(ManifestError, match="D22"):
        build_manifest([baseline])
