"""The sentinel proxy: every call recorded as a signed, chained `AuditEvent` (D46, D50).

An OpenAI-compatible reverse proxy that sits in front of an engine (or any
HTTP tool server, including MCP over HTTP) and, for every request, writes one
`AuditEvent` to an `EventLog`: hash-chained, sealed in batches with an
operational key, and checked against the operator's structural policies as it
happens.

**This puts OPAAS on the data path, which it never was before (D27).** The
boundary drawn here: in `observe` mode the proxy changes nothing in the
traffic — same method, path, headers and bytes, both ways — and a policy
violation is recorded and alerted, never acted on. `gate` mode is opt-in:
policies marked `enforce: block` refuse the action, those marked `enforce:
approve` hold it for a human, and an action a policy cannot decide is held
too rather than rounded to allowed or refused (N1).

**What is kept (N4).** Request and response bodies are recorded as SHA-256
digests. Only the fields named in `keep` travel in clear: by default the
model, sampling settings, token usage, finish reason, and the *names* of
tools offered and called — never message content or tool arguments. A
policy over a value the proxy did not keep is undecidable, and the verdict
says which field to keep.

**A stream is kept as the response it adds up to (D64).** A streamed answer
arrives as many chunks, each carrying a fragment. The proxy assembles the
chunks into the document the same call would have returned unstreamed
(OpenAI chat and completions, the Responses API, Ollama chat and generate)
and applies `keep` to that, so `choices.*.message.content` kept under
`--keep-output` is the whole answer, not its last fragment. A stream in a
shape it does not know falls back to the last data line and says so
(`streamAssembled: false`); one longer than the parse limit is assembled
from its beginning and says that too (`streamTruncated`).

**Who acted comes from the caller, never from a guess.** The
`X-OPAAS-Actor-Chain` header (`user:alice>agent:planner`) or a chain the
operator configures for the whole proxy (labelled as configured, not as
received). W3C `traceparent` is recorded as the caller sent it. A request with
neither has no actor chain, and that is what attribution metrics will count.

**Phases.** `X-OPAAS-Phase` when the caller declares one; otherwise a repeat
of a call that just failed is a `retry`, and a different model called in the
same trace right after a failure is a `fallback`. Either way the record says
which (`phaseSource`).

**Overhead is measured on every request**: the time spent in the proxy before
the request leaves and after the response ends, excluding the upstream's own
time, and summarised as p50/p95/p99 so the cost of being audited is published
next to the audit.
"""

from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

import httpx

from opaas.core.events import (
    ACTOR_ROLES,
    PHASES,
    EventLog,
    parse_actor_chain,
    utcnow_iso,
)
from opaas.core.policy import UNDECIDABLE, VIOLATE, Policy, decide
from opaas.core.stats import summarize
from opaas.sentinel.gate import ApprovalQueue

MODES = ("observe", "gate")

#: Request and response fields kept in clear by default. Dotted paths; `*`
#: walks a list. Everything else is a digest.
DEFAULT_KEEP: tuple[str, ...] = (
    "model",
    "stream",
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "tools.*.function.name",
    "tools.*.name",
    "params.name",
    "method",
)
DEFAULT_KEEP_OUTPUT: tuple[str, ...] = (
    "model",
    "usage",
    "choices.*.finish_reason",
    "choices.*.message.tool_calls.*.function.name",
    "done_reason",
    "prompt_eval_count",
    "eval_count",
    "error.type",
)

_MODEL_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/embeddings",
                "/api/chat", "/api/generate", "/api/embed", "/api/embeddings")
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
#: Bodies above this are digested but not parsed for kept fields.
_PARSE_LIMIT = 4 * 1024 * 1024


class FaultInjector(Protocol):
    """What `swarm` plugs in to inject scripted faults. Returns the fault or None."""

    def pick(self, action: str, target: str | None) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class ProxyResponse:
    status: int
    headers: list[tuple[str, str]]
    body: Iterator[bytes]


@dataclass(slots=True)
class _Pending:
    record: dict[str, Any]
    started: float
    upstream_started: float | None = None
    hasher: Any = field(default_factory=hashlib.sha256)
    buffer: bytearray = field(default_factory=bytearray)
    status: int | None = None
    content_encoding: str | None = None
    truncated: bool = False


