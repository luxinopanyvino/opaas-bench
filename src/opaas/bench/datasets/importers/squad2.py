"""SQuAD 2.0 importer to the OPAAS format: hallucination with a passage (block D).

SQuAD 2.0 (Rajpurkar, Jia and Liang, ACL 2018, CC BY-SA 4.0) pairs Wikipedia
passages with questions, and about half the questions have **no answer in the
passage**. Those were written by crowdworkers to look answerable. That is the
shape `grounded_qa` needs: on an unanswerable question the correct move is to
say the passage does not contain it, and answering anyway is exactly what
block D calls hallucinating. It is detected without a judge, by the abstention
patterns.

**Answerable items.** SQuAD lists several gold answers per question, from
different annotators, and they are *alternatives*: any one is correct. OPAAS's
`expected_facts` requires *every* fact to appear in the response, so only one
answer is kept, the shortest, because it is the one most likely to be
contained in any correct response. A model that paraphrases it ("about forty"
for "40") is scored as omitting it; the report says that scoring is literal.

**A fixed subset, not the whole set.** The dev set has 11,873 questions; every
one is a generation, so the full set would add hours to a run. `DEFAULT_LIMIT`
items are drawn by stratified sampling over answerable and unanswerable with a
fixed seed, and the manifest records the selection. `--limit` still overrides
it, like any other source.

**The passages are Wikipedia, so the model has probably seen them.** That
helps it on answerable questions and not on unanswerable ones, which are the
point. For a deployment's own documents, compose a dataset of them in
(docs/datasets.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Items imported when no `--limit` is given: about 100 answerable and 100
#: unanswerable, enough for intervals of roughly ±10 points on each side.
DEFAULT_LIMIT = 200


class ImportError_(ValueError):
    """The source file is not SQuAD 2.0."""


@dataclass
class ImportStats:
    total: int = 0
    converted: int = 0
    answerable: int = 0
    unanswerable: int = 0
    skipped: int = 0


def read_source(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ImportError_(f"The source file does not exist: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImportError_(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ImportError_(
            f"{path} has no 'data' list. The SQuAD 2.0 dev-v2.0.json file was expected."
        )
    rows = read_source_rows(document)
    if not rows:
        raise ImportError_(f"The file has no questions: {path}")
    return rows


def read_source_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Flattens the article → paragraph → question nesting into one row per question."""
    rows: list[dict[str, Any]] = []
    for article in document["data"]:
        for paragraph in article.get("paragraphs", []):
            for question in paragraph.get("qas", []):
                rows.append(
                    {
                        "id": question.get("id"),
                        "title": article.get("title"),
                        "context": paragraph.get("context"),
                        "question": question.get("question"),
                        "is_impossible": question.get("is_impossible"),
                        "answers": question.get("answers") or [],
                    }
                )
    return rows


def convert(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ImportStats]:
    items: list[dict[str, Any]] = []
    stats = ImportStats(total=len(rows))

    for row in rows:
        context = str(row.get("context") or "").strip()
        question = str(row.get("question") or "").strip()
        impossible = row.get("is_impossible")
        if not context or not question or not isinstance(impossible, bool):
            stats.skipped += 1
            continue

        if impossible:
            facts: list[str] = []
        else:
            answers = sorted(
                {str(a.get("text") or "").strip() for a in row["answers"]} - {""},
                key=lambda text: (len(text), text),
            )
            if not answers:
                # An answerable question with no gold answer cannot be scored:
                # the whole item goes, rather than a half-checkable one (D15).
                stats.skipped += 1
                continue
            facts = [answers[0]]

        items.append(
            {
                "id": f"squad2-{row.get('id')}",
                "context": context,
                "prompt": question,
                "answerable": not impossible,
                "expected_facts": facts,
                # Stratifying on this keeps both sides in any sample size.
                "category": "unanswerable" if impossible else "answerable",
            }
        )
        stats.converted += 1
        if impossible:
            stats.unanswerable += 1
        else:
            stats.answerable += 1

    return items, stats


TASK = {
    "file": "grounded.jsonl",
    "name": "squad2",
    "category": "hallucination",
    "type": "grounded_qa",
    "description": "SQuAD 2.0 — answer from the passage, or say it is not there",
}
MANIFEST = {
    "license": "CC-BY-SA-4.0 (Rajpurkar, Jia and Liang, ACL 2018)",
    "source": "https://rajpurkar.github.io/SQuAD-explorer/",
}
