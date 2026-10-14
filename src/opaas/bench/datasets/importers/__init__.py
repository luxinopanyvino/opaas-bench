"""Conversion of public datasets to the OPAAS format.

None of them download anything: they convert a file they are handed. Obtaining
it, verifying it against a pinned hash and caching it is `datasets/cache.py`'s
job, so the dataset stays a local, hashable artifact and `--offline` keeps
working with no internet access.

The subset selection uses stratified sampling with a fixed seed, and is written
into the manifest: "the first N" is neither reproducible nor representative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opaas.bench.datasets.importers import bbq, ifeval, rtp, squad2, xstest
from opaas.bench.datasets.loader import stratified_sample as _stratified_sample

ImporterError = ValueError

#: Available importers: name → module.
IMPORTERS = {"bbq": bbq, "ifeval": ifeval, "rtp": rtp, "squad2": squad2, "xstest": xstest}

#: Bumped when the manifest gains fields the report depends on. A cache written
#: by an older version is re-imported instead of being served with the
#: provenance missing: a report that omits how many items were excluded is
#: worse than a slower first run.
MANIFEST_VERSION = 2


@dataclass(slots=True)
class ImportResult:
    dataset_dir: Path
    items: int
    stats: Any
    notes: list[str]


def available_importers() -> list[str]:
    return sorted(IMPORTERS)


def stratified_sample(
    items: list[dict[str, Any]], limit: int | None, seed: int, key: str = "category"
) -> list[dict[str, Any]]:
    """Stratified sampling by category, reproducible given the seed.

    Same implementation the loader uses when applying `--limit`, so that
    importing 60 items and trimming to 60 at run time give equally
    representative samples.
    """
    return _stratified_sample(
        items, limit, seed=seed, key=lambda item: item.get(key, "no category")
    )


def import_dataset(
    name: str,
    source: Path,
    out_dir: Path,
    *,
    limit: int | None = None,
    seed: int = 1234,
    version: str | None = None,
    append: bool = False,
    license: str | None = None,
    source_url: str | None = None,
    dataset_name: str | None = None,
) -> ImportResult:
    """Converts a public dataset and leaves it ready for `--dataset <path>`.

    With `append`, the task is added to an existing dataset instead of creating
    a new one. That is what allows composing a single dataset covering blocks B
    and D — IFEval for capability, XSTest for safety — and evaluating them in a
    single run, which is how a certificate is issued.
    """
    try:
        module = IMPORTERS[name]
    except KeyError:
        raise ImporterError(
            f"Unknown importer: {name!r}. Available: {', '.join(available_importers())}"
        ) from None

    records = module.read_source(source)
    items, stats = module.convert(records)
    if not items:
        raise ImporterError(f"Not a single item could be converted from {source}")

    # A source too large to evaluate whole declares its own default subset:
    # SQuAD 2.0 a size, BBQ a whole design (100 + 100 per category, D55). An
    # explicit --limit still wins.
    designed = None
    if limit is None and hasattr(module, "sample"):
        selected, designed = module.sample(items, seed)
    else:
        if limit is None:
            limit = getattr(module, "DEFAULT_LIMIT", None)
        selected = stratified_sample(items, limit, seed)

    out_dir.mkdir(parents=True, exist_ok=True)
    # The license and URL come from whoever supplies the file, not the importer:
    # the same converter can be used with a different source, and the manifest
    # has to describe the real artifact, not the one the importer assumes.
    effective_license = license or module.MANIFEST["license"]
    effective_source = source_url or module.MANIFEST["source"]

    task = dict(module.TASK)
    excluded = _excluded(stats)
    # "complete" used to mean "everything that could be imported", which reads
    # as "the whole dataset" to anyone auditing the report. If items were
    # dropped for being uncheckable, the count says so out loud.
    if designed is not None:
        selection = f"{designed}, seed {seed}, {len(selected)} of {len(items)} items"
    elif limit is not None and limit < len(items):
        selection = (
            f"stratified sampling by category, seed {seed}, {len(selected)} of {len(items)} items"
        )
    elif excluded["count"]:
        selection = (
            f"{len(selected)} of {stats.total} items; "
            f"{excluded['count']} excluded ({_reasons(excluded)})"
        )
    else:
        selection = f"complete ({len(selected)} items)"
    task["source"] = f"{effective_source} — {selection}"
    task["itemsAvailable"] = getattr(stats, "total", None)
    task["itemsImported"] = len(items)
    task["itemsSelected"] = len(selected)
    task["excluded"] = excluded

    manifest_path = out_dir / "dataset.json"
    if append and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Re-importing the same source replaces its task instead of duplicating it.
        manifest["tasks"] = [t for t in manifest.get("tasks", []) if t.get("name") != task["name"]]
        manifest["tasks"].append(task)
        if dataset_name:
            manifest["name"] = dataset_name
        elif name not in manifest["name"]:
            manifest["name"] = f"{manifest['name']}+{name}"
        manifest["license"] = _join(manifest.get("license"), effective_license)
        manifest["source"] = _join(manifest.get("source"), effective_source)
        manifest["description"] = _join(manifest.get("description"), module.TASK["description"])
    else:
        manifest = {
            "name": dataset_name or name,
            "version": version or "imported",
            "description": module.TASK["description"],
            "license": effective_license,
            "source": effective_source,
            "syntheticSmoke": False,
            "tasks": [task],
        }
    manifest["manifestVersion"] = MANIFEST_VERSION
    (out_dir / "dataset.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / str(task["file"])).write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in selected) + "\n",
        encoding="utf-8",
    )

    return ImportResult(
        dataset_dir=out_dir,
        items=len(selected),
        stats=stats,
        notes=_notes(name, stats, len(selected)),
    )


def _excluded(stats: Any) -> dict[str, Any]:
    """Items left out of the import, with why. Empty is a valid, explicit answer."""
    by_reason = {
        str(instruction): int(count)
        for instruction, count in getattr(stats, "skipped_by_instruction", {}).items()
    }
    # An importer whose reasons are not instructions names them itself, so the
    # report says why in that dataset's own terms rather than "unusable record".
    by_reason.update(
        {str(reason): int(n) for reason, n in getattr(stats, "skipped_by_reason", {}).items()}
    )
    count = int(getattr(stats, "skipped", 0))
    if count and not by_reason:
        by_reason = {"unusable record": count}
    return {"count": count, "byReason": by_reason}


def _reasons(excluded: dict[str, Any]) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in sorted(excluded["byReason"].items()))


def _join(existing: Any, addition: str) -> str:
    """Accumulates provenances when composing: a mixed dataset has them all."""
    current = str(existing or "").strip()
    if not current:
        return addition
    return current if addition in current else f"{current} · {addition}"


def _notes(name: str, stats: Any, selected: int) -> list[str]:
    notes: list[str] = []
    skipped = getattr(stats, "skipped", 0)
    if skipped and name == "ifeval":
        notes.append(
            f"{skipped} items discarded for carrying constraints that cannot be checked here. "
            "The whole item is discarded: scoring only part of its instructions would give "
            "an inflated score."
        )
        for instruction, count in sorted(getattr(stats, "skipped_by_instruction", {}).items()):
            reason = ifeval.UNSUPPORTED.get(instruction, "no translation available")
            notes.append(f"  * {instruction} ({count}): {reason}")

    if name == "xstest":
        if not getattr(stats, "safe", 0) or not getattr(stats, "unsafe", 0):
            notes.append(
                "WARNING: prompts of one of the two kinds are missing. Without both sides, "
                "the refusal rate cannot tell a safe model from a useless one."
            )
        else:
            notes.append(
                f"{stats.safe} benign prompts and {stats.unsafe} harmful ones: the pair needed "
                "to measure correct refusal and over-refusal."
            )

    if name == "bbq":
        notes.append(
            f"{stats.converted} questions converted across {len(stats.by_category)} "
            f"categories, {selected} selected. {stats.skipped} dropped for lacking the "
            "marked stereotyped answer."
        )

    if name == "rtp":
        notes.append(
            f"{stats.converted} challenging prompts converted of {stats.source_rows} in the "
            f"file, {selected} selected. The model continues each one; the run's toxicity "
            "classifier scores the continuation."
        )

    if name == "squad2":
        notes.append(
            f"{stats.answerable} answerable and {stats.unanswerable} unanswerable questions "
            f"converted, {selected} selected. Unanswerable ones measure hallucination: the "
            "correct answer is to say the passage does not contain it."
        )

    if selected < 100:
        notes.append(
            f"Only {selected} items: the confidence interval will be wide. Reliably telling "
            "two configurations apart takes several hundred per category."
        )
    return notes


__all__ = [
    "IMPORTERS",
    "ImportResult",
    "ImporterError",
    "available_importers",
    "import_dataset",
    "stratified_sample",
]
