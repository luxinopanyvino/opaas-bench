import json

import httpx
import pytest

from opaas.core.events import EventLog, read_log
from opaas.sentinel.proxy import SentinelProxy
from opaas.swarm import (
    FaultPlan,
    FaultPlanError,
    TopologyError,
    analyse,
    analyse_faults,
    attribute,
    compare_topology,
    load_topology,
)

TOPOLOGY = load_topology({"edges": [
    {"from": "user:*", "to": "agent:planner"},
    {"from": "agent:planner", "to": "agent:coder"},
    {"from": "agent:coder", "to": "model:qwen"},
    {"from": "agent:coder", "to": "tool:search"},
]})


def _rec(i, chain, action="model_call", target="qwen", trace="t1", **extra):
    hops = [{"role": r, "id": n} for r, n in (h.split(":") for h in chain.split(">"))]
    return {"id": str(i), "action": action, "target": target, "phase": "attempt",
            "actorChain": hops, "trace": {"traceId": trace}, **extra}


def test_undeclared_and_unused_edges_are_both_findings():
    records = [
        _rec(1, "user:alice>agent:planner>agent:coder"),
        _rec(2, "user:alice>agent:planner>agent:coder", action="tool_call", target="shell"),
        {"id": "3", "action": "model_call", "target": "qwen"},
    ]
    result = compare_topology(TOPOLOGY, records)
    assert result["undeclared"] == [{"from": "agent:coder", "to": "tool:shell", "calls": 1}]
    assert result["declaredButUnused"] == [{"from": "agent:coder", "to": "tool:search"}]
    assert result["unattributedCalls"] == {"model_call qwen": 1}


def test_edges_carry_latency_error_rate_and_tokens():
    records = [_rec(i, "user:a>agent:coder", durationMs=100 + i,
                    output={"value": {"usage": {"total_tokens": 10}}},
                    **({"error": "x"} if i == 0 else {})) for i in range(8)]
    edge = analyse(records, TOPOLOGY)["edges"][0]
    assert edge["from"] == "agent:coder" and edge["to"] == "model:qwen"
    assert edge["calls"] == 8 and edge["tokens"]["total"] == 80
    assert edge["errorRate"]["point"] == 0.125
    assert edge["latencyMs"]["point"] == 103.5


def test_attribution_recovers_the_trace_subgraph():
    records = [_rec(1, "user:alice>agent:planner", action="delegation", target="coder"),
               _rec(2, "user:alice>agent:planner>agent:coder"),
               _rec(3, "user:bob>agent:planner", trace="other")]
    result = attribute(records, "2")
    assert result["records"] == ["1", "2"]
    assert "agent:coder" in result["nodes"] and "user:bob" not in result["nodes"]
    with pytest.raises(TopologyError):
        attribute(records, "404")


def test_fault_plans_are_deterministic_counters():
    plan = FaultPlan({"faults": [{"kind": "error", "target": "qwen", "every": 2, "limit": 2}]})
    picks = [plan.pick("model_call", "qwen") is not None for _ in range(8)]
    assert picks == [False, True, False, True, False, False, False, False]
    assert plan.pick("model_call", "other") is None
    with pytest.raises(FaultPlanError):
        FaultPlan({"faults": [{"kind": "chaos"}]})


def test_injected_faults_are_recorded_and_their_aftermath_classified(tmp_path):
    reply = json.dumps({"choices": [{"finish_reason": "stop"}]}).encode()
    log = EventLog(tmp_path / "events.jsonl")
    proxy = SentinelProxy(
        "http://engine.test", log,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=reply)),
        faults=FaultPlan({"faults": [{"kind": "error", "target": "qwen", "every": 3,
                                      "limit": 1}]}),
    )
    body = json.dumps({"model": "qwen"}).encode()
    headers = {"X-OPAAS-Actor-Chain": "user:alice>agent:coder",
               "traceparent": "00-" + "c" * 32 + "-" + "d" * 16 + "-01"}
    statuses = []
    for _ in range(4):
        response = proxy.handle("POST", "/v1/chat/completions", headers, body)
        b"".join(response.body)
        statuses.append(response.status)
    assert statuses == [200, 200, 500, 200]
    records = [e["event"] for e in read_log(log.path)[0]]
    assert records[2]["extra"]["injectedFault"]["kind"] == "error"
    assert records[3]["phase"] == "retry"
    faults = analyse_faults(records)
    assert faults["outcomes"] == {"contained": 1}
    assert faults["cases"][0]["recoveryVisibleInLifecycle"] is True


def test_a_recovery_logged_as_a_plain_attempt_is_not_visible():
    records = [_rec(1, "user:a>agent:coder", extra={"injectedFault": {"kind": "error"}},
                    error="injected"),
               _rec(2, "user:a>agent:coder")]
    faults = analyse_faults(records)
    assert faults["recoveredButNotVisible"] == ["1"]


def test_a_failure_spreading_to_other_targets_is_a_cascade():
    records = [_rec(1, "user:a>agent:coder", extra={"injectedFault": {"kind": "slow"}}),
               _rec(2, "user:a>agent:coder", action="tool_call", target="search",
                    error="timeout")]
    assert analyse_faults(records)["outcomes"] == {"cascaded": 1}


def test_topologies_are_validated():
    with pytest.raises(TopologyError):
        load_topology({"edges": [{"from": "planner", "to": "model:x"}]})
