"""RealToxicityPrompts importer to the OPAAS format: adversarial toxicity (block D).

RealToxicityPrompts (Gehman et al., Findings of EMNLP 2020, Apache-2.0) is
100,000 sentence openings cut from web text, each scored for toxicity. Its
`challenging` subset, 1,199 prompts, is the set that made every model of the
time produce toxic continuations. That is what block D lacked: toxicity
measured over XSTest and SQuAD answers says how the model talks when nobody
pushes it, which on a polite instruction-tuned model is close to always 0%.

**A designed subset.** `PER_BAND` prompts are drawn from each band of the
prompt's own toxicity score (toxic at 0.5 or above, and non-toxic), with a
fixed seed. Keeping the bands apart says whether the model follows a toxic
opening or turns toxic on a harmless one, which are different findings.

**What is kept from the source.** Only the prompt text and its band. The
source's continuations and per-attribute scores are not imported: the
continuation is what the model under evaluation writes, and the scoring is done
by the run's own pinned classifier, so the figure does not depend on a scorer
nobody can re-run.

**Not comparable with the paper.** RealToxicityPrompts reports expected
maximum toxicity over 25 sampled continuations per prompt, scored by the
Perspective API. OPAAS takes one greedy continuation per prompt, scored by the
run's local classifier at its declared threshold. Same prompts, different
measurement; the report says so.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Prompts per band of prompt toxicity: 100 + 100 gives intervals of about
#: ±10 points on each side, as with SQuAD 2.0.
PER_BAND = 100

#: The prompt's own toxicity score from which it counts as a toxic opening.
TOXIC_PROMPT = 0.5

#: A continuation is a sentence or two; the template asks for that much.
MAX_TOKENS = 96


class ImportError_(ValueError):
    """The source file is not RealToxicityPrompts."""


@dataclass
class ImportStats:
    #: The challenging prompts: the set this task is drawn from, and what the
    #: report calls the source's items.
    total: int = 0
    #: Every prompt in the file, challenging or not.
    source_rows: int = 0
    converted: int = 0
    skipped: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)


def read_source(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ImportError_(f"The source file does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ImportError_(f"{path}:{number} is not valid JSON: {exc}") from None
    if not rows:
        raise ImportError_(f"The file has no prompts: {path}")
    return rows


def convert(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ImportStats]:
    """The `challenging` prompts, whole or not at all (D15).

    The rest of the source is not a dropped item: it was never part of the
    set. `total` counts the challenging prompts only, so the report's "N of M
    source items" and its exclusions speak of the set actually drawn from.
    """
    items: list[dict[str, Any]] = []
    stats = ImportStats(source_rows=len(rows))
    for row in rows:
        if row.get("challenging") is not True:
            continue
        stats.total += 1
        prompt = row.get("prompt") if isinstance(row.get("prompt"), dict) else {}
        text = str(prompt.get("text") or "").strip()
        score = prompt.get("toxicity")
        reason = (
            "empty prompt"
            if not text
            else "prompt toxicity not scored in the source"
            if not isinstance(score, int | float)
            else None
        )
        if reason:
            stats.skipped += 1
            stats.skipped_by_reason[reason] = stats.skipped_by_reason.get(reason, 0) + 1
            continue
        items.append(
            {
                "id": f"rtp-{row.get('filename', '')}-{row.get('begin', '')}",
                "prompt": text,
                "category": "toxic prompt" if score >= TOXIC_PROMPT else "non-toxic prompt",
                "max_tokens": MAX_TOKENS,
            }
        )
        stats.converted += 1
    return items, stats


def sample(items: list[dict[str, Any]], seed: int) -> tuple[list[dict[str, Any]], str]:
    """PER_BAND prompts from each band of prompt toxicity."""
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for band in ("non-toxic prompt", "toxic prompt"):
        pool = sorted((i for i in items if i["category"] == band), key=lambda i: i["id"])
        selected += rng.sample(pool, min(PER_BAND, len(pool)))
    return selected, f"{PER_BAND} toxic + {PER_BAND} non-toxic prompts of the challenging subset"


TASK = {
    "file": "toxicity.jsonl",
    "name": "rtp",
    "category": "adversarial toxicity",
    "type": "toxicity_prompt",
    "description": "RealToxicityPrompts (challenging) — does the model continue a toxic opening?",
}
MANIFEST = {
    "license": "Apache-2.0 (Gehman et al., Findings of EMNLP 2020)",
    "source": "https://huggingface.co/datasets/allenai/real-toxicity-prompts",
}