def pick(document: Any, paths: Sequence[str]) -> dict[str, Any]:
    """The kept subset of a JSON document, nested as in the document."""
    kept: dict[str, Any] = {}
    for path in paths:
        value = _walk(document, path.split("."))
        if value is not None and value != []:
            kept[path] = value
    return kept


def _walk(value: Any, parts: list[str]) -> Any:
    if not parts:
        return value
    head, rest = parts[0], parts[1:]
    if head == "*":
        if not isinstance(value, list):
            return None
        found = [_walk(item, rest) for item in value]
        flat = [f for f in found if f is not None]
        return flat or None
    if isinstance(value, dict) and head in value:
        return _walk(value[head], rest)
    return None


def _traceparent(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    return {"traceId": parts[1], "spanId": parts[2]}


class SentinelProxy:
    def __init__(
        self,
        upstream: str,
        log: EventLog,
        *,
        policies: Sequence[Policy] = (),
        mode: str = "observe",
        keep: Sequence[str] = DEFAULT_KEEP,
        keep_output: Sequence[str] = DEFAULT_KEEP_OUTPUT,
        actor_chain: str | None = None,
        queue: ApprovalQueue | None = None,
        approval_timeout_s: float = 300.0,
        faults: FaultInjector | None = None,
        alert: Callable[[dict[str, Any]], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 600.0,
        history: int = 10_000,
        name: str = "sentinel",
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"Mode must be one of {', '.join(MODES)}.")
        if mode == "gate" and queue is None and any(p.enforce == "approve" for p in policies):
            raise ValueError("Gate mode with 'approve' policies needs an approval queue.")
        self.upstream = upstream.rstrip("/")
        self.log = log
        self.policies = list(policies)
        self.mode = mode
        self.keep = tuple(keep)
        self.keep_output = tuple(keep_output)
        self.configured_chain = _configured_chain(actor_chain) if actor_chain else None
        self.queue = queue
        self.approval_timeout_s = approval_timeout_s
        self.faults = faults
        self.alert = alert
        self.name = name
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._failures: dict[tuple, bool] = {}
        self._last_failure_in_trace: dict[str, str] = {}
        self._lock = threading.Lock()
        self.overhead_ms: list[float] = []

    # ------------------------------------------------------------------ handle

    def handle(self, method: str, path: str, headers: dict[str, str],
               body: bytes) -> ProxyResponse:
        started = time.perf_counter()
        lowered = {k.lower(): v for k, v in headers.items()}
        record = self._pre_record(method, path, lowered, body)
        pending = _Pending(record=record, started=started)

        if self.mode == "gate":
            refused = self._gate(record, pending)
            if refused is not None:
                return refused

        fault = self.faults.pick(record["action"], record.get("target")) if self.faults else None
        if fault is not None:
            record.setdefault("extra", {})["injectedFault"] = fault
            if fault.get("kind") == "slow":
                time.sleep(float(fault.get("delayMs", 1000)) / 1000)
            elif fault.get("kind") in ("error", "malformed"):
                return self._synthetic(pending, fault)

        forward = [(k, v) for k, v in headers.items() if k.lower() not in _HOP_BY_HOP]
        pending.upstream_started = time.perf_counter()
        try:
            request = self._client.build_request(method, self.upstream + path,
                                                 headers=forward, content=body)
            response = self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            pending.status = 502
            message = json.dumps({"error": {"message": f"Upstream unreachable: {exc}",
                                            "type": "sentinel_upstream"}}).encode()
            pending.hasher.update(message)
            self._finish(pending, error=f"upstream unreachable: {type(exc).__name__}")
            return ProxyResponse(502, [("Content-Type", "application/json")], iter([message]))

        pending.status = response.status_code
        pending.content_encoding = response.headers.get("content-encoding")
        out_headers = [(k, v) for k, v in response.headers.items()
                       if k.lower() not in _HOP_BY_HOP]
        return ProxyResponse(response.status_code, out_headers, self._relay(response, pending))

    def _relay(self, response: httpx.Response, pending: _Pending) -> Iterator[bytes]:
        error = None
        # A response whose body is already in memory (some transports read it
        # eagerly) has nothing left to stream: relay those bytes as they are.
        chunks = iter([response.content]) if response.is_stream_consumed \
            else response.iter_raw()
        try:
            for chunk in chunks:
                pending.hasher.update(chunk)
                room = _PARSE_LIMIT - len(pending.buffer)
                pending.buffer.extend(chunk[:room])
                pending.truncated = pending.truncated or len(chunk) > room
                yield chunk
        except httpx.HTTPError as exc:
            error = f"upstream stream broke: {type(exc).__name__}"
        finally:
            response.close()
            if pending.status and pending.status >= 400 and error is None:
                error = f"upstream HTTP {pending.status}"
            self._finish(pending, error=error)

    # ------------------------------------------------------------------ record

    def _pre_record(self, method: str, path: str, headers: dict[str, str],
                    body: bytes) -> dict[str, Any]:
        document = None
        if body and len(body) <= _PARSE_LIMIT:
            try:
                document = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                document = None
        action, target = _classify(method, path, document)
        record: dict[str, Any] = {
            "schema": 1,
            "id": uuid.uuid4().hex,
            "ts": utcnow_iso(),
            "action": action,
            "source": self.name,
            "policyDecisions": [],
            "args": {"digest": "sha256:" + hashlib.sha256(body).hexdigest()},
            "extra": {"method": method, "path": path.split("?", 1)[0]},
        }
        if target:
            record["target"] = target
        kept = pick(document, self.keep) if isinstance(document, (dict, list)) else {}
        if kept:
            record["args"]["value"] = kept
        if headers.get("x-opaas-actor-chain"):
            record["actorChain"] = parse_actor_chain(headers["x-opaas-actor-chain"])
            record["extra"]["actorChainSource"] = "header"
        elif self.configured_chain:
            record["actorChain"] = [dict(hop) for hop in self.configured_chain]
            record["extra"]["actorChainSource"] = "proxy-configuration"
        trace = _traceparent(headers.get("traceparent"))
        if trace:
            record["trace"] = trace
        record["phase"], record["extra"]["phaseSource"] = self._phase(record, headers)
        return record

    def _key(self, record: dict[str, Any]) -> tuple:
        return (json.dumps(record.get("actorChain")), record.get("target"),
                record["args"]["digest"])

    def _phase(self, record: dict[str, Any], headers: dict[str, str]) -> tuple[str, str]:
        declared = headers.get("x-opaas-phase", "").strip().lower()
        if declared in PHASES:
            return declared, "header"
        with self._lock:
            if self._failures.get(self._key(record)):
                return "retry", "inferred-from-sequence"
            trace = (record.get("trace") or {}).get("traceId")
            failed_target = self._last_failure_in_trace.get(trace) if trace else None
        if failed_target is not None and failed_target != record.get("target"):
            return "fallback", "inferred-from-sequence"
        return "attempt", "default"

    def _finish(self, pending: _Pending, *, error: str | None) -> None:
        record = pending.record
        ended = time.perf_counter()
        record["output"] = {"digest": "sha256:" + pending.hasher.hexdigest()}
        if not pending.content_encoding and pending.buffer:
            kept = _kept_output(bytes(pending.buffer), self.keep_output,
                                truncated=pending.truncated)
            if kept:
                record["output"]["value"] = kept
        record["extra"]["status"] = pending.status
        if error:
            record["error"] = error
        if pending.upstream_started is not None:
            record["durationMs"] = round((ended - pending.started) * 1000, 3)
        # In gate mode the enforced policies were decided before forwarding;
        # the observe-only ones are decided now, with the response in hand.
        decided = {d["policy"] for d in record["policyDecisions"]}
        record["policyDecisions"] += self._decide(record, skip=decided)
        with self._lock:
            key = self._key(record)
            self._failures[key] = error is not None
            trace = (record.get("trace") or {}).get("traceId")
            if trace:
                if error is not None:
                    self._last_failure_in_trace[trace] = str(record.get("target"))
                else:
                    self._last_failure_in_trace.pop(trace, None)
        self._append(record)
        done = time.perf_counter()
        upstream_ms = ((ended - pending.upstream_started) * 1000
                       if pending.upstream_started is not None else 0.0)
        overhead = (done - pending.started) * 1000 - upstream_ms
        with self._lock:
            self.overhead_ms.append(round(overhead, 3))

    def _append(self, record: dict[str, Any]) -> None:
        self.log.append(record)
        with self._lock:
            self._history.append(record)
        for decision in record.get("policyDecisions", []):
            if decision["verdict"] in (VIOLATE, UNDECIDABLE) and self.alert:
                self.alert({"event": record["id"], "target": record.get("target"), **decision})

    # ---------------------------------------------------------------- policies

    def _decide(self, record: dict[str, Any], *, enforce: bool = False,
                skip: set[str] = frozenset()) -> list[dict[str, Any]]:
        """Each policy's verdict *for this event*, given what came before it."""
        with self._lock:
            history = list(self._history)
        decisions = []
        for policy in self.policies:
            if (enforce and policy.enforce == "observe") or policy.id in skip:
                continue
            verdict = decide(policy, [*history, record])
            mine = record["id"] in verdict.events
            outcome = verdict.verdict if mine else "comply"
            decision: dict[str, Any] = {"policy": policy.id, "verdict": outcome,
                                        "decidedAt": utcnow_iso(), "mode": self.mode}
            if outcome == UNDECIDABLE:
                decision["missing"] = verdict.missing
            decisions.append(decision)
        return decisions

    def _gate(self, record: dict[str, Any], pending: _Pending) -> ProxyResponse | None:
        decisions = self._decide(record, enforce=True)
        by_id = {p.id: p for p in self.policies}
        blocking = [d for d in decisions
                    if d["verdict"] == VIOLATE and by_id[d["policy"]].enforce == "block"]
        holding = [d for d in decisions if d not in blocking and d["verdict"] in
                   (VIOLATE, UNDECIDABLE)]
        record["policyDecisions"] = decisions
        if blocking:
            for d in blocking:
                d["enforced"] = "block"
            return self._refuse(pending, "block",
                                f"Blocked by policy {', '.join(d['policy'] for d in blocking)}.")
        if not holding:
            return None
        for d in holding:
            d["enforced"] = "pending"
        pending_record = {**record, "id": uuid.uuid4().hex, "phase": "pending",
                          "extra": {**record["extra"], "for": record["id"]}}
        self._append(pending_record)
        if self.queue is None:
            return self._refuse(pending, "block", "Held for approval, but no queue is set.")
        pending_id = self.queue.submit({
            "event": record["id"], "target": record.get("target"),
            "actorChain": record.get("actorChain"), "args": record["args"],
            "policies": holding,
        })
        decision = self.queue.wait(pending_id, self.approval_timeout_s)
        if decision is None or decision.decision != "approve":
            reason = "timed out" if decision is None else f"denied by {decision.by}"
            return self._refuse(pending, "block", f"Held for approval and {reason}.")
        approval = {
            "schema": 1, "id": uuid.uuid4().hex, "ts": decision.at, "action": "approval",
            "target": record["id"], "phase": "approval", "source": self.name,
            "actorChain": [{"role": "user", "id": decision.by}],
            "policyDecisions": [], "extra": {"pending": pending_id, "note": decision.note},
        }
        if record.get("trace"):
            approval["trace"] = record["trace"]
        self._append(approval)
        for d in holding:
            d["enforced"] = f"approved by {decision.by}"
        return None

    def _refuse(self, pending: _Pending, phase: str, message: str) -> ProxyResponse:
        body = json.dumps({"error": {"message": message, "type": "sentinel_policy"}}).encode()
        pending.record["phase"] = phase
        pending.status = 403
        pending.hasher.update(body)
        self._finish(pending, error=message)
        return ProxyResponse(403, [("Content-Type", "application/json")], iter([body]))

    def _synthetic(self, pending: _Pending, fault: dict[str, Any]) -> ProxyResponse:
        """An injected error or malformed tool call, without reaching the upstream."""
        if fault.get("kind") == "error":
            status = int(fault.get("status", 500))
            body = json.dumps({"error": {"message": "Injected fault (OPAAS swarm).",
                                         "type": "injected_fault"}}).encode()
            error = f"injected fault: HTTP {status}"
        else:
            status = 200
            body = json.dumps({"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "call_0", "type": "function",
                 "function": {"name": "injected", "arguments": "{not json"}}]},
                "finish_reason": "tool_calls"}]}).encode()
            error = None
        pending.status = status
        pending.hasher.update(body)
        pending.buffer.extend(body)
        self._finish(pending, error=error)
        return ProxyResponse(status, [("Content-Type", "application/json")], iter([body]))

    # ----------------------------------------------------------------- summary

    def overhead_summary(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self.overhead_ms)
        return {"unit": "ms", "definition": "time inside the proxy before the request leaves "
                "and after the response ends; the upstream's own time is excluded",
                **summarize(samples, digits=3)}

    def close(self) -> None:
        self.log.seal()
        self._client.close()


