"""Loading of local, versioned and hashed evaluation datasets.

A certificate has to say **exactly** what it was evaluated against. That rules
out downloading the dataset at run time: the certificate page shows the
dataset hash, and if the content can change under our feet the hash means
nothing.

Format: a directory with `dataset.json` (manifest) and one JSONL file per
task. The schema is documented in `docs/datasets.md`.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from opaas.bench.datasets.sources import available_sources, get_source
from opaas.core.hashing import sha256_json, text_file_digest

#: (item, where) -> None. Raises DatasetError when the item cannot be scored.
ItemValidator = Callable[["Item", str], None]

_Entry = TypeVar("_Entry")

MANIFEST_FILENAME = "dataset.json"
BUILTIN_DIR = Path(__file__).parent / "builtin"

#: Item types the block B and D runners know how to score.
#: Item types, each with the check that decides whether one is usable. A block
#: that brings its own kind of data — a security layer with injection attempts,
#: say — registers it here instead of editing this module, the same way an
#: engine registers an adapter and a block registers a suite.
#:
#: A validator raises `DatasetError` naming the file and line. It runs after the
#: common fields are read, so it only has to check what its own type requires.
_ITEM_TYPES: dict[str, ItemValidator] = {}


def register_item_type(name: str, validator: ItemValidator) -> None:
    """Registers an item type and the check that guards it."""
    _ITEM_TYPES[name] = validator


def available_item_types() -> list[str]:
    return sorted(_ITEM_TYPES)

#: Expected behavior on a `refusal` item.
REFUSAL_EXPECTATIONS = frozenset({"refuse", "comply"})


class DatasetError(ValueError):
    """The dataset does not exist, is malformed, or uses an unsupported type."""


@dataclass(slots=True)
class Item:
    """One evaluation item."""

    id: str
    type: str
    category: str
    #: The statement. In multiple_choice, the question; in instruction_following, the order.
    prompt: str
    choices: list[str] = field(default_factory=list)
    answer: str | None = None
    constraints: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int | None = None
    #: `refusal`: "refuse" if the request must be refused, "comply" if it is benign
    #: and refusing it would be a safety false positive.
    expected: str | None = None
    #: `grounded_qa`: passage the model must use as its only source.
    context: str | None = None
    #: `grounded_qa`: False if the answer is NOT in the passage; the correct move
    #: is then to abstain, and answering anyway is hallucinating.
    answerable: bool = True
    expected_facts: list[str] = field(default_factory=list)
    #: `bias_mc`: the letter of the "cannot be determined" option, the letter
    #: of the answer the stereotype points at, whether the question is phrased
    #: negatively ("who was bad at…") or not, and whether the context gives the
    #: answer away ("disambig") or does not ("ambig"). See importers/bbq.py.
    unknown: str | None = None
    target: str | None = None
    polarity: str | None = None
    condition: str | None = None


@dataclass(slots=True)
class Task:
    """One file of items: one block B category."""

    name: str
    category: str
    type: str
    description: str
    items: list[Item]
    source: str | None = None
    file_hash: str = ""
    #: Items the original source had, before anything was dropped.
    items_available: int | None = None
    #: Items that survived the import, before `--limit` trims anything.
    items_imported: int | None = None
    #: Items the import left out, and why. `{"count": 0, "byReason": {}}` when
    #: nothing was dropped; None when the manifest predates this field.
    excluded: dict[str, Any] | None = None


@dataclass(slots=True)
class Dataset:
    name: str
    version: str
    description: str
    tasks: list[Task]
    license: str | None = None
    source: str | None = None
    #: True on smoke datasets: they validate the pipeline, they do not measure.
    synthetic_smoke: bool = False
    content_hash: str = ""
    path: Path | None = None

    @property
    def item_count(self) -> int:
        return sum(len(task.items) for task in self.tasks)

    def provenance(self) -> dict[str, Any]:
        """What gets recorded in the report and ends up on the certificate page."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "source": self.source,
            "syntheticSmoke": self.synthetic_smoke,
            "datasetHash": self.content_hash,
            "itemCount": self.item_count,
            "tasks": [
                {
                    "name": task.name,
                    "category": task.category,
                    "type": task.type,
                    "items": len(task.items),
                    "fileHash": task.file_hash,
                    "source": task.source,
                    # What the source had against what is actually evaluated:
                    # without it, a report saying "IFEval" hides that part of
                    # IFEval was never run.
                    "itemsAvailable": task.items_available,
                    "itemsImported": task.items_imported,
                    "excluded": task.excluded,
                }
                for task in self.tasks
            ],
        }


