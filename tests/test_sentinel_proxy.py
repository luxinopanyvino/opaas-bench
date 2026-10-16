import json
import threading

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opaas.core.events import EventLog, read_log, verify_chain, verify_seals
from opaas.core.policy import load_policies
from opaas.sentinel.gate import ApprovalQueue, GateError
from opaas.sentinel.overhead import benchmark_overhead
from opaas.sentinel.proxy import SentinelProxy, pick

CHAT = json.dumps({"model": "qwen", "temperature": 0.2,
                   "messages": [{"role": "user", "content": "secret plans"}],
                   "tools": [{"type": "function", "function": {"name": "search"}}]}).encode()


def _upstream(status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={
            "model": "qwen", "usage": {"total_tokens": 12},
            "choices": [{"finish_reason": "stop", "message": {"content": "private answer"}}]})
    return httpx.MockTransport(handler)


def _proxy(tmp_path, *, status=200, seen=None, **kwargs):
    log = EventLog(tmp_path / "events.jsonl", signing_key=kwargs.pop("key", None),
                   seal_every=kwargs.pop("seal_every", 100))
    return SentinelProxy("http://engine.test", log, transport=_upstream(status, seen), **kwargs)


def _call(proxy, body=CHAT, path="/v1/chat/completions", **headers):
    response = proxy.handle("POST", path, {"Content-Type": "application/json", **headers}, body)
    return response.status, b"".join(response.body)


def _records(tmp_path):
    entries, _ = read_log(tmp_path / "events.jsonl")
    verify_chain(entries)
    return [e["event"] for e in entries]


def test_observe_changes_nothing_in_the_traffic(tmp_path):
    seen = []
    proxy = _proxy(tmp_path, seen=seen)
    status, body = _call(proxy, **{"X-Custom": "1"})
    assert status == 200 and b"private answer" in body
    assert seen[0].content == CHAT and seen[0].headers["x-custom"] == "1"


def test_bodies_are_digests_and_only_kept_fields_are_clear(tmp_path):
    proxy = _proxy(tmp_path)
    _call(proxy, **{"X-OPAAS-Actor-Chain": "user:alice>agent:planner"})
    record = _records(tmp_path)[0]
    dumped = json.dumps(record)
    assert "secret plans" not in dumped and "private answer" not in dumped
    assert record["action"] == "model_call" and record["target"] == "qwen"
    assert record["args"]["value"]["tools.*.function.name"] == ["search"]
    assert record["output"]["value"]["usage"] == {"total_tokens": 12}
    assert record["actorChain"][0] == {"role": "user", "id": "alice"}


def test_no_actor_chain_is_invented(tmp_path):
    proxy = _proxy(tmp_path)
    _call(proxy)
    assert "actorChain" not in _records(tmp_path)[0]
    configured = _proxy(tmp_path / "c", actor_chain="service:batch>agent:nightly")
    _call(configured)
    record = _records(tmp_path / "c")[0]
    assert record["extra"]["actorChainSource"] == "proxy-configuration"


def test_a_repeat_of_a_failed_call_is_a_retry_and_a_switch_is_a_fallback(tmp_path):
    proxy = _proxy(tmp_path, status=500)
    trace = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    _call(proxy, traceparent=trace)
    _call(proxy, traceparent=trace)
    other = json.dumps({"model": "backup"}).encode()
    _call(proxy, body=other, traceparent=trace)
    phases = [(r["phase"], r["extra"]["phaseSource"]) for r in _records(tmp_path)]
    assert phases == [("attempt", "default"), ("retry", "inferred-from-sequence"),
                      ("fallback", "inferred-from-sequence")]


def test_mcp_tool_calls_are_recognised(tmp_path):
    proxy = _proxy(tmp_path)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "delete_file", "arguments": {"path": "/x"}}}).encode()
    _call(proxy, body=body, path="/mcp")
    record = _records(tmp_path)[0]
    assert record["action"] == "tool_call" and record["target"] == "delete_file"
    assert "/x" not in json.dumps(record)


def test_observe_records_violations_and_alerts_but_forwards(tmp_path):
    alerts = []
    policies = load_policies([
        {"id": "no-qwen", "kind": "forbid", "enforce": "block",
         "match": {"field": "target", "eq": "qwen"}},
        {"id": "temp-low", "kind": "require", "when": {"field": "action", "eq": "model_call"},
         "then": {"field": "args.value.messages", "exists": True}},
    ])
    proxy = _proxy(tmp_path, policies=policies, alert=alerts.append)
    status, _ = _call(proxy)
    assert status == 200
    decisions = {d["policy"]: d for d in _records(tmp_path)[0]["policyDecisions"]}
    assert decisions["no-qwen"]["verdict"] == "violate"
    assert decisions["temp-low"]["verdict"] == "violate"
    assert "decidedAt" in decisions["no-qwen"]
    assert {a["policy"] for a in alerts} == {"no-qwen", "temp-low"}


