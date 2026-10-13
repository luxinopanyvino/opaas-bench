"""Block B scoring: prompts and answer extraction.

Two decisions that affect how comparable the figures are, and that are
recorded in the report:

1. **Scoring is done on generated text**, not on log-probabilities. HTTP
   backends do not expose logprobs uniformly, and a certificate that can only
   be issued with one particular engine is useless. Consequence: the scores
   are not directly comparable with those published by harnesses that score by
   likelihood.
2. **Unparseable answers are counted separately.** A model that knows the
   answer but does not respect the format is not the same as one that does not
   know it; both matter, so they are reported separately instead of being
   lumped together as a silent failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opaas.core.hashing import sha256_json

#: Multiple-choice templates. Changing them changes the results, so their hash
#: travels in the report (`promptTemplateHash`).
#:
#: `direct` measures answering with no room to reason: it is cheap and fast,
#: but it penalizes small models heavily on arithmetic and drags the result
#: toward format compliance. `cot` allows reasoning before answering, which is
#: the protocol MMLU-Pro numbers are published under. They are two different
#: measurements, not two ways of measuring the same thing.
MULTIPLE_CHOICE_TEMPLATES = {
    "direct": (
        "{question}\n\n"
        "{options}\n\n"
        "Reply only with the letter of the correct option, with no explanation.\n"
        "Answer:"
    ),
    "cot": (
        "{question}\n\n"
        "{options}\n\n"
        "Reason step by step, briefly, and finish with a final line "
        "exactly in the format 'Answer: <letter>'.\n"
    ),
}
ANSWER_STYLES = tuple(MULTIPLE_CHOICE_TEMPLATES)

INSTRUCTION_TEMPLATE = "{prompt}"


class ScoringError(ValueError):
    """Unknown answer style."""


def letters(count: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(count)]


def render_multiple_choice(question: str, choices: list[str], style: str = "direct") -> str:
    options = "\n".join(
        f"{letter}. {choice}" for letter, choice in zip(letters(len(choices)), choices, strict=True)
    )
    return _template(style).format(question=question, options=options)


def render_instruction(prompt: str) -> str:
    return INSTRUCTION_TEMPLATE.format(prompt=prompt)


def prompt_template_hash(style: str = "direct") -> str:
    """Identifies the templates used.

    The style is part of the hash: a `direct` run and a `cot` run are not
    comparable, and the report has to make that clear without anyone having to
    read the options.
    """
    return sha256_json(
        {
            "answerStyle": style,
            "multipleChoice": _template(style),
            "instruction": INSTRUCTION_TEMPLATE,
        }
    )


def _template(style: str) -> str:
    try:
        return MULTIPLE_CHOICE_TEMPLATES[style]
    except KeyError:
        raise ScoringError(
            f"Unknown answer style: {style!r}. Available: {', '.join(ANSWER_STYLES)}"
        ) from None


@dataclass(slots=True)
class ExtractedAnswer:
    letter: str | None
    strategy: str

    @property
    def parsed(self) -> bool:
        return self.letter is not None


def extract_choice(text: str, choice_count: int) -> ExtractedAnswer:
    """Pulls the chosen letter out of a free-form response.

    Several strategies are tried, from the strictest to the most permissive,
    and the one that worked is recorded: if most answers need the loose
    strategy, the accuracy figure is less reliable and that is worth knowing.
    """
    valid = set(letters(choice_count))
    body = text.strip()
    if not body:
        return ExtractedAnswer(None, "empty")

    # 1. The response is exactly the letter ("B", "B.", "(B)").
    if letter := _bare_letter(body, valid):
        return ExtractedAnswer(letter, "exact")

    # 2. Last line, which is where the answer ends in chain-of-thought mode
    #    ("...therefore\nAnswer: B").
    lines = [line for line in body.splitlines() if line.strip()]
    if len(lines) > 1:
        last = lines[-1].strip()
        if letter := _bare_letter(last, valid) or _explicit_letter(last, valid):
            return ExtractedAnswer(letter, "last line")

    # 3. Explicit formulas anywhere in the text: "The correct answer is C".
    #    The LAST one is taken: if the model reasons out loud, the conclusion
    #    is at the end, not at the beginning.
    if letter := _explicit_letter(body, valid):
        return ExtractedAnswer(letter, "explicit")

    # 4. First isolated letter in the text ("C. Because...", "(C) is correct").
    loose = re.search(r"(?:^|[\s(\[])([A-Z])(?=[)\].:,\s]|$)", body)
    if loose and (letter := loose.group(1).upper()) in valid:
        return ExtractedAnswer(letter, "loose")

    return ExtractedAnswer(None, "unparsed")


def _bare_letter(text: str, valid: set[str]) -> str | None:
    # The usual decorations are tolerated: parentheses, markdown bold, punctuation.
    match = re.fullmatch(r"[(\[]?\*{0,2}([A-Z])\*{0,2}[)\].:]?", text.strip(), flags=re.IGNORECASE)
    if match and (letter := match.group(1).upper()) in valid:
        return letter
    return None


def _explicit_letter(text: str, valid: set[str]) -> str | None:
    """Last explicit formula in the text.

    The letter has to stand alone: without the lookarounds, a word like
    "correct" contributes its last letter and the extraction returns garbage.
    """
    matches = re.findall(
        r"(?:answer|option|choice)\D{0,20}?(?<!\w)([A-Z])(?!\w)",
        text,
        flags=re.IGNORECASE,
    )
    for candidate in reversed(matches):
        if (letter := candidate.upper()) in valid:
            return letter
    return None
