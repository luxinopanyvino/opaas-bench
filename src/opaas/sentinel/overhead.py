"""What being audited costs: the proxy's added latency, measured and published.

*Auditable Agents* reports 8.3 ms median for its mediation layer. OPAAS
measures its own the same way it measures everything else: p50/p95/p99 over
many requests, with the method stated.

Two measurements, never mixed:

- **in service**: the proxy times every real request (`overhead_summary`) and
  `opaas sentinel observe` writes the summary when it stops;
- **benchmark**: `benchmark_overhead` drives the proxy against an in-process
  upstream that answers instantly, so what remains is the proxy's own work —
  parsing, hashing, policy evaluation, the chained append and the seal. It
  excludes the extra network hop a deployment adds, and says so.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx

from opaas.core.events import EventLog
from opaas.core.policy import Policy
from opaas.sentinel.proxy import SentinelProxy

_COMPLETION = {
    "id": "cmpl-0", "object": "chat.completion", "model": "bench",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok " * 64},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 64, "completion_tokens": 64, "total_tokens": 128},
}


def benchmark_overhead(
    *,
    requests: int = 500,
    policies: list[Policy] | None = None,
    prompt_words: int = 256,
    signing_key: Any = None,
) -> dict[str, Any]:
    body = json.dumps({
        "model": "bench", "temperature": 0,
        "messages": [{"role": "user", "content": "word " * prompt_words}],
    }).encode()
    reply = json.dumps(_COMPLETION).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=reply))
    with tempfile.TemporaryDirectory() as directory:
        log = EventLog(Path(directory) / "bench.jsonl", signing_key=signing_key, seal_every=100)
        proxy = SentinelProxy("http://bench.invalid", log, policies=policies or [],
                              transport=transport)
        for index in range(requests):
            response = proxy.handle(
                "POST", "/v1/chat/completions",
                {"Content-Type": "application/json",
                 "X-OPAAS-Actor-Chain": f"user:bench>agent:a{index % 4}"},
                body,
            )
            b"".join(response.body)
        summary = proxy.overhead_summary()
        proxy.close()
    return {
        "method": "in-process benchmark against an instant upstream",
        "excludes": "the network hop the proxy adds in a deployment",
        "requests": requests,
        "policies": len(policies or []),
        "requestBytes": len(body),
        "sealed": signing_key is not None,
        "overhead": summary,
        "reference": {"paper": "Auditable Agents, mediation layer", "p50Ms": 8.3},
    }


__all__ = ["benchmark_overhead"]
