import pytest

from opaas.canonical import CanonicalizationError, canonicalize, canonicalize_str


def test_keys_are_sorted_and_whitespace_removed():
    assert canonicalize_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_nested_structures():
    assert canonicalize_str({"z": [1, {"b": None, "a": True}]}) == '{"z":[1,{"a":true,"b":null}]}'


def test_non_ascii_is_not_escaped():
    assert canonicalize_str({"k": "café"}) == '{"k":"café"}'
    assert canonicalize({"k": "café"}) == '{"k":"café"}'.encode()


def test_control_characters_use_short_escapes():
    assert canonicalize_str("a\nb\tc\x01") == '"a\\nb\\tc\\u0001"'


def test_quotes_and_backslashes_escaped():
    assert canonicalize_str('he said "hi"\\') == '"he said \\"hi\\"\\\\"'


def test_integral_floats_drop_the_decimal_point():
    """JCS follows ECMAScript: 1.0 serializes as 1, not as 1.0."""
    assert canonicalize_str({"a": 1.0, "b": 2.5}) == '{"a":1,"b":2.5}'


def test_booleans_are_not_confused_with_numbers():
    assert canonicalize_str([True, False, 1, 0]) == "[true,false,1,0]"


def test_negative_zero_matches_zero():
    assert canonicalize_str(-0.0) == "0"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e21, 1e-7])
def test_uncanonicalizable_numbers_fail_loudly(value):
    """Rather than sign bytes another language cannot reproduce, it fails."""
    with pytest.raises(CanonicalizationError):
        canonicalize(value)


def test_non_string_keys_rejected():
    with pytest.raises(CanonicalizationError):
        canonicalize({1: "one"})


def test_unsupported_type_rejected():
    with pytest.raises(CanonicalizationError):
        canonicalize({"k": object()})


def test_output_is_stable_across_dict_ordering():
    first = canonicalize({"a": 1, "b": {"c": 2, "d": 3}})
    second = canonicalize({"b": {"d": 3, "c": 2}, "a": 1})
    assert first == second


def test_sorting_uses_utf16_code_units_not_code_points():
    """Where JCS and Python's `sort_keys=True` disagree.

    U+1F600 encodes in UTF-16 as the pair D83D DE00, so it starts with 0xD83D
    and comes BEFORE U+FB00. By code point — which is what json.dumps would do
    — it would come after. Signing in the wrong order produces a credential no
    conforming verifier can validate.
    """
    encoded = canonicalize_str({"\U0001f600": 1, "ﬀ": 2})
    assert encoded.index('"\U0001f600"') < encoded.index('"ﬀ"')
    assert sorted(["\U0001f600", "ﬀ"]) == ["ﬀ", "\U0001f600"]
