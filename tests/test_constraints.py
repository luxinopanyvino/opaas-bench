import pytest

from opaas.bench.constraints import CHECKERS, ConstraintError, check, check_all


def _check(kind, text, **params):
    ok, _ = check(text, {"kind": kind, **params})
    return ok


def test_word_count_bounds():
    assert _check("word_count_max", "one two three", value=3)
    assert not _check("word_count_max", "one two three four", value=3)
    assert _check("word_count_min", "one two three", value=3)
    assert not _check("word_count_min", "one two", value=3)


def test_word_count_ignores_punctuation():
    ok, detail = check("Hello, how are you?", {"kind": "word_count_max", "value": 4})
    assert ok
    assert "4 words" in detail


def test_contains_is_case_insensitive_by_default():
    assert _check("must_contain", "use enough VRAM", value="vram")
    assert not _check("must_contain", "use enough VRAM", value="vram", ignore_case=False)


def test_must_not_contain():
    assert _check("must_not_contain", "just any GPU", value="NVIDIA")
    assert not _check("must_not_contain", "an NVIDIA GPU", value="nvidia")


def test_starts_and_ends_with_ignore_surrounding_whitespace():
    assert _check("starts_with", "  TTFT is the latency", value="TTFT", ignore_case=False)
    assert not _check("starts_with", "The TTFT latency", value="TTFT", ignore_case=False)
    assert _check("ends_with", "end of the text.  ", value="text.")


def test_json_output_accepts_a_code_fence():
    assert _check("json_output", '```json\n{"a": 1}\n```')
    assert _check("json_output", '{"a": 1}', root="object")
    assert not _check("json_output", "[1, 2]", root="object")
    assert not _check("json_output", "this is not json")


def test_bullet_count_accepts_several_markers():
    text = "- one\n* two\n• three"
    assert _check("bullet_count", text, value=3)
    assert not _check("bullet_count", text, value=2)


def test_bullet_count_ignores_empty_bullets():
    assert _check("bullet_count", "- one\n-\n- two", value=2)


def test_line_count_max_ignores_blank_lines():
    assert _check("line_count_max", "one\n\ntwo", value=2)
    assert not _check("line_count_max", "one\ntwo\nthree", value=2)


def test_case_constraints():
    assert _check("all_lowercase", "all in lowercase")
    assert not _check("all_lowercase", "All in lowercase")
    assert _check("all_uppercase", "ALL IN UPPERCASE")


def test_no_commas():
    assert _check("no_commas", "no commas here")
    ok, detail = check("one, two, three", {"kind": "no_commas"})
    assert not ok
    assert "2 commas" in detail


def test_matches_regex():
    assert _check("matches_regex", "version 1.2.3", value=r"\d+\.\d+\.\d+")
    assert not _check("matches_regex", "no version", value=r"\d+\.\d+\.\d+")


def test_invalid_regex_is_reported_as_dataset_error():
    with pytest.raises(ConstraintError, match="Invalid regular expression"):
        check("text", {"kind": "matches_regex", "value": "([a-z"})


def test_unknown_constraint_lists_the_available_ones():
    with pytest.raises(ConstraintError, match="word_count_max"):
        check("text", {"kind": "made-up"})


def test_missing_parameter_is_explicit():
    with pytest.raises(ConstraintError, match="missing parameter"):
        check("text", {"kind": "word_count_max"})


def test_check_all_requires_every_constraint():
    constraints = [{"kind": "all_lowercase"}, {"kind": "no_commas"}]
    ok, details = check_all("text without commas", constraints)
    assert ok and len(details) == 2

    ok, details = check_all("Text without commas", constraints)
    assert not ok
    assert [d["ok"] for d in details] == [False, True]


def test_every_checker_is_reachable_through_check():
    for kind in CHECKERS:
        assert callable(CHECKERS[kind])