def exclusion_limitations(tasks: Iterable[Task]) -> list[str]:
    """Items the import dropped, said out loud in the report.

    The importer already knew — it left them out on purpose, because scoring an
    item whose instructions cannot all be checked inflates the result. What it
    did not do was tell whoever reads the report, and a score computed over 94%
    of a public benchmark is not comparable with the published figure unless
    that is stated.

    It takes the tasks a block actually scored, not the whole dataset: blocks B
    and D share one composed dataset, and passing them all made each block
    declare the other's exclusions as if they were its own.
    """
    limitations: list[str] = []
    for task in tasks:
        excluded = task.excluded or {}
        if not excluded.get("count"):
            continue
        reasons = ", ".join(
            f"{reason} ({count})" for reason, count in sorted(excluded["byReason"].items())
        )
        limitations.append(
            f"Task '{task.name}': {excluded['count']} of {task.items_available} source items are "
            f"NOT evaluated: the import dropped them whole — {reasons}. "
            "The score covers the rest and is not directly comparable with figures published "
            "over the full dataset."
        )
    return limitations


def stratified_sample(
    entries: list[_Entry],
    limit: int | None,
    *,
    seed: int = 1234,
    key: Any,
) -> list[_Entry]:
    """Trims to `limit` while preserving each category's proportion.

    Taking "the first N" is a silent trap: in a refusal dataset sorted by id,
    the first ones are all benign and over-refusal would stop being measurable
    with nothing warning you. The sampling is deterministic given the seed, so
    the trimming stays reproducible.
    """
    if limit is None or limit >= len(entries):
        return entries

    strata: dict[str, list[_Entry]] = defaultdict(list)
    for entry in entries:
        strata[str(key(entry))].append(entry)

    rng = random.Random(seed)
    order = {id(entry): index for index, entry in enumerate(entries)}
    selected: list[_Entry] = []
    for name in sorted(strata):
        group = list(strata[name])
        rng.shuffle(group)
        share = max(1, round(limit * len(strata[name]) / len(entries)))
        selected.extend(group[:share])

    # Per-stratum rounding can overshoot: it is trimmed deterministically,
    # preserving the file's original order.
    selected.sort(key=lambda entry: order[id(entry)])
    return selected[:limit]


def builtin_datasets() -> list[str]:
    if not BUILTIN_DIR.is_dir():
        return []
    return sorted(p.name for p in BUILTIN_DIR.iterdir() if (p / MANIFEST_FILENAME).is_file())


def resolve_dataset_path(reference: str, *, offline: bool = False) -> Path:
    """Resolves a path, a bundled dataset or a known source.

    With a known source (`ifeval`, `xstest`) it is downloaded the first time and
    reused from the cache afterwards. The download is verified against a pinned
    hash; see `datasets/cache.py`.
    """
    candidate = Path(reference)
    if (candidate / MANIFEST_FILENAME).is_file() or candidate.is_file():
        return candidate
    builtin = BUILTIN_DIR / reference
    if (builtin / MANIFEST_FILENAME).is_file():
        return builtin

    # `ifeval,xstest` composes the two into a single dataset: a certificate is
    # issued from one run, not one per block. Known sources can be mixed with
    # your own directories — your documents, to measure hallucination.
    names = [part.strip() for part in reference.split(",") if part.strip()]
    resolvable = [
        name
        for name in names
        if get_source(name) is not None or (Path(name) / MANIFEST_FILENAME).is_file()
    ]
    if len(names) > 1 and len(resolvable) == len(names):
        from opaas.bench.datasets.cache import CacheError, ensure_composite

        try:
            return ensure_composite(names, offline=offline)
        except CacheError as exc:
            raise DatasetError(str(exc)) from None

    if get_source(reference) is not None:
        from opaas.bench.datasets.cache import CacheError, ensure_dataset

        try:
            return ensure_dataset(reference, offline=offline)
        except CacheError as exc:
            raise DatasetError(str(exc)) from None

    raise DatasetError(
        f"Dataset {reference!r} not found.\n"
        f"  Downloadable sources: {', '.join(available_sources()) or 'none'}"
        f" (combinable with commas: {','.join(available_sources())})\n"
        f"  Bundled: {', '.join(builtin_datasets()) or 'none'}\n"
        "  You can also pass the path to a directory with dataset.json "
        "(schema in docs/datasets.md)."
    )


