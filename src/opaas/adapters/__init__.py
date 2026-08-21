"""Registry of inference engine adapters.

`vllm`, `ollama` and the generic `openai` (which already covers NIM,
llama-server, TGI and LM Studio). The adapters still missing from Phase 2 —
llama.cpp via direct binding and in-process transformers — get registered here
without touching anything else.
"""

from __future__ import annotations

from opaas.adapters.base import (
    BackendError,
    GenerationResult,
    InferenceBackend,
    ResourceMetrics,
)
from opaas.adapters.ollama import OllamaBackend
from opaas.adapters.openai_compatible import OpenAICompatibleBackend
from opaas.adapters.vllm import VllmBackend

_REGISTRY: dict[str, type[InferenceBackend]] = {
    VllmBackend.name: VllmBackend,
    OllamaBackend.name: OllamaBackend,
    OpenAICompatibleBackend.name: OpenAICompatibleBackend,
}


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def get_backend(name: str) -> InferenceBackend:
    """Instantiates an adapter by name (the model is not loaded yet)."""
    try:
        backend_cls = _REGISTRY[name]
    except KeyError:
        raise BackendError(
            f"Unknown backend: {name!r}. Available: {', '.join(available_backends())}"
        ) from None
    return backend_cls()


def register_backend(backend_cls: type[InferenceBackend]) -> type[InferenceBackend]:
    """Registers an adapter. Usable as a decorator."""
    _REGISTRY[backend_cls.name] = backend_cls
    return backend_cls


__all__ = [
    "BackendError",
    "GenerationResult",
    "InferenceBackend",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "ResourceMetrics",
    "VllmBackend",
    "available_backends",
    "get_backend",
    "register_backend",
]