def _classify(method: str, path: str, document: Any) -> tuple[str, str | None]:
    """Model call, MCP tool call, or a plain message. Never guessed from content."""
    bare = path.split("?", 1)[0].rstrip("/")
    if isinstance(document, dict) and document.get("jsonrpc") and document.get(
        "method") == "tools/call":
        params = document.get("params") or {}
        return "tool_call", str(params.get("name")) if params.get("name") else None
    if any(bare.endswith(p) for p in _MODEL_PATHS):
        model = document.get("model") if isinstance(document, dict) else None
        return "model_call", str(model) if model else None
    if bare.startswith("/tools/"):
        return "tool_call", bare.rsplit("/", 1)[-1]
    return "message", f"{method} {bare}"


def _kept_output(body: bytes, keep: Sequence[str], *, truncated: bool = False) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        chunks = _stream_chunks(body)
        if not chunks:
            # Neither a JSON document nor a stream of them: nothing to keep,
            # except that a body cut at the parse limit was cut.
            return {"truncated": True} if truncated else {}
        marks: dict[str, Any] = {"streamed": True}
        if truncated:
            marks["streamTruncated"] = True
        assembled = assemble_stream(chunks)
        if assembled is not None:
            return {**pick(assembled, keep), **marks}
        # A shape this proxy does not assemble: what the last data line
        # carries (usage, finish reason), and a mark saying that is all.
        for chunk in reversed(chunks):
            kept = pick(chunk, keep)
            if kept:
                return {**kept, **marks, "streamAssembled": False}
        return {**marks, "streamAssembled": False}
    return pick(document, keep) if isinstance(document, (dict, list)) else {}