def load_dataset(
    reference: str, *, limit: int | None = None, offline: bool = False
) -> Dataset:
    """Loads a dataset and computes its content hash.

    `limit` trims each task while preserving each category's proportion. The
    trimming shows up in the run hash — not the dataset one — because the
    dataset is still the same artifact: what changes is which part was
    evaluated.
    """
    root = resolve_dataset_path(reference, offline=offline)
    manifest_path = root / MANIFEST_FILENAME
    manifest = _read_json(manifest_path)

    for required in ("name", "version", "tasks"):
        if required not in manifest:
            raise DatasetError(f"{manifest_path}: missing the required field '{required}'")

    tasks: list[Task] = []
    for entry in manifest["tasks"]:
        tasks.append(_load_task(root, entry, limit=limit))

    dataset = Dataset(
        name=str(manifest["name"]),
        version=str(manifest["version"]),
        description=str(manifest.get("description", "")),
        license=manifest.get("license"),
        source=manifest.get("source"),
        synthetic_smoke=bool(manifest.get("syntheticSmoke", False)),
        tasks=tasks,
        path=root,
    )
    # The hash covers the manifest and every item file, not the selection.
    dataset.content_hash = sha256_json(
        {
            "manifest": manifest,
            "files": {task.name: task.file_hash for task in tasks},
        }
    )
    return dataset


def _load_task(root: Path, entry: dict[str, Any], *, limit: int | None) -> Task:
    if "file" not in entry:
        raise DatasetError(f"Task with no 'file' field in the manifest: {entry}")
    path = root / str(entry["file"])
    if not path.is_file():
        raise DatasetError(f"The manifest references a file that does not exist: {path}")

    item_type = str(entry.get("type", "multiple_choice"))
    if item_type not in _ITEM_TYPES:
        raise DatasetError(
            f"Unsupported item type: {item_type!r}. "
            f"Supported: {', '.join(available_item_types())}"
        )

    category = str(entry.get("category", path.stem))
    items = [
        _build_item(raw, item_type, category, path, index)
        for index, raw in enumerate(_read_jsonl(path))
    ]
    if not items:
        raise DatasetError(f"The task file is empty: {path}")

    return Task(
        name=str(entry.get("name", path.stem)),
        category=category,
        type=item_type,
        description=str(entry.get("description", "")),
        source=entry.get("source"),
        items_available=entry.get("itemsAvailable"),
        items_imported=entry.get("itemsImported"),
        excluded=entry.get("excluded"),
        items=stratified_sample(items, limit, seed=1234, key=lambda item: item.category),
        # The hash is of the whole file, not the selection, and of the content,
        # not the bytes: CRLF and LF describe the same dataset.
        file_hash=text_file_digest(path),
    )


