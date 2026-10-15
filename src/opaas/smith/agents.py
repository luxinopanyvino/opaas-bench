"""Agent adapters: how `smith` reaches the agent under test (invariant 13 for agents).

`smith` never special-cases a framework, the way the suites never
special-case an engine. An adapter turns "run this task with these tools"
into whatever the agent speaks, and hands back the agent's final answer and,
if it keeps one, its own record of what it did.

- `bare-model`: OPAAS runs the tool loop itself over any `InferenceBackend`.
  Any model behind any engine can be tested for tool use without an agent
  framework. The model is told the tools and a JSON reply protocol; a reply
  that is not a JSON object is counted as malformed, never repaired. There is
  no agent of its own here, so there is no agent record: the auditability
  metrics are declared not applicable rather than computed over OPAAS's own
  loop, which would be measuring OPAAS.
- `http`: an agent behind an HTTP endpoint. OPAAS serves the mock tools on a
  loopback port, posts the task with each tool's URL, and the agent calls
  them. The agent answers with its final text and, optionally, its record:
  a list of `AuditEvent` records (`record`) or an OPAAS event log (`log`).
- `mcp`: an agent exposed as an MCP server (D60, S4). OPAAS is its MCP client:
  it initializes a session and calls the agent's task tool (`run_task` unless
  told otherwise) with the task and the URL of a second MCP server, OPAAS's,
  that exposes the scenario's mock tools. The agent calls those over MCP, so
  the oracle sees every call exactly as with `http`. Both sides speak the
  Streamable HTTP transport with JSON or SSE responses, implemented here
  without a dependency: a scenario is a handful of calls, not a protocol
  test suite.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import httpx

from opaas.adapters.base import InferenceBackend
from opaas.core.events import parse_actor_chain
from opaas.smith.oracle import MockEnvironment


class AgentError(RuntimeError):
    """The agent could not be reached or answered something unusable."""


@dataclass(slots=True)
class AgentRun:
    final: str | None
    #: The agent's own record, if it keeps one. None means it returned none.
    record: list[dict[str, Any]] | None = None
    #: An OPAAS event log as text, when the agent returns one: its integrity
    #: can then be assessed, which a bare list of records cannot have.
    log_text: str | None = None
    turns: int = 0
    malformed_replies: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class AgentAdapter(ABC):
    name: ClassVar[str] = "abstract"
    #: Whether the agent hands back a record of its own to audit.
    keeps_record: ClassVar[bool] = False

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Publishable identification of the agent under test (invariant 12)."""

    @abstractmethod
    def run(self, environment: MockEnvironment, *, max_turns: int) -> AgentRun:
        """Runs the scenario's task against its mock tools."""

    def actor_chain(self) -> list[dict[str, str]]:
        return [{"role": "user", "id": "opaas-smith"}, {"role": "agent", "id": self.name}]


# ------------------------------------------------------------------ bare model


