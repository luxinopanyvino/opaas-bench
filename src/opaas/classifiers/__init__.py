"""Pinned classifiers for block D."""

from __future__ import annotations

from pathlib import Path

from opaas.classifiers.base import (
    PROFILES,
    Classifier,
    ClassifierError,
    ClassifierMetadata,
    ClassifierProfile,
    classifiers_available,
    describe_missing,
    resolve_profile,
    validate_model_dir,
)
from opaas.classifiers.cache import ensure_default_classifier, is_default_classifier_cached
from opaas.classifiers.registry import DEFAULT_CLASSIFIERS, ClassifierSource


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
    from opaas.classifiers.transformers_backend import SequenceClassifier

    return SequenceClassifier(
        Path(model_dir),
        profile,
        max_length=max_length,
        allow_remote_code=allow_remote_code,
    )


def available_profiles() -> list[str]:
    return sorted(PROFILES)


__all__ = [
    "DEFAULT_CLASSIFIERS",
    "PROFILES",
    "Classifier",
    "ClassifierError",
    "ClassifierMetadata",
    "ClassifierProfile",
    "ClassifierSource",
    "available_profiles",
    "classifiers_available",
    "describe_missing",
    "ensure_default_classifier",
    "is_default_classifier_cached",
    "load_classifier",
    "resolve_profile",
    "validate_model_dir",
]