def _build_item(
    raw: dict[str, Any], item_type: str, category: str, path: Path, index: int
) -> Item:
    where = f"{path}:{index + 1}"
    prompt = raw.get("prompt") or raw.get("question")
    if not prompt:
        raise DatasetError(f"{where}: the item has neither 'prompt' nor 'question'")

    item = Item(
        id=str(raw.get("id", f"{path.stem}-{index + 1}")),
        type=item_type,
        category=str(raw.get("category", category)),
        prompt=str(prompt),
        choices=[str(choice) for choice in raw.get("choices", [])],
        answer=str(raw["answer"]) if raw.get("answer") is not None else None,
        constraints=list(raw.get("constraints", [])),
        max_tokens=raw.get("max_tokens"),
        expected=str(raw["expected"]) if raw.get("expected") is not None else None,
        context=str(raw["context"]) if raw.get("context") is not None else None,
        answerable=bool(raw.get("answerable", True)),
        expected_facts=[str(fact) for fact in raw.get("expected_facts", [])],
        unknown=str(raw["unknown"]) if raw.get("unknown") is not None else None,
        target=str(raw["target"]) if raw.get("target") is not None else None,
        polarity=str(raw["polarity"]) if raw.get("polarity") is not None else None,
        condition=str(raw["condition"]) if raw.get("condition") is not None else None,
    )

    _ITEM_TYPES[item_type](item, where)
    return item


def _check_multiple_choice(item: Item, where: str) -> None:
    if len(item.choices) < 2:
        raise DatasetError(f"{where}: multiple_choice needs at least 2 choices")
    if not item.answer:
        raise DatasetError(f"{where}: multiple_choice needs 'answer'")
    letters = _letters(len(item.choices))
    if item.answer.strip().upper() not in letters:
        raise DatasetError(
            f"{where}: 'answer' is {item.answer!r}, "
            f"but with {len(item.choices)} choices it must be one of {''.join(letters)}"
        )


def _check_instruction_following(item: Item, where: str) -> None:
    if not item.constraints:
        raise DatasetError(f"{where}: instruction_following needs at least one constraint")


def _check_refusal(item: Item, where: str) -> None:
    if item.expected not in REFUSAL_EXPECTATIONS:
        raise DatasetError(
            f"{where}: refusal needs 'expected' set to one of "
            f"{', '.join(sorted(REFUSAL_EXPECTATIONS))}, received {item.expected!r}"
        )


def _check_grounded_qa(item: Item, where: str) -> None:
    if not item.context:
        raise DatasetError(f"{where}: grounded_qa needs 'context'")
    if item.answerable and not item.expected_facts:
        raise DatasetError(
            f"{where}: an answerable grounded_qa needs 'expected_facts' with what must "
            "appear in the response"
        )


register_item_type("multiple_choice", _check_multiple_choice)
register_item_type("instruction_following", _check_instruction_following)
register_item_type("refusal", _check_refusal)
register_item_type("grounded_qa", _check_grounded_qa)


def _check_bias_mc(item: Item, where: str) -> None:
    _check_multiple_choice(item, where)
    if not item.context:
        raise DatasetError(f"{where}: bias_mc needs 'context'")
    valid = _letters(len(item.choices))
    for name, value in (("unknown", item.unknown), ("target", item.target)):
        if value is None or value.strip().upper() not in valid:
            raise DatasetError(f"{where}: bias_mc needs '{name}' as one of {''.join(valid)}")
    if item.unknown == item.target:
        raise DatasetError(f"{where}: the stereotyped answer cannot be the 'unknown' option")
    if item.polarity not in ("neg", "nonneg"):
        raise DatasetError(f"{where}: bias_mc 'polarity' must be 'neg' or 'nonneg'")
    if item.condition not in ("ambig", "disambig"):
        raise DatasetError(f"{where}: bias_mc 'condition' must be 'ambig' or 'disambig'")


register_item_type("bias_mc", _check_bias_mc)


def _check_toxicity_prompt(item: Item, where: str) -> None:
    """An opening to continue: the prompt is all it needs, and it is checked already.

    There is no expected answer. What the model writes is scored by the
    toxicity classifier, so the item carries nothing a pattern could match.
    """


register_item_type("toxicity_prompt", _check_toxicity_prompt)


def _letters(count: int) -> list[str]:
    if count > 26:
        raise DatasetError(f"Too many choices to label with letters: {count}")
    return [chr(ord("A") + i) for i in range(count)]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DatasetError(f"The manifest does not exist: {path}") from None
    except json.JSONDecodeError as exc:
        raise DatasetError(f"Invalid JSON in {path}: {exc}") from None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise DatasetError(f"Invalid JSON in {path}:{number}: {exc}") from None
    return items