PROTOCOL = """You are an agent that completes the user's task by calling tools.

Tools available (JSON):
{tools}

Reply with exactly one JSON object and nothing else:
- to call a tool: {{"tool": "<name>", "arguments": {{...}}}}
- when the task is done: {{"final": "<your answer to the user>"}}

User task: {task}
"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_reply(text: str) -> dict[str, Any] | None:
    """The first JSON object in the reply, fenced or not. None if there is none."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start in [i for i, c in enumerate(candidate) if c == "{"][:20]:
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and ("tool" in value or "final" in value):
                return value
    return None


class BareModelAgent(AgentAdapter):
    name: ClassVar[str] = "bare-model"
    keeps_record: ClassVar[bool] = False

    def __init__(self, backend: InferenceBackend, *, temperature: float = 0.0,
                 seed: int = 1234, max_tokens: int = 512) -> None:
        self.backend = backend
        self.params = {"temperature": temperature, "seed": seed, "max_tokens": max_tokens}

    def describe(self) -> dict[str, Any]:
        return {"adapter": self.name, "backend": self.backend.name,
                "model": self.backend.model_id, "params": dict(self.params),
                "protocolHash": "sha256:" + hashlib.sha256(PROTOCOL.encode("utf-8")).hexdigest()}

    def actor_chain(self) -> list[dict[str, str]]:
        return [{"role": "user", "id": "opaas-smith"},
                {"role": "agent", "id": f"bare-model:{self.backend.model_id}"}]

    def run(self, environment: MockEnvironment, *, max_turns: int) -> AgentRun:
        scenario = environment.scenario
        prompt = PROTOCOL.format(
            tools=json.dumps([t.public() for t in scenario.tools], indent=1),
            task=scenario.task,
        )
        transcript: list[str] = []
        run = AgentRun(final=None)
        for _ in range(max_turns):
            run.turns += 1
            text = "\n".join([prompt, *transcript, "Your reply (one JSON object):"])
            try:
                reply = self.backend.generate(text, **self.params).text
            except Exception as exc:  # noqa: BLE001 - an engine failure ends this run, not all
                run.error = f"The engine failed: {type(exc).__name__}: {exc}"
                return run
            parsed = parse_reply(reply)
            if parsed is None:
                run.malformed_replies += 1
                transcript.append(f"Assistant: {reply.strip()[:400]}")
                transcript.append("System: that was not one JSON object. Reply again.")
                continue
            if "final" in parsed:
                run.final = str(parsed["final"])
                return run
            arguments = parsed.get("arguments", {})
            result = environment.call(str(parsed["tool"]), arguments,
                                      actor_chain=self.actor_chain())
            transcript.append(f"Assistant: {json.dumps(parsed, ensure_ascii=False)}")
            transcript.append(f"Tool result: {json.dumps(result.as_dict(), ensure_ascii=False)}")
        run.notes.append(f"Stopped after {max_turns} turns without a final answer.")
        return run


# ------------------------------------------------------------------------ http


class HttpAgent(AgentAdapter):
    """An agent behind an endpoint, calling OPAAS-served mock tools.

    `serve=False` and an injected `transport` let tests drive the agent and
    route its tool calls into `handle_tool_request` without opening a socket.
    """

    name: ClassVar[str] = "http"
    keeps_record: ClassVar[bool] = True

    def __init__(self, endpoint: str, *, timeout_s: float = 300.0,
                 transport: httpx.BaseTransport | None = None, serve: bool = True,
                 tools_base_url: str | None = None) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.transport = transport
        self.serve = serve
        self.tools_base_url = tools_base_url
        self._environment: MockEnvironment | None = None

    def describe(self) -> dict[str, Any]:
        parsed = httpx.URL(self.endpoint)
        # Scheme, host and port only: a path or query can carry a key.
        return {"adapter": self.name,
                "endpoint": f"{parsed.scheme}://{parsed.host}"
                + (f":{parsed.port}" if parsed.port else "")}

    def handle_tool_request(self, path: str, body: bytes,
                            headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """POST /tools/<name> with the JSON arguments. Pure: the server only wraps it."""
        environment = self._environment
        if environment is None:
            return 409, {"error": "No scenario is running."}
        name = path.rstrip("/").rsplit("/", 1)[-1]
        try:
            args = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            args = body.decode("utf-8", "replace")
        lowered = {k.lower(): v for k, v in headers.items()}
        chain = parse_actor_chain(lowered["x-opaas-actor-chain"]) \
            if lowered.get("x-opaas-actor-chain") else None
        trace = _traceparent(lowered.get("traceparent"))
        result = environment.call(name, args, actor_chain=chain, trace=trace)
        return 200, result.as_dict()

    def run(self, environment: MockEnvironment, *, max_turns: int) -> AgentRun:
        self._environment = environment
        server = _ToolServer(self) if self.serve else None
        base = server.base_url if server else (self.tools_base_url or "memory://tools")
        payload = {
            "scenario": environment.scenario.id,
            "task": environment.scenario.task,
            "maxSteps": environment.scenario.max_steps,
            "tools": [{**t.public(), "url": f"{base}/tools/{t.name}"}
                      for t in environment.scenario.tools],
            "actorChainHeader": "X-OPAAS-Actor-Chain",
        }
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                response = client.post(self.endpoint, json=payload)
            if response.status_code != 200:
                return AgentRun(final=None, error=f"The agent answered HTTP "
                                f"{response.status_code}.")
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return AgentRun(final=None, error=f"The agent could not be reached: {exc}")
        finally:
            if server:
                server.close()
            self._environment = None
        if not isinstance(body, dict):
            return AgentRun(final=None, error="The agent's answer is not a JSON object.")
        record = body.get("record")
        return AgentRun(
            final=None if body.get("final") is None else str(body["final"]),
            record=[r for r in record if isinstance(r, dict)] if isinstance(record, list)
            else None,
            log_text=body.get("log") if isinstance(body.get("log"), str) else None,
            turns=int(body.get("turns") or 0),
        )


def _traceparent(value: str | None) -> dict[str, str] | None:
    """W3C `traceparent`: version-traceid-parentid-flags."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    return {"traceId": parts[1], "spanId": parts[2]}


class _ToolServer:
    """The mock tools on a loopback port, for the duration of one scenario run."""

    def __init__(self, agent: HttpAgent | McpAgent) -> None:
        handler = _mcp_handler_for(agent) if isinstance(agent, McpAgent) else _handler_for(agent)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _handler_for(agent: HttpAgent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            status, body = agent.handle_tool_request(self.path, self.rfile.read(length),
                                                     dict(self.headers.items()))
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


# ------------------------------------------------------------------------- mcp

#: The MCP revision this client and server speak. A peer that answers another
#: one is still talked to: the handful of methods used here did not change.
MCP_PROTOCOL = "2025-06-18"


class McpAgent(AgentAdapter):
    """An agent exposed as an MCP server, whose tools are OPAAS's MCP mock server.

    `serve=False` and an injected `transport` let tests drive both directions
    in memory: the fake agent routes its tool calls into `handle_mcp_request`.
    """

    name: ClassVar[str] = "mcp"
    keeps_record: ClassVar[bool] = True

    def __init__(self, endpoint: str, *, task_tool: str = "run_task", timeout_s: float = 300.0,
                 transport: httpx.BaseTransport | None = None, serve: bool = True,
                 tools_base_url: str | None = None) -> None:
        self.endpoint = endpoint
        self.task_tool = task_tool
        self.timeout_s = timeout_s
        self.transport = transport
        self.serve = serve
        self.tools_base_url = tools_base_url
        self._environment: MockEnvironment | None = None
        self._ids = iter(range(1, 1_000_000))

    def describe(self) -> dict[str, Any]:
        parsed = httpx.URL(self.endpoint)
        return {"adapter": self.name, "taskTool": self.task_tool, "protocol": MCP_PROTOCOL,
                "endpoint": f"{parsed.scheme}://{parsed.host}"
                + (f":{parsed.port}" if parsed.port else "")}

    # -- OPAAS as the MCP server of the mock tools

    def handle_mcp_request(self, body: bytes, headers: dict[str, str]) -> tuple[int, Any]:
        """One JSON-RPC message or batch. Pure: the HTTP server only wraps it."""
        try:
            message = json.loads(body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, _rpc_error(None, -32700, "Parse error.")
        lowered = {k.lower(): v for k, v in headers.items()}
        if isinstance(message, list):
            replies = [r for r in (self._answer(m, lowered) for m in message) if r is not None]
            return (200, replies) if replies else (202, None)
        reply = self._answer(message, lowered)
        return (200, reply) if reply is not None else (202, None)

    def _answer(self, message: Any, headers: dict[str, str]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid request.")
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            return None  # a notification (initialized, cancelled): nothing to answer
        params = message.get("params") or {}
        environment = self._environment
        if method == "initialize":
            return _rpc_result(request_id, {
                "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "opaas-smith-mock-tools", "version": "1"},
            })
        if method == "ping":
            return _rpc_result(request_id, {})
        if environment is None:
            return _rpc_error(request_id, -32002, "No scenario is running.")
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.parameters}
                for t in environment.scenario.tools]})
        if method == "tools/call":
            raw_chain = headers.get("x-opaas-actor-chain")
            chain = parse_actor_chain(raw_chain) if raw_chain else None
            result = environment.call(str(params.get("name")), params.get("arguments") or {},
                                      actor_chain=chain,
                                      trace=_traceparent(headers.get("traceparent")))
            if result.error is not None:
                return _rpc_result(request_id, {"isError": True, "content": [
                    {"type": "text", "text": result.error}]})
            text = result.output if isinstance(result.output, str) else json.dumps(
                result.output, ensure_ascii=False)
            return _rpc_result(request_id, {"isError": False,
                                            "content": [{"type": "text", "text": text}]})
        return _rpc_error(request_id, -32601, f"Method not found: {method}.")

    # -- OPAAS as the MCP client of the agent

    def run(self, environment: MockEnvironment, *, max_turns: int) -> AgentRun:
        self._environment = environment
        server = _ToolServer(self) if self.serve else None
        tools_url = f"{server.base_url}/mcp" if server else (self.tools_base_url
                                                              or "memory://mcp")
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                session = self._initialize(client)
                reply = self._request(client, session, "tools/call", {
                    "name": self.task_tool,
                    "arguments": {
                        "task": environment.scenario.task,
                        "scenario": environment.scenario.id,
                        "maxSteps": environment.scenario.max_steps,
                        "tools": {"transport": "streamable-http", "url": tools_url},
                        "actorChainHeader": "X-OPAAS-Actor-Chain",
                    },
                })
        except (httpx.HTTPError, AgentError, ValueError) as exc:
            return AgentRun(final=None, error=f"The MCP agent could not be used: {exc}")
        finally:
            if server:
                server.close()
            self._environment = None
        return _run_from_tool_result(reply)

    def _initialize(self, client: httpx.Client) -> str | None:
        response = self._post(client, None, {
            "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "opaas-smith", "version": "1"}}})
        _result_of(response)
        session = response.headers.get("mcp-session-id")
        self._post(client, session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return session

    def _request(self, client: httpx.Client, session: str | None, method: str,
                 params: dict[str, Any]) -> dict[str, Any]:
        response = self._post(client, session, {"jsonrpc": "2.0", "id": next(self._ids),
                                                "method": method, "params": params})
        return _result_of(response)

    def _post(self, client: httpx.Client, session: str | None,
              message: dict[str, Any]) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": MCP_PROTOCOL}
        if session:
            headers["Mcp-Session-Id"] = session
        response = client.post(self.endpoint, json=message, headers=headers)
        if response.status_code >= 400:
            raise AgentError(f"the agent answered HTTP {response.status_code}")
        return response


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC result, from a JSON body or the last data line of an SSE stream."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
        body: Any = next((m for m in reversed(messages) if "result" in m or "error" in m), None)
    else:
        body = response.json() if response.content else None
    if not isinstance(body, dict):
        raise AgentError("the agent sent no JSON-RPC answer")
    if "error" in body:
        error = body["error"] or {}
        raise AgentError(f"the agent answered error {error.get('code')}: {error.get('message')}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise AgentError("the agent's JSON-RPC answer has no result")
    return result


def _run_from_tool_result(result: dict[str, Any]) -> AgentRun:
    """The agent's answer: JSON in its text content ({final, record, log, turns}), or text."""
    if result.get("isError"):
        return AgentRun(final=None, error="The agent's task tool reported an error.")
    structured = result.get("structuredContent")
    texts = [c.get("text", "") for c in result.get("content") or []
             if isinstance(c, dict) and c.get("type") == "text"]
    body: Any = structured
    if body is None and texts:
        try:
            body = json.loads(texts[0])
        except json.JSONDecodeError:
            return AgentRun(final="\n".join(texts))
    if not isinstance(body, dict):
        return AgentRun(final="\n".join(texts) if texts else None)
    record = body.get("record")
    return AgentRun(
        final=None if body.get("final") is None else str(body["final"]),
        record=[r for r in record if isinstance(r, dict)] if isinstance(record, list) else None,
        log_text=body.get("log") if isinstance(body.get("log"), str) else None,
        turns=int(body.get("turns") or 0),
    )


def _mcp_handler_for(agent: McpAgent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            status, body = agent.handle_mcp_request(self.rfile.read(length),
                                                    dict(self.headers.items()))
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


_REGISTRY: dict[str, type[AgentAdapter]] = {
    "bare-model": BareModelAgent, "http": HttpAgent, "mcp": McpAgent,
}


def available_agents() -> list[str]:
    return sorted(_REGISTRY)


def agent_keeps_record(name: str | None) -> bool | None:
    """Whether the named adapter's agent keeps a record of its own; None if unknown.

    The system audit reads a smith report's auditability as *not applicable*
    when it is False: OPAAS ran that loop, and its record is OPAAS's (D60).
    """
    adapter = _REGISTRY.get(name or "")
    return None if adapter is None else adapter.keeps_record


__all__ = [
    "PROTOCOL",
    "AgentAdapter",
    "AgentError",
    "AgentRun",
    "BareModelAgent",
    "HttpAgent",
    "MCP_PROTOCOL",
    "McpAgent",
    "agent_keeps_record",
    "available_agents",
    "parse_reply",
]