def _stream_chunks(body: bytes) -> list[dict[str, Any]]:
    """The JSON objects of an SSE (`data: {...}`) or NDJSON stream, in order."""
    chunks = []
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.removeprefix("data:").strip()
        if not line.startswith("{"):
            continue  # `event:` lines, `[DONE]`, keep-alive comments
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue  # the last line of a stream cut at the parse limit
        if isinstance(chunk, dict):
            chunks.append(chunk)
    return chunks


def assemble_stream(chunks: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The document a streamed call adds up to, or None for a shape not known here.

    Fragments that are strings are concatenated in order; everything else
    takes its last value, as the unstreamed response would carry it.
    """
    for chunk in reversed(chunks):
        if chunk.get("type") == "response.completed" and isinstance(chunk.get("response"),
                                                                     dict):
            return chunk["response"]  # Responses API: the last event is the whole response
    if any(isinstance(c.get("choices"), list) for c in chunks):
        return _assemble_openai(chunks)
    if any(isinstance(c.get("message"), dict) or isinstance(c.get("response"), str)
           for c in chunks) and any("done" in c for c in chunks):
        return _assemble_ollama(chunks)
    return None


def _assemble_openai(chunks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    choices: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for key, value in chunk.items():
            if key != "choices" and value is not None:
                document[key] = value
        for part in chunk.get("choices") or []:
            if not isinstance(part, dict):
                continue
            choice = choices.setdefault(int(part.get("index", 0)), {"index": part.get("index", 0)})
            if part.get("finish_reason") is not None:
                choice["finish_reason"] = part["finish_reason"]
            if isinstance(part.get("text"), str):  # /v1/completions
                choice["text"] = choice.get("text", "") + part["text"]
            delta = part.get("delta")
            if isinstance(delta, dict):
                _merge_delta(choice.setdefault("message", {}), delta)
    if document.get("object") == "chat.completion.chunk":
        document["object"] = "chat.completion"
    document["choices"] = [choices[i] for i in sorted(choices)]
    return document


def _merge_delta(message: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        if key == "tool_calls" and isinstance(value, list):
            calls = message.setdefault("tool_calls", [])
            for fragment in value:
                if not isinstance(fragment, dict):
                    continue
                index = int(fragment.get("index", len(calls)))
                while len(calls) <= index:
                    calls.append({})
                call = calls[index]
                for field_name in ("id", "type"):
                    if fragment.get(field_name) is not None:
                        call[field_name] = fragment[field_name]
                function = fragment.get("function")
                if isinstance(function, dict):
                    target = call.setdefault("function", {})
                    for name, piece in function.items():
                        if isinstance(piece, str):
                            target[name] = target.get(name, "") + piece
        elif key == "role" or not isinstance(value, str):
            if value is not None:
                message[key] = value
        else:
            message[key] = message.get(key, "") + value


def _assemble_ollama(chunks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for chunk in chunks:
        for key, value in chunk.items():
            if key == "message" and isinstance(value, dict):
                message = document.setdefault("message", {})
                for name, piece in value.items():
                    if name == "tool_calls" and isinstance(piece, list):
                        message.setdefault("tool_calls", []).extend(piece)
                    elif name in ("content", "thinking") and isinstance(piece, str):
                        message[name] = message.get(name, "") + piece
                    else:
                        message[name] = piece
            elif key == "response" and isinstance(value, str):  # /api/generate
                document["response"] = document.get("response", "") + value
            else:
                document[key] = value
    return document


# ------------------------------------------------------------------- server


def serve(proxy: SentinelProxy, host: str, port: int,
          ready: Callable[[str], None] | None = None) -> None:
    """Runs until interrupted. The handler only moves bytes; `handle` does the rest."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _any(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            result = proxy.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(result.status)
            for key, value in result.headers:
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in result.body:
                if chunk:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _any  # noqa: N815

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    if ready:
        ready(f"http://{host}:{server.server_address[1]}")
    try:
        with stop_on_terminate():
            server.serve_forever()
    finally:
        server.server_close()
        proxy.close()


#: Signals a service manager sends to stop a process: SIGTERM from `docker stop`
#: and systemd, SIGBREAK from a Windows console close or service stop.
_TERMINATE_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGBREAK") if hasattr(signal, name)
)


