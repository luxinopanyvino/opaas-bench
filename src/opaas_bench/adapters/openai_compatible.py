"""Adapter for any server that speaks the OpenAI protocol.

Covers vLLM, local NVIDIA NIM, llama-server (llama.cpp), TGI, LM Studio and
Ollama's OpenAI shim. Specific adapters inherit from this one and only change
defaults and metadata.
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

import httpx

from opaas_bench.adapters.base import (
    DEFAULT_PROMPT_MODE,
    PROMPT_MODES,
    BackendError,
    GenerationResult,
    InferenceBackend,
    ResourceMetrics,
)
from opaas_bench.telemetry.gpu import query_gpus
from opaas_bench.telemetry.system import query_ram, resolve_listening_pid

#: Approximation used only when the server does not return `usage`. It is flagged
#: in the report with `tokenCountsExact: false` so estimates are not presented as
#: measurements.
_CHARS_PER_TOKEN = 4.0

#: Sentinel: distinguishes "not looked up yet" from "looked it up and there is no process".
_UNRESOLVED = -1


class OpenAICompatibleBackend(InferenceBackend):
    """Speaks `/v1/completions` or `/v1/chat/completions` over HTTP."""

    name: ClassVar[str] = "openai"
    default_base_url: ClassVar[str] = "http://localhost:8000/v1"
    #: Which product this endpoint is, for the credential.
    engine_label: ClassVar[str] = "openai-compatible"

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.Client | None = None
        self._base_url: str = self.default_base_url
        self._api: str = "completions"
        self._server_info: dict[str, Any] = {}
        self._pid: int | None = _UNRESOLVED
        self._pid_note: str | None = None

    # ---------------------------------------------------------------- lifecycle

    def load(self, model_id: str, config: dict[str, Any]) -> None:
        self._model_id = model_id
        self._config = dict(config)
        self._base_url = str(config.get("base_url") or self.default_base_url).rstrip("/")
        # `/v1/chat/completions` applies the model's template; `/v1/completions`
        # does not. Without a template, an instruct model completes text instead
        # of following instructions — see PROMPT_MODES.
        self._prompt_mode = _resolve_prompt_mode(config)
        self._api = "chat" if self._prompt_mode == "chat" else "completions"

        headers = {"Content-Type": "application/json"}
        if api_key := config.get("api_key"):
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(float(config.get("timeout_s", 300.0)), connect=10.0),
            # Escape hatch for injecting transports (tests, unix sockets, proxies).
            transport=config.get("transport"),
        )
        self._server_info = self._probe_server()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise BackendError("The backend has not been loaded: call load() first")
        return self._client

    def _probe_server(self) -> dict[str, Any]:
        """Checks that the server responds and that the requested model is served."""
        try:
            response = self.client.get("/models")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise BackendError(
                f"Could not reach the server at {self._base_url}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise BackendError(f"Non-JSON response from {self._base_url}/models: {exc}") from exc

        entries = [entry for entry in payload.get("data", []) if isinstance(entry, dict)]
        served = [entry.get("id") for entry in entries]
        if served and self._model_id not in served:
            raise BackendError(
                f"Model {self._model_id!r} is not served at {self._base_url}. "
                f"Available models: {', '.join(str(m) for m in served)}"
            )
        # vLLM (and several compatible servers) publish the window the server
        # was started with. It is fixed for the whole process, so one read at
        # load time is enough.
        window = next(
            (
                entry.get("max_model_len")
                for entry in entries
                if entry.get("id") == self._model_id and entry.get("max_model_len")
            ),
            None,
        )
        return {"servedModels": served, "maxModelLen": _positive_int(window)}

    # ---------------------------------------------------------------- inference

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        stream = bool(params.pop("stream", True))
        body = self._build_body(prompt, stream=stream, **params)
        path = "/chat/completions" if self._api == "chat" else "/completions"
        return self._stream(path, body, prompt) if stream else self._single(path, body, prompt)

    def _build_body(self, prompt: str, *, stream: bool, **params: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": int(params.pop("max_tokens", 128)),
            "temperature": float(params.pop("temperature", 0.0)),
            "stream": stream,
        }
        if (seed := params.pop("seed", None)) is not None:
            body["seed"] = int(seed)
        if self._api == "chat":
            body["messages"] = [{"role": "user", "content": prompt}]
        else:
            body["prompt"] = prompt
        if stream:
            # vLLM/NIM/llama-server return `usage` in the last chunk with this.
            body["stream_options"] = {"include_usage": True}
        body.update(params)
        return body

    def _single(self, path: str, body: dict[str, Any], prompt: str) -> GenerationResult:
        started = time.perf_counter()
        try:
            response = self.client.post(path, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"{path} failed: {exc}") from exc
        elapsed = time.perf_counter() - started

        choice = (payload.get("choices") or [{}])[0]
        text = _extract_text(choice, self._api)
        prompt_tokens, completion_tokens, exact = self._token_counts(
            payload.get("usage"), prompt, text
        )
        return GenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_s=elapsed,
            # Without streaming, TTFT is not measurable: the first response is already the last.
            ttft_s=None,
            finish_reason=choice.get("finish_reason"),
            token_counts_exact=exact,
            raw={"logprobs": choice.get("logprobs")},
        )

    def _stream(self, path: str, body: dict[str, Any], prompt: str) -> GenerationResult:
        chunks: list[str] = []
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        ttft_s: float | None = None
        logprobs: Any = None

        started = time.perf_counter()
        try:
            with self.client.stream("POST", path, json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        piece = _extract_text(choice, self._api, streaming=True)
                        if piece:
                            if ttft_s is None:
                                ttft_s = time.perf_counter() - started
                            chunks.append(piece)
                        if choice.get("logprobs"):
                            logprobs = choice["logprobs"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except httpx.HTTPError as exc:
            raise BackendError(f"{path} streaming failed: {exc}") from exc

        elapsed = time.perf_counter() - started
        text = "".join(chunks)
        prompt_tokens, completion_tokens, exact = self._token_counts(usage, prompt, text)
        return GenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_s=elapsed,
            ttft_s=ttft_s,
            finish_reason=finish_reason,
            token_counts_exact=exact,
            raw={"logprobs": logprobs},
        )

    def _token_counts(
        self, usage: dict[str, Any] | None, prompt: str, text: str
    ) -> tuple[int, int, bool]:
        if usage and "prompt_tokens" in usage and "completion_tokens" in usage:
            return int(usage["prompt_tokens"]), int(usage["completion_tokens"]), True
        return (
            max(1, round(len(prompt) / _CHARS_PER_TOKEN)),
            max(0, round(len(text) / _CHARS_PER_TOKEN)),
            False,
        )

    # ---------------------------------------------------------------- metadata

    def server_pid(self) -> int | None:
        if not _is_local(self._base_url):
            return None
        if self._pid is _UNRESOLVED:
            port = httpx.URL(self._base_url).port or 80
            self._pid, self._pid_note = resolve_listening_pid(port)
        return self._pid

    def get_resource_usage(self) -> ResourceMetrics:
        """Local telemetry. Only valid if the server runs on this same host."""
        gpus = query_gpus()
        ram = query_ram(self.server_pid())
        remote_note = (
            None
            if _is_local(self._base_url)
            else "Non-local endpoint: the telemetry is from the host running OPAAS-Bench, "
            "not from the one serving the model"
        )
        if not gpus.available:
            return ResourceMetrics(
                available=ram.available,
                ram_used_mb=ram.process_rss_mb or ram.used_mb,
                note=remote_note or gpus.note,
            )
        return ResourceMetrics(
            available=True,
            vram_used_mb=gpus.vram_used_mb,
            vram_total_mb=gpus.vram_total_mb,
            ram_used_mb=ram.process_rss_mb or ram.used_mb,
            power_w=gpus.power_w,
            gpu_util_pct=gpus.gpu_util_pct,
            gpu_count=len(gpus.gpus),
            note=remote_note,
        )

    def get_backend_metadata(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "engine": self.engine_label,
            "engineVersion": self._engine_version(),
            "baseUrl": self._base_url,
            "api": self._api,
            "promptMode": self._prompt_mode,
            "servedModels": self._server_info.get("servedModels", []),
            "contextWindowTokens": self.context_window(),
            "contextWindowNote": (
                "Reported by the server as max_model_len: the window it was started with."
                if self.context_window() is not None
                else "The server does not publish max_model_len: the served window is unknown, "
                "so an engine trimming the prompt cannot be told apart from a model losing it."
            ),
            "quantization": self._config.get("quantization"),
            "supportsConcurrency": self.supports_concurrency,
            "localEndpoint": _is_local(self._base_url),
            "serverPid": self.server_pid(),
            "serverPidNote": self._pid_note,
        }

    def context_window(self) -> int | None:
        """`max_model_len` as published by the server, when it publishes it."""
        window = self._server_info.get("maxModelLen")
        return int(window) if window else None

    def _engine_version(self) -> str | None:
        """Generic OpenAI-compatible servers do not expose a version."""
        return None


def _extract_text(choice: dict[str, Any], api: str, *, streaming: bool = False) -> str:
    if api == "chat":
        source = choice.get("delta") if streaming else choice.get("message")
        if isinstance(source, dict):
            return str(source.get("content") or "")
        return ""
    return str(choice.get("text") or "")


def _is_local(base_url: str) -> bool:
    host = httpx.URL(base_url).host
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _positive_int(value: Any) -> int | None:
    """A window the server reports as 0, negative or garbage is 'unknown'."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _resolve_prompt_mode(config: dict[str, Any]) -> str:
    """Accepts `prompt_mode`, or the low-level `api` for programmatic use."""
    if api := config.get("api"):
        if api not in ("completions", "chat"):
            raise BackendError(f"api must be 'completions' or 'chat', received: {api!r}")
        return "chat" if api == "chat" else "raw"
    mode = str(config.get("prompt_mode") or DEFAULT_PROMPT_MODE)
    if mode not in PROMPT_MODES:
        raise BackendError(
            f"Unknown prompt_mode: {mode!r}. Valid ones: {', '.join(PROMPT_MODES)}"
        )
    return mode