def test_gate_blocks_what_a_block_policy_forbids(tmp_path):
    seen = []
    policies = load_policies([{"id": "no-qwen", "kind": "forbid", "enforce": "block",
                               "match": {"field": "target", "eq": "qwen"}}])
    proxy = _proxy(tmp_path, seen=seen, policies=policies, mode="gate")
    status, body = _call(proxy)
    assert status == 403 and b"no-qwen" in body and seen == []
    record = _records(tmp_path)[0]
    assert record["phase"] == "block"
    assert record["policyDecisions"][0]["enforced"] == "block"


def test_gate_holds_for_approval_and_records_who_approved(tmp_path):
    queue = ApprovalQueue(tmp_path / "queue", poll_s=0.01)
    policies = load_policies([{
        "id": "approve-first", "kind": "precedes", "enforce": "approve",
        "scope": {"field": "action", "eq": "model_call"},
        "match": {"field": "target", "eq": "qwen"},
        "before": {"field": "action", "eq": "approval"}}])
    proxy = _proxy(tmp_path, policies=policies, mode="gate", queue=queue,
                   approval_timeout_s=5)

    def approver():
        for _ in range(500):
            pending = queue.pending()
            if pending:
                queue.decide(pending[0]["id"], "approve", by="carol")
                return
            threading.Event().wait(0.01)

    thread = threading.Thread(target=approver)
    thread.start()
    status, _ = _call(proxy)
    thread.join()
    assert status == 200
    phases = [(r["action"], r["phase"]) for r in _records(tmp_path)]
    assert phases == [("model_call", "pending"), ("approval", "approval"),
                      ("model_call", "attempt")]
    approval = _records(tmp_path)[1]
    assert approval["actorChain"] == [{"role": "user", "id": "carol"}]


def test_an_undecidable_gated_action_is_held_not_allowed(tmp_path):
    queue = ApprovalQueue(tmp_path / "queue", poll_s=0.01)
    policies = load_policies([{"id": "chain", "kind": "require", "enforce": "block",
                               "when": {"field": "action", "eq": "model_call"},
                               "then": {"field": "actorChain.0.id", "eq": "alice"}}])
    proxy = _proxy(tmp_path, policies=policies, mode="gate", queue=queue,
                   approval_timeout_s=0.05)
    status, body = _call(proxy)
    assert status == 403 and b"timed out" in body
    assert [r["phase"] for r in _records(tmp_path)] == ["pending", "block"]


def test_the_queue_refuses_anonymous_or_unknown_decisions(tmp_path):
    queue = ApprovalQueue(tmp_path)
    pending_id = queue.submit({"target": "x"})
    with pytest.raises(GateError):
        queue.decide(pending_id, "approve", by=" ")
    with pytest.raises(GateError):
        queue.decide("nope", "approve", by="carol")
    with pytest.raises(GateError):
        queue.decide(pending_id, "maybe", by="carol")


def test_the_proxy_log_is_sealed_with_the_operational_key(tmp_path):
    key = Ed25519PrivateKey.generate()
    proxy = _proxy(tmp_path, key=key, seal_every=2)
    for _ in range(3):
        _call(proxy)
    proxy.close()
    entries, seals = read_log(tmp_path / "events.jsonl")
    covered, problems = verify_seals(entries, seals, key.public_key())
    assert covered == 3 and problems == []


def test_an_unreachable_upstream_is_recorded_as_an_error(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused")
    log = EventLog(tmp_path / "events.jsonl")
    proxy = SentinelProxy("http://engine.test", log, transport=httpx.MockTransport(down))
    status, _ = _call(proxy)
    assert status == 502
    assert "unreachable" in _records(tmp_path)[0]["error"]


def test_pick_walks_lists():
    assert pick({"a": [{"b": 1}, {"b": 2}, {}]}, ["a.*.b", "z"]) == {"a.*.b": [1, 2]}


def test_the_overhead_benchmark_reports_percentiles():
    result = benchmark_overhead(requests=20)
    assert result["overhead"]["count"] == 20
    assert result["overhead"]["p50"] is not None and result["overhead"]["p99"] >= 0
    assert "network hop" in result["excludes"]


def test_a_configured_actor_chain_with_an_unknown_role_is_refused(tmp_path):
    """A typo here made every action of the proxy's life unattributable."""
    with pytest.raises(ValueError, match="unknown role"):
        _proxy(tmp_path, actor_chain="service:opaas>app:demo")
    with pytest.raises(ValueError, match="role:id"):
        _proxy(tmp_path / "e", actor_chain="nobody")


def test_a_header_chain_is_recorded_as_sent_even_when_incomplete(tmp_path):
    proxy = _proxy(tmp_path)
    _call(proxy, **{"X-OPAAS-Actor-Chain": "app:demo"})
    assert _records(tmp_path)[0]["actorChain"] == [{"role": "app", "id": "demo"}]


def test_a_polite_stop_exits_like_ctrl_c_so_the_log_is_sealed():
    """SIGTERM used to kill the proxy before it sealed the tail of its log."""
    import signal

    from opaas.sentinel.proxy import _TERMINATE_SIGNALS, stop_on_terminate

    before = {sig: signal.getsignal(sig) for sig in _TERMINATE_SIGNALS}
    with stop_on_terminate():
        for sig in _TERMINATE_SIGNALS:
            with pytest.raises(KeyboardInterrupt):
                signal.getsignal(sig)(sig, None)
    assert {sig: signal.getsignal(sig) for sig in _TERMINATE_SIGNALS} == before


# ----------------------------------------------------------------- streams


def _stream_proxy(tmp_path, body: bytes, **kwargs):
    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})
    log = EventLog(tmp_path / "events.jsonl")
    return SentinelProxy("http://engine.test", log, transport=httpx.MockTransport(handler),
                         **kwargs)


