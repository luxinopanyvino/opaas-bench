from __future__ import annotations

import time
from typing import Any, ClassVar

import pytest

from opaas_bench.adapters.base import GenerationResult, InferenceBackend, ResourceMetrics


class FakeBackend(InferenceBackend):
    """Deterministic backend for tests: no network, no GPU, controlled timings."""

    name: ClassVar[str] = "fake"
    engine_label: ClassVar[str] = "Fake Engine"
    default_base_url: ClassVar[str] = "memory://fake"

    def __init__(self, *, latency_s: float = 0.002, fail_after: int | None = None) -> None:
        super().__init__()
        self.latency_s = latency_s
        self.fail_after = fail_after
        self.calls = 0
        self.closed = False

    def load(self, model_id: str, config: dict[str, Any]) -> None:
        self._model_id = model_id
        self._config = dict(config)

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("simulated backend down")
        time.sleep(self.latency_s)
        max_tokens = int(params.get("max_tokens", 128))
        return GenerationResult(
            text="response " * max(1, max_tokens // 8),
            prompt_tokens=max(1, len(prompt.split())),
            completion_tokens=max_tokens,
            total_s=self.latency_s * 2,
            ttft_s=self.latency_s,
            finish_reason="length",
        )

    def get_resource_usage(self) -> ResourceMetrics:
        return ResourceMetrics(available=False, note="test backend")

    def get_backend_metadata(self) -> dict[str, Any]:
        return {"adapter": self.name, "engine": self.engine_label, "backend": "Fake Engine 0.0"}

    def close(self) -> None:
        self.closed = True


class ScriptedBackend(FakeBackend):
    """Answers according to a function of the prompt: lets hits and misses be scripted."""

    name: ClassVar[str] = "fake-scripted"

    def __init__(self, responder, prompt_token_cap: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.responder = responder
        #: Simulates an engine that trims the prompt: the count stops growing.
        self.prompt_token_cap = prompt_token_cap
        self.prompts: list[str] = []

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("simulated backend down")
        text = self.responder(prompt)
        counted = max(1, len(prompt.split()))
        if self.prompt_token_cap is not None:
            counted = min(counted, self.prompt_token_cap)
        return GenerationResult(
            text=text,
            prompt_tokens=counted,
            completion_tokens=max(1, len(text.split())),
            total_s=0.002,
            ttft_s=0.001,
            finish_reason="stop",
        )


class NoConcurrencyBackend(FakeBackend):
    """Equivalent to in-process `transformers`: no real concurrency."""

    name: ClassVar[str] = "fake-inprocess"
    supports_concurrency: ClassVar[bool] = False


@pytest.fixture()
def fake_backend() -> FakeBackend:
    backend = FakeBackend()
    backend.load("fake/model-1b", {})
    return backend


@pytest.fixture(autouse=True)
def _no_classifier_extra(monkeypatch):
    """No test may reach the pinned default classifiers.

    Once the classifiers extra is installed, block D resolves a default when no
    model path is given — which in a test means loading real weights, running
    real inference, and on a cold machine downloading ~800 MB in the middle of
    `pytest`. It also makes results depend on whether torch happens to be
    installed: the same test then passes in CI and fails on a developer's
    machine. Tests that exercise the default opt in by patching this back.
    """
    import opaas_bench.suites.safety as safety_module

    monkeypatch.setattr(safety_module, "classifiers_available", lambda: False)


@pytest.fixture(autouse=True)
def _isolate_registries():
    """The registries are global: a test that registers a backend must not leak it."""
    from opaas_bench import adapters, suites

    backends = dict(adapters._REGISTRY)
    suite_registry = dict(suites._REGISTRY)
    yield
    adapters._REGISTRY.clear()
    adapters._REGISTRY.update(backends)
    suites._REGISTRY.clear()
    suites._REGISTRY.update(suite_registry)
