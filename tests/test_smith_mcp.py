"""The MCP agent adapter (D60, phase S4): OPAAS as the agent's MCP client and tool server.

Everything runs in memory: the agent endpoint is an `httpx.MockTransport`, and
the fake agent reaches OPAAS's mock tools through `handle_mcp_request`, as a
real agent would through the URL it is given. No test opens a socket.
"""

import json

import httpx
import pytest

from opaas.smith.agents import McpAgent, agent_keeps_record, available_agents
from opaas.smith.oracle import MockEnvironment
from opaas.smith.runner import build_report, run_scenarios
from opaas.smith.scenario import load_scenarios


def _scenario(name="task-calendar-lookup"):
    return next(s for s in load_scenarios("reference") if s.id == name)


def _environment(scenario=None):
    return MockEnvironment(scenario or _scenario(),
                           default_chain=[{"role": "user", "id": "u"},
                                          {"role": "agent", "id": "a"}])


def _rpc(adapter, method, params=None, request_id=1, headers=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if request_id is not None:
        message["id"] = request_id
    return adapter.handle_mcp_request(json.dumps(message).encode(), headers or {})


class FakeMcpAgent:
    """An agent exposed as an MCP server. On `run_task` it lists and calls OPAAS's tools."""

    def __init__(self, adapter, *, sse=False, answer=None, status=200):
        self.adapter, self.sse, self.answer, self.status = adapter, sse, answer, status
        self.seen_methods: list[str] = []
        self.task_arguments: dict = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        message = json.loads(request.content)
        self.seen_methods.append(message["method"])
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-agent"}}
            return self._reply(message["id"], result, {"Mcp-Session-Id": "session-1"})
        assert request.headers.get("mcp-session-id") == "session-1"
        self.task_arguments = message["params"]["arguments"]
        _, listed = _rpc(self.adapter, "tools/list")
        tool = listed["result"]["tools"][0]["name"]
        _, called = _rpc(self.adapter, "tools/call",
                         {"name": tool, "arguments": {"date": "2026-03-14"}},
                         headers={"X-OPAAS-Actor-Chain": "user:u>agent:fake"})
        events = called["result"]["content"][0]["text"]
        final = self.answer if self.answer is not None else f"Seen: {events}"
        record = [{"schema": 1, "action": "tool_call", "target": tool, "id": "r1",
                   "ts": "2026-10-15T00:00:00Z", "source": "agent", "policyDecisions": []}]
        text = json.dumps({"final": final, "record": record, "turns": 2})
        return self._reply(message["id"], {"content": [{"type": "text", "text": text}]})

    def _reply(self, request_id, result, headers=None):
        body = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if self.sse:
            data = f"event: message\ndata: {json.dumps(body)}\n\n"
            return httpx.Response(200, text=data, headers={
                "content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=body, headers=headers or {})


def _adapter(**fake):
    adapter = McpAgent("http://agent.test/mcp", serve=False)
    agent = FakeMcpAgent(adapter, **fake)
    adapter.transport = httpx.MockTransport(agent)
    return adapter, agent


def test_mcp_is_a_registered_adapter_that_keeps_a_record():
    assert "mcp" in available_agents()
    assert agent_keeps_record("mcp") is True


@pytest.mark.parametrize("sse", [False, True])
def test_an_mcp_agent_is_run_and_every_tool_call_is_seen_by_the_oracle(sse):
    """The mock tools are the ground truth (N3) whatever transport reaches them."""
    adapter, agent = _adapter(sse=sse)
    environment = _environment()
    run = adapter.run(environment, max_turns=5)
    assert run.error is None and "10:30" in run.final
    assert agent.seen_methods == ["initialize", "notifications/initialized", "tools/call"]
    assert agent.task_arguments["task"].startswith("At what time")
    assert agent.task_arguments["tools"]["url"] == "memory://mcp"
    assert [c["tool"] for c in environment.calls] == ["list_events"]
    assert run.record and run.record[0]["target"] == "list_events"


def test_the_mock_tool_server_speaks_json_rpc():
    adapter = McpAgent("http://agent.test", serve=False)
    status, init = _rpc(adapter, "initialize", {"protocolVersion": "2025-03-26"})
    assert status == 200 and init["result"]["protocolVersion"] == "2025-03-26"
    assert _rpc(adapter, "notifications/initialized", request_id=None) == (202, None)
    _, idle = _rpc(adapter, "tools/list")
    assert idle["error"]["code"] == -32002, "no scenario running"
    adapter._environment = _environment()
    _, unknown = _rpc(adapter, "resources/list")
    assert unknown["error"]["code"] == -32601
    _, failed = _rpc(adapter, "tools/call", {"name": "no_such_tool", "arguments": {}})
    assert failed["result"]["isError"] is True
    status, broken = adapter.handle_mcp_request(b"{not json", {})
    assert status == 400 and broken["error"]["code"] == -32700


def test_an_unreachable_or_failing_agent_is_an_error_not_an_answer():
    adapter, _ = _adapter(status=500)
    run = adapter.run(_environment(), max_turns=5)
    assert run.final is None and "HTTP 500" in run.error


def test_smith_scores_an_mcp_agent_like_any_other():
    adapter, _ = _adapter()
    scenarios = [_scenario()]
    outcomes = run_scenarios(scenarios, adapter, repeats=1)
    report = build_report(scenarios, adapter, outcomes, repeats=1)
    assert report["agent"]["adapter"] == "mcp"
    assert report["behaviour"]["taskCompleted"]["point"] == 1
    assert "mcp-agent-adapter" not in report["notImplemented"]
    assert not any("MCP client adapter" in item for item in report["limitations"])
