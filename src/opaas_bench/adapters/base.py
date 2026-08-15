"""Common inference engine interface.

Every evaluation module (blocks A–E) talks to this interface, never directly
to vLLM/Ollama/transformers. Adding a new engine = adding an adapter, without
touching the suites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(slots=True)
class GenerationResult:
    """Result of one generation, with what block A metrics need."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    total_s: float
    ttft_s: float | None = None
    finish_reason: str | None = None
    #: True if prompt/completion tokens come from the server; False if estimated.
    token_counts_exact: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def output_tokens_per_s(self) -> float | None:
        """Output tokens per second excluding TTFT (decoding speed)."""
        decode_s = self.total_s if self.ttft_s is None else self.total_s - self.ttft_s
        if decode_s <= 0 or self.completion_tokens <= 0:
            return None
        return self.completion_tokens / decode_s


@dataclass(slots=True)
class ResourceMetrics:
    """Resource usage at an instant. Unavailable fields are left as None."""

    available: bool = False
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    ram_used_mb: float | None = None
    power_w: float | None = None
    gpu_util_pct: float | None = None
    gpu_count: int = 0
    #: Why information is missing (e.g. remote backend, no nvidia-smi).
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "vramUsedMb": self.vram_used_mb,
            "vramTotalMb": self.vram_total_mb,
            "ramUsedMb": self.ram_used_mb,
            "powerW": self.power_w,
            "gpuUtilPct": self.gpu_util_pct,
            "gpuCount": self.gpu_count,
            "note": self.note,
        }


class BackendError(RuntimeError):
    """Inference engine failure (connection, invalid response, missing model)."""


#: How the prompt is handed to the model.
#:
#: - 'chat' applies the model's template: it is how it is used in production
#:   and the only thing that really measures instruction following.
#: - 'raw' skips it and sends the text as is. It gives a cleaner measurement of
#:   the engine for block A, but it turns the model into a text completer:
#:   measured on IFEval with qwen2.5:7b, 88% with the template against 52%
#:   without it. This is not a style preference, it is measuring something else.
PROMPT_MODES = ("chat", "raw")
DEFAULT_PROMPT_MODE = "chat"


class InferenceBackend(ABC):
    """Contract that each engine implements.

    Lifecycle: ``load()`` → N × ``generate()`` → ``close()``. Usable as a
    context manager.
    """

    #: Adapter identifier exactly as passed to ``--backend``.
    name: ClassVar[str] = "abstract"

    #: False for in-process engines with no server (transformers): block A's
    #: concurrency metrics are not representative and are documented as a known
    #: limitation instead of being hidden.
    supports_concurrency: ClassVar[bool] = True

    #: True if the adapter can measure real TTFT (streaming response).
    supports_streaming: ClassVar[bool] = True

    def __init__(self) -> None:
        self._model_id: str | None = None
        self._config: dict[str, Any] = {}

    @property
    def model_id(self) -> str:
        if self._model_id is None:
            raise BackendError("The backend has not been loaded: call load() first")
        return self._model_id

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    @abstractmethod
    def load(self, model_id: str, config: dict[str, Any]) -> None:
        """Prepares the engine for ``model_id`` and checks that it responds."""

    @abstractmethod
    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        """Generates a response. Usual params: max_tokens, temperature, seed, stream."""

    @abstractmethod
    def get_resource_usage(self) -> ResourceMetrics:
        """Current resource usage (VRAM, RAM, power). May be unavailable."""

    @abstractmethod
    def get_backend_metadata(self) -> dict[str, Any]:
        """Engine version, detected quantization and everything that goes into the credential."""

    def context_window(self) -> int | None:
        """Tokens the engine will really process in a single request, if known.

        This is the *served* window, not the one the model was trained with: an
        engine happily serving a 32k model inside a 4096-token window trims the
        prompt and says nothing. Block C needs this number to tell that apart
        from a model that simply loses track of a long prompt.

        Returns None when the adapter cannot establish it. None means "unknown",
        never "unlimited", and the report says so.
        """
        return None

    def server_pid(self) -> int | None:
        """PID of the process serving the model, if it runs on this machine.

        It allows measuring the engine's RAM rather than the whole host's.
        Returns None when the engine is remote, in-process, or cannot be
        identified.
        """
        return None

    def close(self) -> None:  # noqa: B027 - deliberately optional: not every engine holds resources
        """Releases resources. By default, nothing."""

    def __enter__(self) -> InferenceBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
