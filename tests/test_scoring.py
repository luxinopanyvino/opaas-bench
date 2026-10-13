import pytest

from opaas.bench.scoring import (
    ScoringError,
    extract_choice,
    prompt_template_hash,
    render_multiple_choice,
)
from opaas.core.stats import wilson_interval


def test_multiple_choice_prompt_labels_options_with_letters():
    prompt = render_multiple_choice("How much is 2+2?", ["3", "4", "5"])
    assert "A. 3" in prompt
    assert "B. 4" in prompt
    assert "C. 5" in prompt
    assert prompt.rstrip().endswith("Answer:")


def test_cot_style_asks_for_reasoning_then_a_final_line():
    prompt = render_multiple_choice("How much is 2+2?", ["3", "4"], "cot")
    assert "step by step" in prompt
    assert "Answer: <letter>" in prompt


def test_unknown_answer_style_is_rejected():
    with pytest.raises(ScoringError, match="direct"):
        render_multiple_choice("?", ["a", "b"], "telepathy")


def test_prompt_template_hash_is_stable():
    assert prompt_template_hash() == prompt_template_hash()
    assert prompt_template_hash().startswith("sha256:")


def test_prompt_template_hash_separates_answer_styles():
    """A 'direct' run and a 'cot' one do not compare: the hash has to say so."""
    assert prompt_template_hash("direct") != prompt_template_hash("cot")


@pytest.mark.parametrize("text", ["B", "b", "B.", "(B)", " B ", "[B]"])
def test_bare_letter_is_extracted_exactly(text):
    extracted = extract_choice(text, 4)
    assert extracted.letter == "B"
    assert extracted.strategy == "exact"


@pytest.mark.parametrize(
    "text",
    [
        "Answer: C",
        "The correct answer is C.",
        "answer: C",
        "Option C is the correct one",
    ],
)
def test_explicit_formulas_are_extracted(text):
    extracted = extract_choice(text, 4)
    assert extracted.letter == "C"
    assert extracted.strategy == "explicit"


def test_loose_extraction_is_flagged_as_such():
    extracted = extract_choice("D) because the train travels at that speed", 4)
    assert extracted.letter == "D"
    assert extracted.strategy == "loose"


def test_chain_of_thought_answer_is_read_from_the_last_line():
    text = "Let us see: 120 km over 2 hours is 60 km/h.\nThat matches option B.\nAnswer: B"
    extracted = extract_choice(text, 4)
    assert extracted.letter == "B"
    assert extracted.strategy == "last line"


def test_reasoning_that_discards_options_does_not_hijack_the_answer():
    """Reasoning out loud names several options; the conclusion is what counts."""
    text = "Option A is incorrect and option B does not fit either.\nAnswer: C"
    assert extract_choice(text, 4).letter == "C"


def test_last_explicit_formula_wins_within_a_single_line():
    text = "I rule out option A because it does not add up, so the correct answer is D."
    assert extract_choice(text, 4).letter == "D"


def test_bold_markdown_letter_is_understood():
    assert extract_choice("**B**", 4).letter == "B"


def test_letters_outside_the_option_range_are_not_accepted():
    """With 3 options, a 'D' is not a valid answer."""
    assert extract_choice("D", 3).letter is None


def test_unparseable_answers_are_reported_not_guessed():
    for text in ["I do not know", "", "   "]:
        extracted = extract_choice(text, 4)
        assert not extracted.parsed
        assert extracted.letter is None


def test_wilson_interval_brackets_the_point_estimate():
    interval = wilson_interval(31, 50)
    assert interval["point"] == 0.62
    assert interval["low"] < 0.62 < interval["high"]
    assert interval["n"] == 50


def test_wilson_interval_narrows_with_more_samples():
    small = wilson_interval(31, 50)
    large = wilson_interval(620, 1000)
    assert (large["high"] - large["low"]) < (small["high"] - small["low"])


def test_wilson_interval_stays_inside_zero_one():
    perfect = wilson_interval(10, 10)
    assert perfect["point"] == 1.0
    assert perfect["high"] == 1.0
    assert perfect["low"] < 1.0

    zero = wilson_interval(0, 10)
    assert zero["low"] == 0.0
    assert zero["high"] > 0.0


def test_wilson_interval_without_samples():
    assert wilson_interval(0, 0) == {"point": None, "low": None, "high": None, "n": 0}