@contextmanager
def stop_on_terminate() -> Iterator[None]:
    """Turns a polite stop into the same clean exit as Ctrl-C.

    Python's default for SIGTERM is to die on the spot: no `finally`, so the
    tail of the log was left unsealed and the overhead summary never written,
    on exactly the stops a deployed sentinel gets. A kill that cannot be
    caught (SIGKILL, TerminateProcess) still loses them; the unsealed tail is
    then reported by `inquest`, not hidden.
    """
    if threading.current_thread() is not threading.main_thread():
        yield  # handlers can only be installed from the main thread
        return

    def _interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, _interrupt) for sig in _TERMINATE_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _configured_chain(text: str) -> list[dict[str, str]]:
    """The operator's --actor-chain, refused at start-up if it would never be complete.

    A header chain is recorded as sent: a malformed one is the caller's
    finding. A configured one is stamped on every request, so a typo in a role
    silently made every action unattributable (AC) for the life of the proxy.
    """
    chain = parse_actor_chain(text)
    unknown = sorted({hop["role"] for hop in chain} - set(ACTOR_ROLES))
    if not chain or unknown:
        what = f"unknown role(s) {', '.join(unknown)}" if unknown else "no role:id hop"
        raise ValueError(
            f"The actor chain '{text}' has {what}. Write it as role:id hops joined by '>', "
            f"with roles from: {', '.join(ACTOR_ROLES)}."
        )
    return chain



__all__ = [
    "DEFAULT_KEEP",
    "DEFAULT_KEEP_OUTPUT",
    "MODES",
    "FaultInjector",
    "ProxyResponse",
    "SentinelProxy",
    "assemble_stream",
    "pick",
    "serve",
]
