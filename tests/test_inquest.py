import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opaas.core.events import AuditEvent, EventLog, evidence, parse_actor_chain
from opaas.core.policy import load_policies
from opaas.inquest import audit, compare_degraded, import_records, parse_degradation
from opaas.inquest.audit import AuditError

POLICIES = load_policies([
    {"id": "no-shell", "kind": "forbid", "scope": {"field": "action", "eq": "tool_call"},
     "match": {"field": "target", "eq": "shell"}},
    {"id": "only-alice", "kind": "require", "when": {"field": "action", "eq": "tool_call"},
     "then": {"field": "actorChain.0.id", "eq": "alice"}},
])


def _sentinel_log(tmp_path, key=None, shell=False):
    log = EventLog(tmp_path / "s.jsonl", signing_key=key, seal_every=2)
    for target in ("search", "shell" if shell else "fetch", "search"):
        log.append(AuditEvent(action="tool_call", target=target,
                              actor_chain=parse_actor_chain("user:alice>agent:a"),
                              args=evidence({}), output=evidence("")).to_record())
    log.seal()
    return log.path


def test_integrity_comes_first_and_needs_the_verifiers_key(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = _sentinel_log(tmp_path, key=key)
    signed = audit(import_records(path, "sentinel", public_key=key.public_key()))
    assert signed["dimensions"]["evidenceIntegrity"]["IS"] == "signed"
    assert signed["dimensions"]["evidenceIntegrity"]["VC"]["ms"] >= 0
    unkeyed = audit(import_records(path, "sentinel"))
    assert unkeyed["dimensions"]["evidenceIntegrity"]["IS"] == "hash-chained"
    assert any("no key" in p for p in unkeyed["integrity"]["problems"])


def test_verdicts_are_three_valued_and_name_missing_fields(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(r) for r in [
        {"id": "1", "action": "tool_call", "target": "search",
         "actorChain": [{"role": "user", "id": "alice"}]},
        {"id": "2", "action": "tool_call", "target": "fetch"},
        {"id": "3", "target": "no action at all"},
    ]), encoding="utf-8")
    result = audit(import_records(trace, "jsonl"), policies=POLICIES)
    verdicts = {v["policy"]: v for v in result["verdicts"]}
    assert verdicts["no-shell"]["verdict"] == "comply"
    assert verdicts["only-alice"]["verdict"] == "undecidable"
    assert verdicts["only-alice"]["missing"] == ["actorChain.0.id"]
    assert result["source"]["excluded"][0]["line"] == 3
    spdr = result["dimensions"]["policyCheckability"]["SPDR"]
    assert spdr["missingFieldsBlocking"] == {"actorChain.0.id": 1}
    assert result["dimensions"]["evidenceIntegrity"]["IS"] == "none"
    assert "no chain" in result["limitations"][0]


def test_acr_is_never_claimed_without_ground_truth(tmp_path):
    result = audit(import_records(_sentinel_log(tmp_path), "sentinel"))
    acr = result["dimensions"]["actionRecoverability"]["ACR"]
    assert acr["measured"] is False


def test_degradation_answers_q6_with_measurements(tmp_path):
    imported = import_records(_sentinel_log(tmp_path), "sentinel")
    base = audit(imported, policies=POLICIES)
    degraded = audit(imported, policies=POLICIES,
                     degradations=[parse_degradation("drop-field:actorChain")])
    change = compare_degraded(base, degraded)
    assert change["verdictsChanged"] == [
        {"policy": "only-alice", "was": "comply", "now": "undecidable",
         "missing": ["actorChain.0.id"]}]
    assert change["RF"]["after"] < change["RF"]["before"]
    assert imported.records[0].get("actorChain")  # the original is untouched


def test_dropping_records_can_hide_a_violation(tmp_path):
    imported = import_records(_sentinel_log(tmp_path, shell=True), "sentinel")
    assert audit(imported, policies=POLICIES)["verdicts"][0]["verdict"] == "violate"
    hidden = audit(imported, policies=POLICIES,
                   degradations=[parse_degradation("drop-every:2")])
    assert hidden["verdicts"][0]["verdict"] == "comply"


@pytest.mark.parametrize("bad", ["drop-field", "drop-every:1", "shuffle"])
def test_bad_degradations_are_refused(bad):
    with pytest.raises(AuditError):
        parse_degradation(bad)


def _attr(key, value):
    kind = "intValue" if isinstance(value, int) else "stringValue"
    return {"key": key, "value": {kind: value}}


def test_otel_genai_spans_become_records_with_their_span_ancestry(tmp_path):
    trace_id = "a" * 32
    spans = [
        {"traceId": trace_id, "spanId": "1" * 16, "name": "invoke_agent planner",
         "startTimeUnixNano": "1700000000000000000", "endTimeUnixNano": "1700000001000000000",
         "attributes": [_attr("gen_ai.operation.name", "invoke_agent"),
                        _attr("gen_ai.agent.name", "planner"), _attr("enduser.id", "alice")]},
        {"traceId": trace_id, "spanId": "2" * 16, "parentSpanId": "1" * 16,
         "name": "execute_tool search", "startTimeUnixNano": "1700000000500000000",
         "attributes": [_attr("gen_ai.operation.name", "execute_tool"),
                        _attr("gen_ai.tool.name", "search"),
                        _attr("gen_ai.tool.call.arguments", '{"q": "secret"}')]},
        {"traceId": trace_id, "spanId": "3" * 16, "name": "http GET",
         "startTimeUnixNano": "1700000000600000000", "attributes": []},
        {"traceId": trace_id, "name": "broken", "startTimeUnixNano": "1",
         "attributes": [_attr("gen_ai.operation.name", "chat")]},
    ]
    path = tmp_path / "spans.json"
    path.write_text(json.dumps({"resourceSpans": [{"resource": {"attributes": [
        _attr("service.name", "agents")]}, "scopeSpans": [{"spans": spans}]}]}),
        encoding="utf-8")
    imported = import_records(path, "otel")
    assert imported.skipped == 1 and len(imported.excluded) == 1
    tool = next(r for r in imported.records if r["action"] == "tool_call")
    assert tool["actorChain"] == [{"role": "user", "id": "alice"},
                                  {"role": "agent", "id": "planner"}]
    assert "secret" not in json.dumps(tool) and tool["args"]["digest"].startswith("sha256:")
    assert tool["trace"]["parentSpanId"] == "1" * 16
    assert imported.integrity["level"] == "none"
