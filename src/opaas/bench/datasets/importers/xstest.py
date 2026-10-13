"""XSTest importer to the OPAAS format.

XSTest (Röttger et al., NAACL 2024, CC-BY-4.0) is 250 safe prompts that *look*
dangerous and 200 genuinely harmful ones. It is exactly the pair block D needs:
the refusal rate on its own cannot tell a safe model from a useless one, and
this dataset exists precisely to measure that difference.

Direct mapping: `label: safe` → `expected: comply`, `label: unsafe` →
`expected: refuse`. XSTest's `type` column is kept as the category, which is
what makes it possible to see *what kind* of false positive the model makes
(homonyms, figurative language, risk groups…).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LABEL_TO_EXPECTATION = {"safe": "comply", "unsafe": "refuse"}


class ImportError_(ValueError):
    """The source file is not in the expected format."""


@dataclass
class ImportStats:
    total: int = 0
    converted: int = 0
    safe: int = 0
    unsafe: int = 0
    skipped: int = 0


def convert(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], ImportStats]:
    items: list[dict[str, Any]] = []
    stats = ImportStats(total=len(rows))

    for row in rows:
        label = (row.get("label") or "").strip().lower()
        expectation = _LABEL_TO_EXPECTATION.get(label)
        prompt = (row.get("prompt") or "").strip()
        if not expectation or not prompt:
            stats.skipped += 1
            continue

        items.append(
            {
                "id": f"xstest-{row.get('id') or len(items) + 1}",
                "prompt": prompt,
                "expected": expectation,
                # XSTest's type says what kind of false positive is being
                # provoked: homonyms, figurative language, risk groups...
                "category": (row.get("type") or "no type").strip(),
                "max_tokens": 320,
            }
        )
        stats.converted += 1
        if expectation == "comply":
            stats.safe += 1
        else:
            stats.unsafe += 1

    return items, stats


def read_source(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ImportError_(f"The source file does not exist: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "prompt" not in reader.fieldnames:
            raise ImportError_(
                f"{path} has no 'prompt' column. The XSTest CSV was expected, with "
                f"columns id,prompt,type,label,focus,note; found: {reader.fieldnames}"
            )
        rows = list(reader)
    if not rows:
        raise ImportError_(f"The file is empty: {path}")
    return rows


TASK = {
    "file": "safety.jsonl",
    "name": "xstest",
    "category": "refusal",
    "type": "refusal",
    "description": "XSTest — correct refusal versus over-refusal",
}
MANIFEST = {
    "license": "CC-BY-4.0 (Röttger et al., NAACL 2024)",
    "source": "https://github.com/paul-rottger/xstest",
}
