"""Cache for the pinned default classifiers: downloaded once, reused always.

Mirrors `datasets/cache.py` — see D20 in docs/decisions.md, and D21 for why
the same reasoning now also applies to classifier weights:

    ~/.opaas-bench/cache/classifiers/<task>/    the pinned model, at its revision

The second run does not touch the network: the directory carries a marker
recording which repo and revision produced it, and it is trusted without
re-downloading as long as both match the pinned `ClassifierSource`. `--offline`
forbids the network explicitly and fails saying what is missing, exactly like
the datasets.

This is only reached when the operator did not pass `--toxicity-model` /
`--faithfulness-model` explicitly: an explicit path is used as-is and never
downloads anything, on this path or any other.
"""

from __future__ import annotations

import json
from pathlib import Path

from opaas_bench.classifiers.base import ClassifierError
from opaas_bench.classifiers.registry import DEFAULT_CLASSIFIERS, ClassifierSource
from opaas_bench.datasets.cache import cache_dir

_MARKER = "opaas-source.json"


def classifier_dir(task: str) -> Path:
    return cache_dir() / "classifiers" / task


def _marker_matches(path: Path, source: ClassifierSource) -> bool:
    marker = path / _MARKER
    if not marker.is_file():
        return False
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    return recorded.get("repoId") == source.repo_id and recorded.get("revision") == source.revision


def is_default_classifier_cached(task: str) -> bool:
    source = DEFAULT_CLASSIFIERS.get(task)
    return source is not None and _marker_matches(classifier_dir(task), source)


def ensure_default_classifier(task: str, *, offline: bool = False) -> Path:
    """Returns the local directory of the pinned default classifier for `task`,
    downloading it at its pinned revision if it is not already there.

    Raises `ClassifierError` — the same exception `--toxicity-model` /
    `--faithfulness-model` failures raise — so callers do not need a second
    error type to handle.
    """
    source = DEFAULT_CLASSIFIERS.get(task)
    if source is None:
        raise ClassifierError(f"No default classifier is pinned for task {task!r}.")

    destination = classifier_dir(task)
    if _marker_matches(destination, source):
        return destination

    if offline:
        raise ClassifierError(
            f"No '{task}' classifier cached at {destination} and offline mode was "
            "requested.\n"
            f"  Populate it on a connected machine: opaas-bench fetch-classifiers {task}\n"
            f"  and copy {cache_dir()} to this machine, or point --{task}-model at "
            "weights you already have."
        )

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ClassifierError(
            "huggingface-hub is missing. It installs with the classifiers extra: "
            "pip install 'opaas-bench[classifiers]'"
        ) from exc

    destination.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=source.repo_id,
            revision=source.revision,
            local_dir=destination,
            allow_patterns=list(source.allow_patterns),
        )
    except Exception as exc:  # noqa: BLE001 - huggingface_hub raises assorted errors
        raise ClassifierError(
            f"Could not download the default '{task}' classifier "
            f"({source.repo_id}@{source.revision[:12]}): {exc}\n"
            f"  Download it yourself and pass --{task}-model <path>, or check your "
            "network and retry."
        ) from exc

    (destination / _MARKER).write_text(
        json.dumps({"repoId": source.repo_id, "revision": source.revision}, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
