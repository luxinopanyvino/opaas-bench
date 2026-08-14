"""Pinned classifiers for block D."""

from __future__ import annotations

from pathlib import Path

from opaas_bench.classifiers.base import (
    PROFILES,
    Classifier,
    ClassifierError,
    ClassifierMetadata,
    ClassifierProfile,
    describe_missing,
    resolve_profile,
    validate_model_dir,
)


def load_classifier(
    model_dir: Path,
    profile_name: str,
    *,
    threshold: float | None = None,
    max_length: int = 512,
    allow_remote_code: bool = False,
) -> Classifier:
    """Loads a local classifier. Imports transformers only when needed."""
    profile = resolve_profile(profile_name, threshold)
    from opaas_bench.classifiers.transformers_backend import SequenceClassifier

    return SequenceClassifier(
        Path(model_dir),
        profile,
        max_length=max_length,
        allow_remote_code=allow_remote_code,
    )


def available_profiles() -> list[str]:
    return sorted(PROFILES)


__all__ = [
    "PROFILES",
    "Classifier",
    "ClassifierError",
    "ClassifierMetadata",
    "ClassifierProfile",
    "available_profiles",
    "describe_missing",
    "load_classifier",
    "resolve_profile",
    "validate_model_dir",
]
