import json

from opaas_bench.datasets import load_dataset
from opaas_bench.suites.capability import CapabilitySuite

from .conftest import ScriptedBackend

OPTIONS = {"dataset": "opaas-smoke", "concurrency": 1}


def _backend(responder, **kwargs):
    backend = ScriptedBackend(responder, **kwargs)
    backend.load("fake/model-1b", {})
    return backend


def _tasks(result):
    return {task["category"]: task for task in result.metrics["tasks"]}


def _run(responder, options=None, **kwargs):
    return CapabilitySuite({**OPTIONS, **(options or {})}).run(_backend(responder, **kwargs))


def test_dataset_provenance_travels_into_the_metrics():
    result = _run(lambda prompt: "A")
    dataset = result.metrics["dataset"]
    assert dataset["name"] == "opaas-smoke"
    assert dataset["datasetHash"].startswith("sha256:")
    assert dataset["syntheticSmoke"] is True
    assert result.metrics["promptTemplateHash"].startswith("sha256:")


def test_smoke_dataset_is_flagged_as_not_certifiable():
    result = _run(lambda prompt: "A")
    assert any("SMOKE" in limitation for limitation in result.limitations)


def test_a_perfect_run_scores_full_marks():
    """A model that always gets it right: the statement identifies the item unambiguously."""
    answers = {
        item.prompt: item.answer
        for task in load_dataset("opaas-smoke").tasks
        if task.type == "multiple_choice"
        for item in task.items
    }

    def responder(prompt: str) -> str:
        for question, letter in answers.items():
            if question in prompt:
                return letter
        return "A"

    result = _run(responder)
    tasks = _tasks(result)
    assert tasks["reasoning"]["correct"] == 8
    assert tasks["reasoning"]["accuracy"]["point"] == 1.0
    assert tasks["science"]["correct"] == 8


def test_confidence_interval_is_reported_next_to_the_score():
    result = _run(lambda prompt: "A")
    accuracy = _tasks(result)["reasoning"]["accuracy"]
    assert accuracy["n"] == 8
    assert accuracy["low"] <= accuracy["point"] <= accuracy["high"]
    assert accuracy["high"] - accuracy["low"] > 0.2  # at n=8 the interval is enormous


def test_small_sample_warns_about_the_interval():
    result = _run(lambda prompt: "A")
    assert any("confidence interval is wide" in lim for lim in result.limitations)


def test_unparseable_answers_are_counted_apart_from_wrong_ones():
    result = _run(lambda prompt: "I am not sure about this one")
    task = _tasks(result)["reasoning"]
    assert task["unparsed"] == 8
    assert task["correct"] == 0
    assert any("do not state a recognizable option" in lim for lim in result.limitations)


def test_extraction_strategy_is_recorded():
    result = _run(lambda prompt: "Answer: B")
    task = _tasks(result)["reasoning"]
    assert task["extractionStrategies"] == {"explicit": 8}


def test_instruction_following_is_scored_by_constraints():
    def responder(prompt: str) -> str:
        if "JSON" in prompt:
            return '{"name": "x", "year": 2026}'
        return "short text"

    task = _tasks(_run(responder))["instruction-following"]
    assert task["items"] == 8
    assert task["correct"] >= 1
    breakdown = {c["kind"]: c for c in task["constraintBreakdown"]}
    assert breakdown["json_output"]["passed"] == 1
    assert breakdown["word_count_min"]["passed"] == 0


def test_constraint_breakdown_shows_which_instruction_is_ignored():
    task = _tasks(_run(lambda prompt: "AN ANSWER IN UPPERCASE, WITH A COMMA"))[
        "instruction-following"
    ]
    breakdown = {c["kind"]: c for c in task["constraintBreakdown"]}
    assert breakdown["all_lowercase"]["passed"] == 0
    assert breakdown["no_commas"]["passed"] == 0


def test_failures_are_sampled_for_debugging():
    result = _run(lambda prompt: "A")
    failures = _tasks(result)["science"]["failures"]
    assert 0 < len(failures) <= 5
    assert failures[0]["expected"] and failures[0]["got"] == "A"
    assert failures[0]["response"] == "A"


def test_backend_errors_leave_items_out_of_the_denominator():
    result = _run(lambda prompt: "A", fail_after=4)
    tasks = _tasks(result)
    assert tasks["reasoning"]["items"] == 4
    assert tasks["reasoning"]["errors"] == 4
    assert tasks["reasoning"]["accuracy"]["n"] == 4
    assert any("out of the denominator" in lim for lim in result.limitations)


def test_limit_shrinks_the_sample_and_says_so():
    result = _run(lambda prompt: "A", options={"limit": 3})
    assert _tasks(result)["reasoning"]["items"] == 3
    assert any("was trimmed to 3 items" in lim for lim in result.limitations)


def test_raw_prompt_mode_is_flagged_as_measuring_something_else(monkeypatch):
    """52% without the template against 88% with it: the figure does not mean the same."""
    backend = _backend(lambda prompt: "A")
    monkeypatch.setattr(backend, "get_backend_metadata", lambda: {"promptMode": "raw"})
    result = CapabilitySuite(OPTIONS).run(backend)
    assert any("'raw' mode" in lim for lim in result.limitations)
    assert any("--prompt-mode chat" in lim for lim in result.limitations)


def test_chat_prompt_mode_raises_no_such_flag(fake_backend):
    result = CapabilitySuite(OPTIONS).run(fake_backend)
    assert not any("'raw' mode" in lim for lim in result.limitations)


def test_no_aggregate_score_across_categories():
    """Each category on its own; no global score, as the design requires."""
    result = _run(lambda prompt: "A")
    serialized = json.dumps(result.metrics).lower()
    for forbidden in ("overall", '"score"', '"tier"', '"grade"'):
        assert forbidden not in serialized


def test_missing_dataset_fails_the_suite_without_crashing_the_run():
    result = CapabilitySuite({"dataset": "does-not-exist"}).run(_backend(lambda p: "A"))
    assert result.status == "failed"
    assert "does-not-exist" in result.error


def test_concurrency_preserves_item_order():
    serial = _run(lambda prompt: "A", options={"concurrency": 1})
    parallel = _run(lambda prompt: "A", options={"concurrency": 4})
    assert [t["failures"] for t in serial.metrics["tasks"]] == [
        t["failures"] for t in parallel.metrics["tasks"]
    ]
