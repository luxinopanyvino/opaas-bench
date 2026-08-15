import json

import httpx
import pytest

from opaas_bench.adapters import get_backend
from opaas_bench.adapters.base import BackendError
from opaas_bench.adapters.ollama import OllamaBackend

MODEL = "llama3.3:70b"

TAGS = {
    "models": [
        {"name": MODEL, "details": {"quantization_level": "Q4_K_M", "parameter_size": "70B"}}
    ]
}
SHOW = {
    "details": {"quantization_level": "Q4_K_M", "parameter_size": "70B", "family": "llama"},
    "model_info": {"general.architecture": "llama", "llama.context_length": 32768},
}


def _ndjson(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _handler(*, tags=TAGS, generate=None):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json=tags)
        if path == "/api/show":
            return httpx.Response(200, json=SHOW)
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.6.2"})
        if path == "/api/generate":
            return generate(request)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def _load(transport, model=MODEL, **config):
    backend = OllamaBackend()
    backend.load(model, {"transport": transport, **config})
    return backend


def _done(**overrides) -> dict:
    return {
        "done": True,
        "done_reason": "length",
        "prompt_eval_count": 64,
        "eval_count": 24,
        "prompt_eval_duration": 500_000_000,
        "eval_duration": 2_000_000_000,
        "total_duration": 2_600_000_000,
        **overrides,
    }


def test_registered_under_its_own_name():
    assert isinstance(get_backend("ollama"), OllamaBackend)


def test_missing_model_names_the_pull_command():
    with pytest.raises(BackendError, match="ollama pull"):
        _load(_handler(), model="no-descargado")


def test_latest_tag_is_resolved():
    tags = {"models": [{"name": "mistral:latest", "details": {}}]}
    backend = _load(_handler(tags=tags), model="mistral")
    assert backend.model_id == "mistral:latest"


def test_unreachable_server_is_explicit():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BackendError, match="Could not reach Ollama"):
        _load(httpx.MockTransport(handle))


def test_streaming_collects_text_ttft_and_exact_counts():
    def generate(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["raw"] is False, "the chat template is applied by default"
        assert body["options"]["num_predict"] == 24
        assert body["options"]["seed"] == 7
        return httpx.Response(
            200,
            text=_ndjson(
                {"response": "Hola", "done": False},
                {"response": " mundo", "done": False},
                _done(response=""),
            ),
        )

    backend = _load(_handler(generate=generate))
    result = backend.generate("prompt", max_tokens=24, seed=7)
    assert result.text == "Hola mundo"
    assert result.prompt_tokens == 64
    assert result.completion_tokens == 24
    assert result.token_counts_exact is True
    assert result.ttft_s is not None
    assert result.finish_reason == "length"


def test_engine_internal_timings_are_preserved():
    def generate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson({"response": "x", "done": False}, _done()))

    result = _load(_handler(generate=generate)).generate("prompt", max_tokens=24)
    assert result.raw["engineEvalS"] == 2.0
    assert result.raw["enginePromptEvalS"] == 0.5


def test_error_inside_the_stream_surfaces():
    def generate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson({"error": "model not loaded"}))

    with pytest.raises(BackendError, match="model not loaded"):
        _load(_handler(generate=generate)).generate("prompt")


def test_raw_mode_skips_the_chat_template():
    """It can be requested, but it measures something else: 88% with the template
    against 52% without it."""

    def generate(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["raw"] is True
        return httpx.Response(200, text=_ndjson({"response": "x", "done": False}, _done()))

    backend = _load(_handler(generate=generate), prompt_mode="raw")
    assert backend.get_backend_metadata()["promptMode"] == "raw"
    backend.generate("prompt")


def test_the_default_applies_the_chat_template():
    assert _load(_handler()).get_backend_metadata()["promptMode"] == "chat"


def test_an_unknown_prompt_mode_is_rejected():
    with pytest.raises(BackendError, match="Unknown prompt_mode"):
        _load(_handler(), prompt_mode="telepathy")


def test_metadata_carries_quantization_and_concurrency_caveat():
    metadata = _load(_handler()).get_backend_metadata()
    assert metadata["backend"] == "Ollama 0.6.2"
    assert metadata["quantization"] == "Q4_K_M"
    assert metadata["parameterSize"] == "70B"
    assert "OLLAMA_NUM_PARALLEL" in metadata["concurrencyNote"]


def test_num_ctx_is_sent_on_every_request_when_it_is_pinned():
    """Without it the server's default applies and trims long prompts silently."""
    seen: list[dict] = []

    def generate(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["options"])
        return httpx.Response(200, text=_ndjson({"response": "x", "done": False}, _done()))

    backend = _load(_handler(generate=generate), num_ctx=8192)
    backend.generate("prompt", max_tokens=24)
    backend.generate("another prompt", max_tokens=24)
    assert [options["num_ctx"] for options in seen] == [8192, 8192]
    assert backend.context_window() == 8192


def test_without_num_ctx_the_served_window_is_declared_unknown():
    """Unknown is the honest answer: the API does not expose the server default."""

    def generate(request: httpx.Request) -> httpx.Response:
        assert "num_ctx" not in json.loads(request.content)["options"]
        return httpx.Response(200, text=_ndjson({"response": "x", "done": False}, _done()))

    backend = _load(_handler(generate=generate))
    backend.generate("prompt")
    assert backend.context_window() is None
    metadata = backend.get_backend_metadata()
    assert metadata["contextWindowTokens"] is None
    assert "--num-ctx" in metadata["contextWindowNote"]


def test_the_model_window_is_read_but_is_not_taken_as_the_served_one():
    """A 32k model served inside a 4k window is exactly the trap being detected."""
    backend = _load(_handler())
    assert backend.model_context_length() == 32768
    assert backend.context_window() is None
    assert backend.get_backend_metadata()["modelContextLength"] == 32768


def test_a_nonsensical_num_ctx_is_rejected_at_load_time():
    with pytest.raises(BackendError, match="num_ctx"):
        _load(_handler(), num_ctx=0)
