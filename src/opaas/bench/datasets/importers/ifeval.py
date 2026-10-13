"""IFEval importer to the OPAAS format.

IFEval (Google, Apache-2.0) brings 541 prompts with programmatically checkable
instructions. Each item carries `instruction_id_list` and `kwargs`; they are
translated here into `opaas.bench.constraints` constraints.

**The importer downloads nothing**: it converts a file it is handed. Fetching
it — with its hash pinned and verified — belongs to `datasets/cache.py`, and
with `--offline` even that is forbidden, so an air-gapped deployment works from
a copied cache.

**An item whose constraint list cannot be checked in full is discarded**, and
how many and why is reported. Scoring an item while checking only part of its
instructions would give an inflated score, which is worse than not measuring
it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: IFEval instructions that cannot be checked here.
UNSUPPORTED: dict[str, str] = {
    "language:response_language": (
        "Requires automatic language identification, which would add a dependency "
        "with its own model and version to the result."
    ),
}


class ImportError_(ValueError):
    """The source file is not in the expected format."""


@dataclass
class ImportStats:
    total: int = 0
    converted: int = 0
    skipped: int = 0
    skipped_by_instruction: dict[str, int] = field(default_factory=dict)

    def skip(self, instruction: str) -> None:
        self.skipped += 1
        self.skipped_by_instruction[instruction] = (
            self.skipped_by_instruction.get(instruction, 0) + 1
        )


def _relation_min(relation: str) -> bool:
    """IFEval uses 'at least' / 'less than'."""
    return "least" in str(relation).lower()


def _count_constraints(kind_min: str, kind_max: str, value: int, relation: str) -> dict[str, Any]:
    if _relation_min(relation):
        return {"kind": kind_min, "value": value}
    # 'less than N' is strictly less; our _max checkers are inclusive.
    return {"kind": kind_max, "value": max(0, value - 1)}


def _occurrence_constraint(
    kind_min: str, kind_max: str, target: Any, times: int, relation: str
) -> dict[str, Any]:
    """'How many times X appears' constraints, which carry a value and a count."""
    if _relation_min(relation):
        return {"kind": kind_min, "value": target, "times": times}
    return {"kind": kind_max, "value": target, "times": max(0, times - 1)}


def translate(instruction_id: str, kwargs: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
    """Translates one IFEval instruction into our constraints.

    Raises KeyError with the id if it is not supported.
    """
    kwargs = {k: v for k, v in (kwargs or {}).items() if v is not None}

    match instruction_id:
        case "keywords:existence":
            return [{"kind": "must_contain", "value": word} for word in kwargs["keywords"]]
        case "keywords:forbidden_words":
            return [
                {"kind": "must_not_contain", "value": word} for word in kwargs["forbidden_words"]
            ]
        case "keywords:frequency":
            return [
                _occurrence_constraint(
                    "keyword_count_min", "keyword_count_max",
                    kwargs["keyword"], int(kwargs["frequency"]),
                    kwargs.get("relation", "at least"),
                )
            ]
        case "keywords:letter_frequency":
            return [
                _occurrence_constraint(
                    "letter_count_min", "letter_count_max",
                    kwargs["letter"], int(kwargs["let_frequency"]),
                    kwargs.get("let_relation", "at least"),
                )
            ]
        case "length_constraints:number_words":
            return [
                _count_constraints(
                    "word_count_min", "word_count_max",
                    int(kwargs["num_words"]), kwargs.get("relation", "at least"),
                )
            ]
        case "length_constraints:number_sentences":
            return [
                _count_constraints(
                    "sentence_count_min", "sentence_count_max",
                    int(kwargs["num_sentences"]), kwargs.get("relation", "at least"),
                )
            ]
        case "length_constraints:number_paragraphs":
            return [{"kind": "paragraph_count", "value": int(kwargs["num_paragraphs"])}]
        case "length_constraints:nth_paragraph_first_word":
            return [
                {
                    "kind": "nth_paragraph_first_word",
                    "paragraph": int(kwargs["nth_paragraph"]),
                    "value": kwargs["first_word"],
                }
            ]
        case "detectable_content:number_placeholders":
            return [{"kind": "placeholder_count_min", "value": int(kwargs["num_placeholders"])}]
        case "detectable_content:postscript":
            return [{"kind": "postscript", "value": kwargs["postscript_marker"]}]
        case "detectable_format:number_bullet_lists":
            return [{"kind": "bullet_count", "value": int(kwargs["num_bullets"])}]
        case "detectable_format:number_highlighted_sections":
            return [{"kind": "highlighted_count_min", "value": int(kwargs["num_highlights"])}]
        case "detectable_format:multiple_sections":
            return [
                {
                    "kind": "section_count",
                    "splitter": kwargs["section_spliter"],
                    "value": int(kwargs["num_sections"]),
                }
            ]
        case "detectable_format:json_format":
            return [{"kind": "json_output"}]
        case "detectable_format:title":
            return [{"kind": "title_format"}]
        case "detectable_format:constrained_response":
            return [
                {
                    "kind": "one_of",
                    "options": ["My answer is yes.", "My answer is no.", "My answer is maybe."],
                }
            ]
        case "combination:two_responses":
            return [{"kind": "two_responses"}]
        case "combination:repeat_prompt":
            return [
                {
                    "kind": "starts_with",
                    "value": kwargs.get("prompt_to_repeat", prompt),
                    "ignore_case": False,
                }
            ]
        case "startend:end_checker":
            return [{"kind": "ends_with", "value": kwargs["end_phrase"]}]
        case "startend:quotation":
            return [{"kind": "wrapped_in_quotes"}]
        case "change_case:english_capital":
            return [{"kind": "all_uppercase"}]
        case "change_case:english_lowercase":
            return [{"kind": "all_lowercase"}]
        case "change_case:capital_word_frequency":
            relation = kwargs.get("capital_relation", "at least")
            frequency = int(kwargs["capital_frequency"])
            return [
                _count_constraints(
                    "capital_word_count_min", "capital_word_count_max", frequency, relation
                )
            ]
        case "punctuation:no_comma":
            return [{"kind": "no_commas"}]
        case _:
            raise KeyError(instruction_id)


def convert(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ImportStats]:
    """Converts IFEval records into `instruction_following` items."""
    items: list[dict[str, Any]] = []
    stats = ImportStats(total=len(records))

    for record in records:
        for required in ("prompt", "instruction_id_list"):
            if required not in record:
                raise ImportError_(
                    f"Record with no '{required}' field: is this really IFEval? {record}"
                )

        prompt = str(record["prompt"])
        ids = list(record["instruction_id_list"])
        all_kwargs = list(record.get("kwargs") or [{}] * len(ids))

        constraints: list[dict[str, Any]] = []
        blocked: str | None = None
        for instruction_id, kwargs in zip(ids, all_kwargs, strict=False):
            try:
                constraints.extend(translate(str(instruction_id), kwargs, prompt))
            except (KeyError, TypeError, ValueError):
                blocked = str(instruction_id)
                break

        if blocked or not constraints:
            stats.skip(blocked or "no translatable constraints")
            continue

        items.append(
            {
                "id": str(record.get("key", f"ifeval-{len(items) + 1}")),
                "prompt": prompt,
                "constraints": constraints,
                "max_tokens": 700,
            }
        )
        stats.converted += 1

    return items, stats


def read_source(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ImportError_(f"The source file does not exist: {path}")
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ImportError_(f"Invalid JSON in {path}:{number}: {exc}") from None
    if not records:
        raise ImportError_(f"The file is empty: {path}")
    return records


TASK = {
    "file": "instructions.jsonl",
    "name": "ifeval",
    "category": "instruction-following",
    "type": "instruction_following",
    "description": "IFEval — programmatically checkable instructions",
}
MANIFEST = {
    "license": "Apache-2.0 (Google Research)",
    "source": "https://huggingface.co/datasets/google/IFEval",
}
