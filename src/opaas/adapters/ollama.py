"""Ollama adapter (Phase 2).

Ollama also exposes an OpenAI shim, but its native API (`/api/generate`) is
used here for two reasons that matter to block A: it returns the engine's
internal timings (`prompt_eval_duration`, `eval_duration`) and it allows
`raw: true`, which keeps the chat template from polluting the throughput
measurement with tokens you did not ask for.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, ClassVar

import httpx

from opaas.adapters.base import (
    DEFAULT_PROMPT_MODE,
    PROMPT_MODES,
    BackendError,
    GenerationResult,
    InferenceBackend,
    ResourceMetrics,
)
from opaas.telemetry.gpu import query_gpus
from opaas.telemetry.system import query_ram, resolve_listening_pid

_NS_PER_S = 1_000_000_000

#: Sentinel: distinguishes "not looked up yet" from "looked it up and there is no process".
_UNRESOLVED = -1
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _is_local(base_url: str) -> bool:
    return httpx.URL(base_url).host in _LOCAL_HOSTS


def _resolve_prompt_mode(config: dict[str, Any]) -> str:
    """Accepts `prompt_mode`, or the low-level `raw` for programmatic use."""
    if "raw" in config:
        return "raw" if config["raw"] else "chat"
    mode = str(config.get("prompt_mode") or DEFAULT_PROMPT_MODE)
    if mode not in PROMPT_MODES:
        raise BackendError(
            f"Unknown prompt_mode: {mode!r}. Valid ones: {', '.join(PROMPT_MODES)}"
        )
    return mode


class OllamaBackend(InferenceBackend):
    name: ClassVar[str] = "ollama"
    engine_label: ClassVar[str] = "Ollama"
    default_base_url: ClassVar[str] = "http://localhost:11434"

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.Client | None = None
        self._base_url: str = self.default_base_url
        self._raw: bool = True
        self._details: dict[str, Any] = {}
        self._version: str | None = None
        self._pid: int | None = _UNRESOLVED
        self._pid_note: str | None = None
        self._num_ctx: int | None = None
        self._digest: str | None = None

    # ---------------------------------------------------------------- lifecycle

    def load(self, model_id: str, config: dict[str, Any]) -> None:
        self._model_id = model_id
        self._config = dict(config)
        self._base_url = str(config.get("base_url") or self.default_base_url).rstrip("/")
        # The chat template is applied by default: without it the model completes
        # text instead of following instructions. See PROMPT_MODES.
        self._prompt_mode = _resolve_prompt_mode(config)
        self._raw = self._prompt_mode == "raw"
        # Not sending num_ctx means inheriting whatever the server defaults to
        # (OLLAMA_CONTEXT_LENGTH), which is usually far below what the model
        # supports and silently trims the prompt. See context_window().
        self._num_ctx = _positive_int(config.get("num_ctx"), "num_ctx")

        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(float(config.get("timeout_s", 300.0)), connect=10.0),
            transport=config.get("transport"),
        )
        self._assert_model_available()
        self._details = self._show()
        self._version = self._server_version()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise BackendError("The backend has not been loaded: call load() first")
        return self._client

    def _assert_model_available(self) -> None:
        try:
            response = self.client.get("/api/tags")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise BackendError(
                f"Could not reach Ollama at {self._base_url}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise BackendError(f"Non-JSON response from {self._base_url}/api/tags: {exc}") from exc

        models = [m for m in payload.get("models", []) if isinstance(m, dict)]
        available = [m.get("name") for m in models]
        # Ollama implicitly tags with ':latest'.
        for name in (self._model_id, f"{self._model_id}:latest"):
            if name in available:
                self._model_id = name
                self._digest = _prefixed_digest(models[available.index(name)].get("digest"))
                return
        raise BackendError(
            f"Model {self._model_id!r} is not downloaded in Ollama. "
            f"Available models: {', '.join(str(m) for m in available) or 'none'}. "
            f"Download it with: ollama pull {self._model_id}"
        )

    def _show(self) -> dict[str, Any]:
        try:
            response = self.client.post("/api/show", json={"model": self.model_id})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return {}

    def _server_version(self) -> str | None:
        try:
            response = self.client.get("/api/version")
            response.raise_for_status()
            return str(response.json().get("version"))
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None

    # ---------------------------------------------------------------- inference

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        options: dict[str, Any] = {
            "num_predict": int(params.pop("max_tokens", 128)),
            "temperature": float(params.pop("temperature", 0.0)),
        }
        if (seed := params.pop("seed", None)) is not None:
            options["seed"] = int(seed)
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx
        options.update(params.pop("options", {}))

        body = {
            "model": self.model_id,
            "prompt": prompt,
            "stream": bool(params.pop("stream", True)),
            "raw": self._raw,
            "options": options,
        }
        return self._stream(body) if body["stream"] else self._single(body)

    def _single(self, body: dict[str, Any]) -> GenerationResult:
        started = time.perf_counter()
        try:
            response = self.client.post("/api/generate", json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"/api/generate failed: {exc}") from exc
        return self._build(
            text=str(payload.get("response") or ""),
            final=payload,
            ttft_s=None,
            elapsed=time.perf_counter() - started,
        )

    def _stream(self, body: dict[str, Any]) -> GenerationResult:
        chunks: list[str] = []
        final: dict[str, Any] = {}
        ttft_s: float | None = None

        started = time.perf_counter()
        try:
            with self.client.stream("POST", "/api/generate", json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if error := event.get("error"):
                        raise BackendError(f"Ollama returned an error: {error}")
                    if piece := event.get("response"):
                        if ttft_s is None:
                            ttft_s = time.perf_counter() - started
                        chunks.append(piece)
                    if event.get("done"):
                        final = event
        except httpx.HTTPError as exc:
            raise BackendError(f"/api/generate streaming failed: {exc}") from exc

        return self._build(
            text="".join(chunks),
            final=final,
            ttft_s=ttft_s,
            elapsed=time.perf_counter() - started,
        )

    def _build(
        self, *, text: str, final: dict[str, Any], ttft_s: float | None, elapsed: float
    ) -> GenerationResult:
        return GenerationResult(
            text=text,
            prompt_tokens=int(final.get("prompt_eval_count") or 0),
            completion_tokens=int(final.get("eval_count") or 0),
            total_s=elapsed,
            ttft_s=ttft_s,
            finish_reason=final.get("done_reason"),
            token_counts_exact="eval_count" in final,
            raw={
                # The engine's internal timings, useful to cross-check against ours.
                "engineEvalS": _ns_to_s(final.get("eval_duration")),
                "enginePromptEvalS": _ns_to_s(final.get("prompt_eval_duration")),
                "engineLoadS": _ns_to_s(final.get("load_duration")),
                "engineTotalS": _ns_to_s(final.get("total_duration")),
            },
        )

    # ---------------------------------------------------------------- metadata

    def context_window(self) -> int | None:
        """The window this run is really served with, when it can be established.

        Ollama decides it per request from `num_ctx`, and when nobody sends one
        it falls back to the server's default — a number the API does not
        expose. So: pinned with `--num-ctx`, this returns it and the run is
        reproducible; without it, the honest answer is None.
        """
        return self._num_ctx

    def model_context_length(self) -> int | None:
        """Window the *model* was trained with, from `/api/show`.

        It is an upper bound on what could be served, never proof of what is
        being served: the gap between the two is exactly where silent
        truncation lives.
        """
        info = self._details.get("model_info") if isinstance(self._details, dict) else None
        if not isinstance(info, dict):
            return None
        for key, value in info.items():
            if str(key).endswith(".context_length"):
                return _positive_int(value, str(key))
        return None

    def model_digest(self) -> str | None:
        return self._digest

    def chat_template_hash(self) -> str | None:
        template = self._details.get("template") if isinstance(self._details, dict) else None
        if not isinstance(template, str) or not template:
            return None
        return "sha256:" + hashlib.sha256(template.encode("utf-8")).hexdigest()

    def server_pid(self) -> int | None:
        if not _is_local(self._base_url):
            return None
        if self._pid is _UNRESOLVED:
            port = httpx.URL(self._base_url).port or 11434
            self._pid, self._pid_note = resolve_listening_pid(port)
        return self._pid

    def get_resource_usage(self) -> ResourceMetrics:
        gpus = query_gpus()
        ram = query_ram(self.server_pid())
        if not gpus.available:
            return ResourceMetrics(
                available=ram.available,
                ram_used_mb=ram.process_rss_mb or ram.used_mb,
                note=gpus.note,
            )
        return ResourceMetrics(
            available=True,
            vram_used_mb=gpus.vram_used_mb,
            vram_total_mb=gpus.vram_total_mb,
            ram_used_mb=ram.process_rss_mb or ram.used_mb,
            power_w=gpus.power_w,
            gpu_util_pct=gpus.gpu_util_pct,
            gpu_count=len(gpus.gpus),
        )

    def get_backend_metadata(self) -> dict[str, Any]:
        details = self._details.get("details", {}) if isinstance(self._details, dict) else {}
        return {
            "adapter": self.name,
            "engine": self.engine_label,
            "engineVersion": self._version,
            "backend": f"Ollama {self._version}"
            if self._version
            else "Ollama (unknown version)",
            "baseUrl": self._base_url,
            "api": "native",
            "promptMode": self._prompt_mode,
            "raw": self._raw,
            "quantization": details.get("quantization_level") or self._config.get("quantization"),
            "parameterSize": details.get("parameter_size"),
            "family": details.get("family"),
            "numCtx": self._num_ctx,
            "contextWindowTokens": self.context_window(),
            "modelContextLength": self.model_context_length(),
            "contextWindowNote": (
                f"num_ctx pinned to {self._num_ctx} tokens on every request."
                if self._num_ctx is not None
                else "num_ctx NOT pinned: the server's default applies (OLLAMA_CONTEXT_LENGTH, "
                "typically far below what the model supports) and it is not exposed by the API. "
                "Pin it with --num-ctx so the served window is known and the run reproducible."
            ),
            "supportsConcurrency": self.supports_concurrency,
            "concurrencyNote": "Real parallelism is capped by OLLAMA_NUM_PARALLEL on the server: "
            "if it is lower than the requested concurrency, Ollama queues and the measurement "
            "reflects that queue, not the engine.",
            "localEndpoint": _is_local(self._base_url),
            "serverPid": self.server_pid(),
            "serverPidNote": self._pid_note,
        }


def _prefixed_digest(value: Any) -> str | None:
    """Ollama lists the digest as bare hex; the report spells every hash `sha256:…`."""
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BackendError(f"{field} must be an integer, received {value!r}") from None
    if number <= 0:
        raise BackendError(f"{field} must be greater than 0, received {number}")
    return number


def _ns_to_s(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(value / _NS_PER_S, 6)
