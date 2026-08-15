import json

import httpx
import pytest

from opaas_bench.adapters import available_backends, get_backend
from opaas_bench.adapters.base import BackendError
from opaas_bench.adapters.openai_compatible import OpenAICompatibleBackend
from opaas_bench.adapters.vllm import VllmBackend

MODEL = "fake/model-1b"


def _sse(*events: dict) -> str:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + "data: [DONE]\n\n"


def _handler(*, models=(MODEL,), completion_response=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        if request.url.path.endswith("/completions"):
            return completion_response(request)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def _load(transport, **config):
    """Defaults to 'raw' for the tests that use the /completions format."""
    backend = OpenAICompatibleBackend()
    backend.load(
        MODEL,
        {
            "base_url": "http://localhost:8000/v1",
            "transport": transport,
            "prompt_mode": "raw",
            **config,
        },
    )
    return backend


def test_the_default_prompt_mode_applies_the_chat_template():
    """Without a template an instruct model completes text: that is not what we want to measure."""
    backend = OpenAICompatibleBackend()
    backend.load(MODEL, {"base_url": "http://localhost:8000/v1", "transport": _handler()})
    metadata = backend.get_backend_metadata()
    assert metadata["promptMode"] == "chat"
    assert metadata["api"] == "chat"


def test_raw_mode_uses_the_plain_completions_endpoint():
    backend = _load(_handler())
    assert backend.get_backend_metadata()["promptMode"] == "raw"
    assert backend.get_backend_metadata()["api"] == "completions"


def test_an_unknown_prompt_mode_is_rejected():
    with pytest.raises(BackendError, match="Unknown prompt_mode"):
        _load(_handler(), prompt_mode="telepathy")


def test_registry_exposes_implemented_backends():
    assert available_backends() == ["ollama", "openai", "vllm"]
    assert isinstance(get_backend("vllm"), VllmBackend)


def test_unknown_backend_is_explicit():
    with pytest.raises(BackendError, match="Unknown backend"):
        get_backend("does-not-exist")


def test_load_fails_when_model_not_served():
    transport = _handler(models=("other/model",))
    with pytest.raises(BackendError, match="is not served"):
        _load(transport)


def test_load_fails_when_server_unreachable():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BackendError, match="Could not reach the server"):
        _load(httpx.MockTransport(handle))


def test_streaming_measures_ttft_and_uses_server_usage():
    def completion(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            text=_sse(
                {"choices": [{"text": "Hola"}]},
                {"choices": [{"text": " mundo", "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}},
            ),
        )

    backend = _load(_handler(completion_response=completion))
    result = backend.generate("test prompt", max_tokens=8)
    assert result.text == "Hola mundo"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 2
    assert result.token_counts_exact is True
    assert result.ttft_s is not None and result.ttft_s <= result.total_s
    assert result.finish_reason == "stop"


def test_missing_usage_is_flagged_as_estimated():
    def completion(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse({"choices": [{"text": "text"}]}))

    backend = _load(_handler(completion_response=completion))
    result = backend.generate("prompt", max_tokens=8)
    assert result.token_counts_exact is False
    assert result.completion_tokens > 0


def test_chat_api_sends_messages():
    seen = {}

    def completion(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_sse({"choices": [{"delta": {"content": "ok"}}]}))

    backend = OpenAICompatibleBackend()
    backend.load(
        MODEL,
        {
            "base_url": "http://localhost:8000/v1",
            "transport": _handler(completion_response=completion),
            "api": "chat",
        },
    )
    result = backend.generate("hola", max_tokens=4)
    assert seen["messages"] == [{"role": "user", "content": "hola"}]
    assert result.text == "ok"


def test_invalid_api_style_rejected():
    with pytest.raises(BackendError, match="api must be"):
        _load(_handler(), api="responses")


def test_the_low_level_api_key_still_works_for_programmatic_use():
    backend = OpenAICompatibleBackend()
    backend.load(
        MODEL, {"base_url": "http://localhost:8000/v1", "transport": _handler(), "api": "chat"}
    )
    assert backend.get_backend_metadata()["promptMode"] == "chat"


def test_http_error_becomes_backend_error():
    def completion(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    backend = _load(_handler(completion_response=completion))
    with pytest.raises(BackendError):
        backend.generate("prompt")


def test_backend_metadata_carries_traceability():
    backend = _load(_handler(), quantization="GGUF Q4_K_M")
    metadata = backend.get_backend_metadata()
    assert metadata["adapter"] == "openai"
    assert metadata["baseUrl"] == "http://localhost:8000/v1"
    assert metadata["quantization"] == "GGUF Q4_K_M"
    assert metadata["localEndpoint"] is True
    assert metadata["servedModels"] == [MODEL]


def test_remote_endpoint_flags_telemetry_caveat():
    backend = OpenAICompatibleBackend()
    backend.load(
        MODEL,
        {"base_url": "http://gpu-box.lan:8000/v1", "transport": _handler()},
    )
    usage = backend.get_resource_usage()
    if usage.available:
        assert "non-local" in (usage.note or "").lower()
    else:
        assert usage.note


def _models_handler(entries):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": entries})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_the_served_window_comes_from_max_model_len():
    """vLLM fixes it at startup, so reading it once at load time is enough."""
    backend = _load(_models_handler([{"id": MODEL, "max_model_len": 8192}]))
    assert backend.context_window() == 8192
    assert backend.get_backend_metadata()["contextWindowTokens"] == 8192


def test_a_server_that_hides_the_window_says_unknown_instead_of_guessing():
    backend = _load(_models_handler([{"id": MODEL}]))
    assert backend.context_window() is None
    assert "unknown" in backend.get_backend_metadata()["contextWindowNote"]
