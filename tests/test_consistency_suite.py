import itertools

from opaas_bench.suites.consistency import ConsistencySuite, _canary_prompt, _first_divergence

from .conftest import ScriptedBackend

OPTIONS = {
    "determinism_repeats": 4,
    "determinism_max_tokens": 32,
    "robustness_max_tokens": 16,
    "context_probe_words": [64],
}


def _run(responder, options=None, **kwargs):
    backend = ScriptedBackend(responder, **kwargs)
    backend.load("fake/model-1b", {})
    return CosistencyRun(CoSuite(options).run(backend), backend)


class CoSuite(ConsistencySuite):
    def __init__(self, options=None):
        super().__init__({**OPTIONS, **(options or {})})


class CosistencyRun:
    def __init__(self, result, backend):
        self.result = result
        self.backend = backend

    @property
    def metrics(self):
        return self.result.metrics


def _canary_aware(text_for_canary="OPAAS-CANARY-7Q42"):
    def responder(prompt: str) -> str:
        if "verification code" in prompt:
            return f"The code is {text_for_canary}."
        return "stable response"

    return responder


def test_a_deterministic_model_scores_one():
    run = _run(_canary_aware())
    determinism = run.metrics["determinism"]
    assert determinism["identicalRate"]["point"] == 1.0
    assert determinism["distinctOutputs"] == 1
    assert determinism["firstDivergenceChar"] is None


def test_a_nondeterministic_model_is_caught():
    counter = itertools.count()

    def responder(prompt: str) -> str:
        if "verification code" in prompt:
            return "The code is OPAAS-CANARY-7Q42."
        return f"variable response {next(counter)}"

    run = _run(responder)
    determinism = run.metrics["determinism"]
    assert determinism["identicalRate"]["point"] < 1.0
    assert determinism["distinctOutputs"] > 1
    assert determinism["firstDivergenceChar"] is not None
    assert any("NOT deterministic" in lim for lim in run.result.limitations)


def test_batching_caveat_is_always_stated():
    """100% serially does not guarantee determinism under load: it must always be said."""
    run = _run(_canary_aware())
    assert any("continuous batching" in lim for lim in run.result.limitations)


def test_first_divergence_points_at_the_right_character():
    assert _first_divergence(["abcdef", "abcXef"]) == 3
    assert _first_divergence(["abc", "abc"]) is None
    assert _first_divergence(["abc", "abcdef"]) == 3


def test_every_adversarial_case_is_exercised():
    run = _run(_canary_aware())
    robustness = run.metrics["robustness"]
    assert robustness["total"] == 9
    names = {case["case"] for case in robustness["cases"]}
    assert "empty prompt" in names
    assert "raw special tokens" in names


def test_an_explicit_error_counts_as_handled_not_as_crash():
    """Rejecting an invalid prompt with an error is correct behavior."""
    def responder(prompt: str) -> str:
        if not prompt.strip():
            raise RuntimeError("empty prompt not accepted")
        if "verification code" in prompt:
            return "The code is OPAAS-CANARY-7Q42."
        return "ok"

    run = _run(responder)
    empty = next(c for c in run.metrics["robustness"]["cases"] if c["case"] == "empty prompt")
    assert empty["outcome"] == "error"
    assert empty["engineBroken"] is False
    assert run.metrics["robustness"]["handled"] == 9
    assert run.metrics["robustness"]["skipped"] == 0


def test_an_engine_that_dies_after_a_case_is_flagged():
    state = {"alive": True}

    def responder(prompt: str) -> str:
        if not prompt.strip():
            state["alive"] = False
            raise RuntimeError("boom")
        if not state["alive"]:
            raise RuntimeError("the engine no longer responds")
        if "verification code" in prompt:
            return "The code is OPAAS-CANARY-7Q42."
        return "ok"

    run = _run(responder)
    robustness = run.metrics["robustness"]
    assert robustness["enginesBrokenAfterCase"] == ["empty prompt"]
    # The remaining cases were not tried: saying they failed would be as false
    # as saying they passed.
    assert robustness["evaluated"] == 1
    assert robustness["skipped"] == 8
    assert robustness["cases"][1]["outcome"] == "not run"
    assert any("stopped responding" in lim for lim in run.result.limitations)


def test_context_probe_confirms_the_window_when_the_canary_comes_back():
    run = _run(_canary_aware())
    context = run.metrics["context"]
    assert context["controlPassed"] is True
    assert context["largestVerifiedWords"] == 64
    assert context["contextLossDetected"] is False
    assert all(level["canaryEchoed"] for level in context["levels"])


def _loses_the_canary_when_long(prompt: str) -> str:
    """Remembers the canary with trivial context and loses it with long context."""
    if "verification code" in prompt and len(prompt) < 400:
        return "The code is OPAAS-CANARY-7Q42."
    return "Sure, here is a summary of the text."


def test_engine_truncation_is_confirmed_when_the_token_count_stops_growing():
    """The engine trims the prompt: it is the worst failure and it can be proven."""
    backend = ScriptedBackend(_loses_the_canary_when_long, prompt_token_cap=30)
    backend.load("fake/model-1b", {})
    result = CoSuite().run(backend)
    context = result.metrics["context"]

    assert context["contextLossDetected"] is True
    assert context["enginePromptTruncation"] == "confirmed"
    assert any("SILENT TRUNCATION CONFIRMED" in lim for lim in result.limitations)


def test_attention_loss_is_not_reported_as_engine_truncation():
    """If the engine did process the whole prompt, it is not to blame: say so."""
    backend = ScriptedBackend(_loses_the_canary_when_long)
    backend.load("fake/model-1b", {})
    result = CoSuite().run(backend)
    context = result.metrics["context"]

    assert context["contextLossDetected"] is True
    assert context["enginePromptTruncation"] == "ruled-out"
    assert any("the model's attention" in lim for lim in result.limitations)
    assert not any("CONFIRMED" in lim for lim in result.limitations)


def test_a_model_that_cannot_follow_the_instruction_is_not_accused_of_truncating():
    """A weak model must not be accused of a defect it does not have."""
    run = _run(lambda prompt: "Sure, here is a summary of the text.")
    context = run.metrics["context"]
    assert context["controlPassed"] is False
    assert context["status"] == "inconclusive"
    assert context["contextLossDetected"] is False
    assert context["largestVerifiedWords"] is None
    assert any("INCONCLUSIVE" in lim for lim in run.result.limitations)


def test_explicit_context_error_is_not_silent_truncation():
    """Rejecting a long prompt with an error is correct; truncating it quietly is not."""

    def responder(prompt: str) -> str:
        if "verification code" in prompt and len(prompt) > 400:
            raise RuntimeError("context length exceeded")
        if "verification code" in prompt:
            return "The code is OPAAS-CANARY-7Q42."
        return "ok"

    run = _run(responder)
    context = run.metrics["context"]
    assert context["controlPassed"] is True
    assert context["contextLossDetected"] is False
    assert context["levels"][-1]["status"] == "explicit-error"
    assert context["firstFailureWords"] == 64


def test_an_engine_that_fails_even_the_control_is_inconclusive():
    def responder(prompt: str) -> str:
        if "verification code" in prompt:
            raise RuntimeError("the engine will not start")
        return "ok"

    run = _run(responder)
    context = run.metrics["context"]
    assert context["controlPassed"] is False
    assert "trivial context" in context["detail"]


def test_canary_prompt_puts_the_code_at_the_start():
    prompt = _canary_prompt(50)
    assert prompt.index("OPAAS-CANARY-7Q42") < 60
    assert prompt.rstrip().endswith("at the beginning.")
