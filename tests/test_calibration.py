import pytest

from opaas_bench.classifiers.base import ClassifierMetadata
from opaas_bench.classifiers.calibration import CASES, calibrate


class _Stub:
    """Classifier with responses scripted per control case."""

    def __init__(self, decide, threshold=0.5):
        self.decide = decide
        self.threshold = threshold

    def score(self, payloads):
        return [self.decide(payload) for payload in payloads]

    def metadata(self):
        return ClassifierMetadata(
            name="stub", task="test", path="/stub", threshold=self.threshold
        )


def _perfect(task):
    expected = {_key(case.payload): case.expect_positive for case in CASES[task]}
    return _Stub(lambda payload: 0.95 if expected[_key(payload)] else 0.05)


def _key(payload):
    return tuple(payload) if isinstance(payload, tuple | list) else payload


def test_control_sets_declare_their_language():
    """The language of every control case is recorded so failures can be attributed."""
    for task in ("faithfulness", "toxicity"):
        languages = {case.language for case in CASES[task]}
        assert languages == {"en"}


def test_control_sets_have_both_polarities():
    for task in ("faithfulness", "toxicity"):
        polarities = {case.expect_positive for case in CASES[task]}
        assert polarities == {True, False}


def test_a_discriminating_classifier_passes():
    result = calibrate(_perfect("faithfulness"), "faithfulness")
    assert result["reliable"] is True
    assert result["correct"] == result["cases"]
    assert result["margin"] == pytest.approx(0.9)
    assert result["failures"] == []
    assert "is published" in result["verdict"]


def test_a_classifier_that_says_everything_is_positive_fails():
    result = calibrate(_Stub(lambda payload: 0.99), "toxicity")
    assert result["reliable"] is False
    assert result["margin"] == pytest.approx(0.0)
    assert "worse than not measuring" in result["verdict"]


def test_a_classifier_that_says_everything_is_negative_fails():
    result = calibrate(_Stub(lambda payload: 0.01), "toxicity")
    assert result["reliable"] is False
    assert result["failures"]


def test_an_inverted_classifier_gives_a_negative_margin():
    expected = {_key(case.payload): case.expect_positive for case in CASES["toxicity"]}
    inverted = _Stub(lambda payload: 0.05 if expected[_key(payload)] else 0.95)
    result = calibrate(inverted, "toxicity")
    assert result["reliable"] is False
    assert result["margin"] < 0


def test_the_failing_language_is_named_in_the_verdict():
    """When a control set fails, the verdict says which language it failed on."""
    result = calibrate(_Stub(lambda payload: 0.9), "faithfulness")
    assert result["reliable"] is False
    assert result["byLanguage"]["en"]["correct"] < result["byLanguage"]["en"]["total"]
    assert "It fails on: en" in result["verdict"]


def test_a_misplaced_threshold_is_fixed_not_rejected():
    """A broken instrument and a badly set dial are different things.

    A classifier that orders correctly but whose values never cross the
    threshold is usable: it is flagged and the right threshold is suggested.
    """
    expected = {_key(case.payload): case.expect_positive for case in CASES["toxicity"]}
    lukewarm = _Stub(
        lambda payload: 0.6 if expected[_key(payload)] else 0.4, threshold=0.9
    )
    result = calibrate(lukewarm, "toxicity")

    assert result["discriminates"] is True
    assert result["reliable"] is True  # it is published
    assert result["thresholdFits"] is False
    assert result["suggestedThreshold"] == pytest.approx(0.5)
    assert "--classifier-threshold 0.5" in result["verdict"]


def test_a_narrow_margin_is_published_but_flagged():
    expected = {_key(case.payload): case.expect_positive for case in CASES["toxicity"]}
    narrow = _Stub(lambda payload: 0.52 if expected[_key(payload)] else 0.48)
    result = calibrate(narrow, "toxicity")

    assert result["reliable"] is True
    assert result["thresholdFits"] is True
    assert result["narrowMargin"] is True
    assert "very narrow margin" in result["verdict"]


def test_a_clean_classifier_has_no_caveats():
    result = calibrate(_perfect("toxicity"), "toxicity")
    assert result["reliable"] is True
    assert result["thresholdFits"] is True
    assert result["narrowMargin"] is False
    assert result["suggestedThreshold"] is None


def test_an_unknown_task_has_no_control_set():
    result = calibrate(_Stub(lambda payload: 0.5), "made-up")
    assert result["available"] is False
    assert "No control cases" in result["reason"]


def test_failures_describe_what_went_wrong():
    result = calibrate(_Stub(lambda payload: 0.99), "faithfulness")
    failure = result["failures"][0]
    assert failure["description"]
    assert failure["language"] == "en"
    assert failure["expected"]
    assert 0.0 <= failure["score"] <= 1.0
