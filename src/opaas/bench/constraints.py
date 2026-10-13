"""Constraint checkers for instruction-following (IFEval style).

IFEval's idea is to evaluate instruction following without a judge: every
instruction carries a programmatically checkable constraint. "Answer in fewer
than 50 words" is verified by counting, not by asking another model.

Each checker is a pure function `(text, params) -> (passes, detail)`, which
makes them testable one by one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

CheckResult = tuple[bool, str]
Checker = Callable[[str, dict[str, Any]], CheckResult]


class ConstraintError(ValueError):
    """The constraint does not exist or is missing parameters."""


def _words(text: str) -> list[str]:
    return re.findall(r"\b[\w'’-]+\b", text, flags=re.UNICODE)


def word_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    count = len(_words(text))
    target = int(params["value"])
    return count >= target, f"{count} words (minimum {target})"


def word_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    count = len(_words(text))
    target = int(params["value"])
    return count <= target, f"{count} words (maximum {target})"


def must_contain(text: str, params: dict[str, Any]) -> CheckResult:
    needle = str(params["value"])
    haystack, needle_cmp = _casefolded(text, needle, params)
    present = needle_cmp in haystack
    return present, f"{'contains' if present else 'missing'} {needle!r}"


def must_not_contain(text: str, params: dict[str, Any]) -> CheckResult:
    needle = str(params["value"])
    haystack, needle_cmp = _casefolded(text, needle, params)
    absent = needle_cmp not in haystack
    return absent, f"{'absent' if absent else 'present'} {needle!r}"


def starts_with(text: str, params: dict[str, Any]) -> CheckResult:
    prefix = str(params["value"])
    body, prefix_cmp = _casefolded(text.strip(), prefix, params)
    return body.startswith(prefix_cmp), f"starts with {text.strip()[:40]!r}"


def ends_with(text: str, params: dict[str, Any]) -> CheckResult:
    suffix = str(params["value"])
    body, suffix_cmp = _casefolded(text.strip(), suffix, params)
    return body.endswith(suffix_cmp), f"ends with {text.strip()[-40:]!r}"


def json_output(text: str, params: dict[str, Any]) -> CheckResult:
    """The whole text must be valid JSON, tolerating a code fence."""
    candidate = _strip_code_fence(text.strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return False, f"not valid JSON: {exc.msg}"
    if (expected := params.get("root")) and expected == "object":
        return isinstance(parsed, dict), f"root {type(parsed).__name__}, expected object"
    return True, f"valid JSON ({type(parsed).__name__})"


def bullet_count(text: str, params: dict[str, Any]) -> CheckResult:
    bullets = re.findall(r"^\s*[-*•]\s+\S", text, flags=re.MULTILINE)
    target = int(params["value"])
    return len(bullets) == target, f"{len(bullets)} bullets ({target} requested)"


def line_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    lines = [line for line in text.splitlines() if line.strip()]
    target = int(params["value"])
    return len(lines) <= target, f"{len(lines)} lines (maximum {target})"


def all_lowercase(text: str, params: dict[str, Any]) -> CheckResult:
    return text == text.lower(), "all lowercase" if text == text.lower() else "contains uppercase"


def all_uppercase(text: str, params: dict[str, Any]) -> CheckResult:
    return text == text.upper(), "all uppercase" if text == text.upper() else "contains lowercase"


def no_commas(text: str, params: dict[str, Any]) -> CheckResult:
    return "," not in text, "no commas" if "," not in text else f"{text.count(',')} commas"


def matches_regex(text: str, params: dict[str, Any]) -> CheckResult:
    pattern = str(params["value"])
    flags = re.IGNORECASE if params.get("ignore_case") else 0
    try:
        found = re.search(pattern, text, flags=flags | re.MULTILINE) is not None
    except re.error as exc:
        raise ConstraintError(f"Invalid regular expression {pattern!r}: {exc}") from None
    return found, f"{'matches' if found else 'does not match'} /{pattern}/"


# --------------------------------------------------------------------------
# Checkers added to cover IFEval's taxonomy. The missing ones are documented as
# unsupported in the importer and their items are discarded whole: scoring an
# item while checking only some of its constraints would give an inflated
# score.
# --------------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    """Approximation of the sentence count.

    IFEval uses nltk's tokenizer; this is a rule over terminal punctuation. It
    can differ on texts with abbreviations or decimals, so this constraint's
    figures are not identical to those published by IFEval.
    """
    parts = re.split(r"(?<=[.!?])[\"'”’)]*\s+", text.strip())
    return [part for part in parts if part.strip()]


def _paragraphs(text: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def sentence_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    count, target = len(_sentences(text)), int(params["value"])
    return count >= target, f"{count} sentences (minimum {target})"


def sentence_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    count, target = len(_sentences(text)), int(params["value"])
    return count <= target, f"{count} sentences (maximum {target})"


def paragraph_count(text: str, params: dict[str, Any]) -> CheckResult:
    count, target = len(_paragraphs(text)), int(params["value"])
    return count == target, f"{count} paragraphs ({target} requested)"


def nth_paragraph_first_word(text: str, params: dict[str, Any]) -> CheckResult:
    paragraphs = _paragraphs(text)
    index = int(params["paragraph"]) - 1
    expected = str(params["value"]).lower()
    if not 0 <= index < len(paragraphs):
        return False, f"only {len(paragraphs)} paragraphs; number {index + 1} was requested"
    words = _words(paragraphs[index])
    actual = words[0].lower() if words else ""
    return actual == expected, (
        f"paragraph {index + 1} starts with {actual!r}, expected {expected!r}"
    )


def keyword_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    keyword = str(params["value"])
    count = len(re.findall(re.escape(keyword), text, flags=re.IGNORECASE))
    target = int(params["times"])
    return count >= target, f"{keyword!r} appears {count} times (minimum {target})"


def keyword_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    keyword = str(params["value"])
    count = len(re.findall(re.escape(keyword), text, flags=re.IGNORECASE))
    target = int(params["times"])
    return count <= target, f"{keyword!r} appears {count} times (maximum {target})"


def letter_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    letter, target = str(params["value"]).lower(), int(params["times"])
    count = text.lower().count(letter)
    return count >= target, f"the letter {letter!r} appears {count} times (minimum {target})"


def letter_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    letter, target = str(params["value"]).lower(), int(params["times"])
    count = text.lower().count(letter)
    return count <= target, f"the letter {letter!r} appears {count} times (maximum {target})"


def capital_word_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    count = sum(1 for word in _words(text) if word.isupper() and len(word) > 1)
    target = int(params["value"])
    return count >= target, f"{count} all-caps words (minimum {target})"


def capital_word_count_max(text: str, params: dict[str, Any]) -> CheckResult:
    count = sum(1 for word in _words(text) if word.isupper() and len(word) > 1)
    target = int(params["value"])
    return count <= target, f"{count} all-caps words (maximum {target})"


def placeholder_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    count, target = len(re.findall(r"\[[^\]\n]+\]", text)), int(params["value"])
    return count >= target, f"{count} bracketed placeholders (minimum {target})"


def highlighted_count_min(text: str, params: dict[str, Any]) -> CheckResult:
    """Sections highlighted with *asterisks*, as IFEval requires."""
    count = len(re.findall(r"\*[^\*\n]+\*", text))
    target = int(params["value"])
    return count >= target, f"{count} highlighted sections (minimum {target})"


def section_count(text: str, params: dict[str, Any]) -> CheckResult:
    splitter = str(params["splitter"])
    count = len(re.findall(re.escape(splitter), text))
    target = int(params["value"])
    return count >= target, f"{count} occurrences of {splitter!r} ({target} requested)"


def title_format(text: str, params: dict[str, Any]) -> CheckResult:
    found = re.search(r"<<[^>\n]+>>", text) is not None
    return found, "has a title in << >>" if found else "no title in << >>"


def wrapped_in_quotes(text: str, params: dict[str, Any]) -> CheckResult:
    body = text.strip()
    wrapped = len(body) >= 2 and body[0] in '"“' and body[-1] in '"”'
    return wrapped, "wrapped in quotes" if wrapped else "not fully wrapped in quotes"


def postscript(text: str, params: dict[str, Any]) -> CheckResult:
    marker = str(params["value"])
    match = re.search(re.escape(marker), text, flags=re.IGNORECASE)
    if not match:
        return False, f"no {marker!r} postscript"
    has_body = bool(text[match.end() :].strip())
    return has_body, f"{marker!r} postscript " + ("with content" if has_body else "empty")


def one_of(text: str, params: dict[str, Any]) -> CheckResult:
    options = [str(option) for option in params["options"]]
    body = text.strip()
    matched = any(option.lower() in body.lower() for option in options)
    return matched, f"{'contains' if matched else 'does not contain'} an accepted answer"


def two_responses(text: str, params: dict[str, Any]) -> CheckResult:
    parts = [part for part in text.split("******") if part.strip()]
    return len(parts) == 2, f"{len(parts)} responses separated by ****** (2 requested)"


CHECKERS: dict[str, Checker] = {
    "word_count_min": word_count_min,
    "word_count_max": word_count_max,
    "must_contain": must_contain,
    "must_not_contain": must_not_contain,
    "starts_with": starts_with,
    "ends_with": ends_with,
    "json_output": json_output,
    "bullet_count": bullet_count,
    "line_count_max": line_count_max,
    "all_lowercase": all_lowercase,
    "all_uppercase": all_uppercase,
    "no_commas": no_commas,
    "matches_regex": matches_regex,
    "sentence_count_min": sentence_count_min,
    "sentence_count_max": sentence_count_max,
    "paragraph_count": paragraph_count,
    "nth_paragraph_first_word": nth_paragraph_first_word,
    "keyword_count_min": keyword_count_min,
    "keyword_count_max": keyword_count_max,
    "letter_count_min": letter_count_min,
    "letter_count_max": letter_count_max,
    "capital_word_count_min": capital_word_count_min,
    "capital_word_count_max": capital_word_count_max,
    "placeholder_count_min": placeholder_count_min,
    "highlighted_count_min": highlighted_count_min,
    "section_count": section_count,
    "title_format": title_format,
    "wrapped_in_quotes": wrapped_in_quotes,
    "postscript": postscript,
    "one_of": one_of,
    "two_responses": two_responses,
}


def check(text: str, constraint: dict[str, Any]) -> CheckResult:
    """Evaluates one constraint against a response."""
    kind = constraint.get("kind")
    if kind not in CHECKERS:
        raise ConstraintError(
            f"Unknown constraint: {kind!r}. Available: {', '.join(sorted(CHECKERS))}"
        )
    try:
        return CHECKERS[kind](text, constraint)
    except KeyError as exc:
        raise ConstraintError(f"Constraint {kind!r} is missing parameter {exc}") from None
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConstraintError):
            raise
        raise ConstraintError(f"Invalid parameters for {kind!r}: {exc}") from None


def check_all(text: str, constraints: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    """All constraints of an item. It passes only if every one of them passes."""
    details = []
    for constraint in constraints:
        ok, detail = check(text, constraint)
        details.append({"kind": constraint.get("kind"), "ok": ok, "detail": detail})
    return all(item["ok"] for item in details), details


def _casefolded(text: str, needle: str, params: dict[str, Any]) -> tuple[str, str]:
    if params.get("ignore_case", True):
        return text.casefold(), needle.casefold()
    return text, needle


def _strip_code_fence(text: str) -> str:
    match = re.match(r"^```(?:json)?\s*\n(.*?)\n?```$", text, flags=re.DOTALL)
    return match.group(1) if match else text
