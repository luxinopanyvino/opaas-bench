"""BBQ importer to the OPAAS format: social bias in question answering (block D).

BBQ (Parrish et al., Findings of ACL 2022, CC BY 4.0) asks who did something,
about two people who differ in one social attribute (age, disability, gender
identity, nationality, appearance, race, religion, socio-economic status,
sexual orientation). Every question comes in two versions:

- **ambiguous**: the context does not say who. The only correct answer is the
  "cannot be determined" option, and picking a person is picking on a
  stereotype;
- **disambiguated**: the context does say who. This is the control: a model
  that answered "cannot be determined" to everything would look unbiased and
  fail here.

Each question also has a **polarity**. A negative one ("who was bad at maths?")
is answered with bias by picking the person the stereotype targets; a
non-negative one ("who was good at maths?") by picking the other person. The
answer the stereotype targets is not in the question files: it is in the
dataset's own `additional_metadata.csv` (`target_loc`), which is why that file
is the source's main file and the nine category files travel with it.

Scoring is the letter of the chosen option, extracted as block B extracts it.
There is no judge (D11).

**What is left out.** The two intersectional files (race × SES, race × gender)
are not imported: the proposal measures one axis at a time and declares
intersectionality unmeasured (D32). Questions whose stereotyped answer the
metadata does not mark (16, all in gender identity) are dropped whole (D15).

**A designed subset, not a size.** Every category gets 100 ambiguous and 100
disambiguated questions, each half negative and half non-negative, drawn with
a fixed seed. Proportional sampling would give the largest categories most of
the items and leave sexual orientation with a handful. Equal cells keep every
category's intervals comparable, at about ±10 points.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The nine single-axis categories, as BBQ names its files.
CATEGORIES: tuple[str, ...] = (
    "Age",
    "Disability_status",
    "Gender_identity",
    "Nationality",
    "Physical_appearance",
    "Race_ethnicity",
    "Religion",
    "SES",
    "Sexual_orientation",
)

#: Items per (category, condition) cell. Each cell is split evenly between
#: negative and non-negative questions.
PER_CELL = 100

#: The answer is one letter; a short cap keeps 1,800 items affordable. An
#: operator's --safety-max-tokens still wins (D54).
MAX_TOKENS = 64

_LETTERS = "ABC"


class ImportError_(ValueError):
    """The source files are not BBQ."""


@dataclass
class ImportStats:
    total: int = 0
    converted: int = 0
    skipped: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    #: Why each dropped question was dropped, in BBQ's terms (see _convert_row).
    skipped_by_reason: dict[str, int] = field(default_factory=dict)


def read_source(path: Path) -> list[dict[str, Any]]:
    """`path` is the metadata file; the category files sit beside it."""
    if not path.exists():
        raise ImportError_(f"The source file does not exist: {path}")
    targets = _read_targets(path)
    rows: list[dict[str, Any]] = []
    for category in CATEGORIES:
        file = path.parent / f"{category}.jsonl"
        if not file.is_file():
            raise ImportError_(
                f"{file} is missing. BBQ needs its nine category files next to "
                f"{path.name}; fetch the source again with: opaas fetch bbq"
            )
        for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ImportError_(f"{file}:{number} is not valid JSON: {exc}") from None
            row["target_loc"] = targets.get((str(row.get("category")), str(row.get("example_id"))))
            rows.append(row)
    if not rows:
        raise ImportError_(f"No BBQ questions found next to {path}")
    return rows


def _read_targets(path: Path) -> dict[tuple[str, str], str]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "target_loc" not in reader.fieldnames:
            raise ImportError_(
                f"{path} has no 'target_loc' column. BBQ's "
                "analysis_scripts/additional_metadata.csv was expected."
            )
        return {(row["category"], row["example_id"]): row["target_loc"] for row in reader}


def convert(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ImportStats]:
    items: list[dict[str, Any]] = []
    stats = ImportStats(total=len(rows))

    for row in rows:
        item = _convert_row(row)
        if isinstance(item, str):
            stats.skipped += 1
            stats.skipped_by_reason[item] = stats.skipped_by_reason.get(item, 0) + 1
            continue
        items.append(item)
        stats.converted += 1
        stats.by_category[item["category"]] = stats.by_category.get(item["category"], 0) + 1

    return items, stats


def _convert_row(row: dict[str, Any]) -> dict[str, Any] | str:
    """A whole item, or the reason there is none (D15)."""
    try:
        target = int(float(str(row.get("target_loc"))))
    except (TypeError, ValueError):
        return "stereotyped answer not marked in the metadata"
    info = row.get("answer_info") or {}
    choices = [str(row.get(f"ans{i}") or "").strip() for i in range(3)]
    unknown = [i for i in range(3) if (info.get(f"ans{i}") or [None, None])[1] == "unknown"]
    label = row.get("label")
    if (
        not all(choices)
        or len(unknown) != 1
        or target not in (0, 1, 2)
        or target == unknown[0]
        or label not in (0, 1, 2)
        or row.get("question_polarity") not in ("neg", "nonneg")
        or row.get("context_condition") not in ("ambig", "disambig")
        or not row.get("context")
        or not row.get("question")
    ):
        return "malformed question record"

    return {
        "id": f"bbq-{row['category']}-{row['example_id']}",
        "category": str(row["category"]),
        "context": str(row["context"]).strip(),
        "prompt": str(row["question"]).strip(),
        "choices": choices,
        "answer": _LETTERS[label],
        "unknown": _LETTERS[unknown[0]],
        "target": _LETTERS[target],
        "polarity": row["question_polarity"],
        "condition": row["context_condition"],
        "max_tokens": MAX_TOKENS,
    }


def sample(items: list[dict[str, Any]], seed: int) -> tuple[list[dict[str, Any]], str]:
    """PER_CELL items per (category, condition), half of each polarity."""
    rng = random.Random(seed)
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in sorted(items, key=lambda i: i["id"]):
        key = (item["category"], item["condition"], item["polarity"])
        cells.setdefault(key, []).append(item)

    selected: list[dict[str, Any]] = []
    for key in sorted(cells):
        cell = cells[key]
        selected += rng.sample(cell, min(PER_CELL // 2, len(cell)))
    return selected, (
        f"{PER_CELL} ambiguous + {PER_CELL} disambiguated per category, "
        "half negative and half non-negative"
    )


TASK = {
    "file": "bias.jsonl",
    "name": "bbq",
    "category": "bias",
    "type": "bias_mc",
    "description": "BBQ — does the model pick a stereotype when the context does not say?",
}
MANIFEST = {
    "license": "CC-BY-4.0 (Parrish et al., Findings of ACL 2022)",
    "source": "https://github.com/nyu-mll/BBQ",
}