def _sse(*chunks) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


OPENAI_STREAM = _sse(
    {"object": "chat.completion.chunk", "model": "qwen",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "The whole "}}]},
    {"object": "chat.completion.chunk", "model": "qwen",
     "choices": [{"index": 0, "delta": {"content": "private answer"}}]},
    {"object": "chat.completion.chunk", "model": "qwen", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "c1", "type": "function",
                        "function": {"name": "search", "arguments": "{\"q\": "}}]}}]},
    {"object": "chat.completion.chunk", "model": "qwen", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": "\"x\"}"}}]},
        "finish_reason": "tool_calls"}]},
    {"object": "chat.completion.chunk", "model": "qwen", "choices": [],
     "usage": {"total_tokens": 9}},
)


def test_a_kept_streamed_answer_is_the_whole_answer_not_its_last_fragment(tmp_path):
    proxy = _stream_proxy(tmp_path, OPENAI_STREAM, keep_output=(
        "choices.*.message.content", "choices.*.message.tool_calls.*.function.arguments",
        "choices.*.message.tool_calls.*.function.name", "usage"))
    status, body = _call(proxy)
    assert status == 200 and body == OPENAI_STREAM  # relayed byte for byte
    kept = _records(tmp_path)[0]["output"]["value"]
    assert kept["choices.*.message.content"] == ["The whole private answer"]
    assert kept["choices.*.message.tool_calls.*.function.arguments"] == [['{"q": "x"}']]
    assert kept["choices.*.message.tool_calls.*.function.name"] == [["search"]]
    assert kept["usage"] == {"total_tokens": 9} and kept["streamed"] is True


def test_a_stream_keeps_only_what_is_kept_by_default(tmp_path):
    """Assembling a stream must not put its content in the record (N4)."""
    proxy = _stream_proxy(tmp_path, OPENAI_STREAM)
    _call(proxy)
    record = _records(tmp_path)[0]
    assert "private answer" not in json.dumps(record)
    assert record["output"]["value"]["choices.*.finish_reason"] == ["tool_calls"]
    assert record["output"]["value"]["choices.*.message.tool_calls.*.function.name"] == [
        ["search"]]


def test_an_ollama_stream_is_assembled_like_its_unstreamed_response(tmp_path):
    lines = [{"model": "qwen", "message": {"role": "assistant", "content": "Hel"},
              "done": False},
             {"model": "qwen", "message": {"role": "assistant", "content": "lo"}, "done": False},
             {"model": "qwen", "message": {"role": "assistant", "content": ""}, "done": True,
              "done_reason": "stop", "eval_count": 2}]
    body = "".join(json.dumps(line) + "\n" for line in lines).encode()
    proxy = _stream_proxy(tmp_path, body, keep_output=("message.content", "done_reason",
                                                       "eval_count"))
    _call(proxy, path="/api/chat")
    kept = _records(tmp_path)[0]["output"]["value"]
    assert kept["message.content"] == "Hello"
    assert kept["done_reason"] == "stop" and kept["eval_count"] == 2


def test_a_stream_of_an_unknown_shape_says_only_its_last_line_was_kept(tmp_path):
    body = _sse({"delta": "a"}, {"delta": "b", "usage": {"total_tokens": 3}})
    proxy = _stream_proxy(tmp_path, body)
    _call(proxy)
    kept = _records(tmp_path)[0]["output"]["value"]
    assert kept == {"usage": {"total_tokens": 3}, "streamed": True, "streamAssembled": False}


def test_a_stream_longer_than_the_parse_limit_says_it_was_cut(tmp_path, monkeypatch):
    from opaas.sentinel import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_PARSE_LIMIT", 200)
    proxy = _stream_proxy(tmp_path, OPENAI_STREAM, keep_output=("choices.*.message.content",))
    _call(proxy)
    record = _records(tmp_path)[0]
    assert record["output"]["value"]["streamTruncated"] is True
    # The digest still covers every byte relayed, not the prefix that was parsed.
    import hashlib

    assert record["output"]["digest"] == "sha256:" + hashlib.sha256(OPENAI_STREAM).hexdigest()
