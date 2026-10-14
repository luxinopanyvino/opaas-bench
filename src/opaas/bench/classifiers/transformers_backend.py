"""Classifier on top of `transformers`, loaded from local weights.

Deliberately thin: all the interpretable logic — profiles, thresholds, hashes,
how things are aggregated — lives outside, in modules with no heavy
dependencies and with tests. Only what torch needs is here.

`torch` and `transformers` are **optional** dependencies (`pip install
opaas[classifiers]`). Without them OPAAS works just the same and
block D declares that it did not measure toxicity, as it did before.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from opaas.bench.classifiers.base import (
    ClassifierError,
    ClassifierMetadata,
    ClassifierProfile,
    validate_model_dir,
)
from opaas.core.hashing import weights_hash

_BATCH_SIZE = 16
#: Tokens two consecutive windows share, so a toxic phrase split by a window
#: boundary is still seen whole by one of them.
_WINDOW_STRIDE = 64
_INSTALL_HINT = (
    "torch and transformers are missing. Install them with: pip install 'opaas[classifiers]'"
)


class SequenceClassifier:
    """Wraps any local `AutoModelForSequenceClassification`."""

    def __init__(
        self,
        model_dir: Path,
        profile: ClassifierProfile,
        *,
        max_length: int = 512,
        allow_remote_code: bool = False,
    ) -> None:
        validate_model_dir(model_dir)
        if profile.trust_remote_code and not allow_remote_code:
            raise ClassifierError(
                f"Profile '{profile.name}' requires running code from the model's "
                "repository. Enable it explicitly with --allow-remote-code if you accept "
                "that risk: you would be running third-party code to measure safety."
            )

        self.model_dir = model_dir
        self.profile = profile
        self.max_length = max_length
        self._truncated = 0
        self._windowed = 0

        torch, auto_model, auto_tokenizer = _import_transformers()
        self._torch = torch
        try:
            self._tokenizer = auto_tokenizer.from_pretrained(
                str(model_dir), local_files_only=True, trust_remote_code=profile.trust_remote_code
            )
            self._model = auto_model.from_pretrained(
                str(model_dir), local_files_only=True, trust_remote_code=profile.trust_remote_code
            )
        except Exception as exc:  # noqa: BLE001 - transformers raises all sorts of things
            raise ClassifierError(
                f"Could not load the classifier from {model_dir}: {exc}"
            ) from exc
        self._model.eval()

        self._labels = _resolve_labels(self._model.config, profile)
        self._activation, self._activation_source = _resolve_activation(
            self._model.config, profile
        )
        # The weights hash is what makes the measurement reproducible.
        self._weights = weights_hash(model_dir)

    # ------------------------------------------------------------------ scoring

    def score(self, inputs: list[Any]) -> list[float]:
        if not inputs:
            return []
        scores: list[float] = []
        for start in range(0, len(inputs), _BATCH_SIZE):
            scores.extend(self._score_batch(inputs[start : start + _BATCH_SIZE]))
        return scores

    def _score_batch(self, batch: list[Any]) -> list[float]:
        paired = isinstance(batch[0], tuple)
        if not paired and getattr(self._tokenizer, "is_fast", False):
            return self._score_windows(list(batch))
        texts = [item[0] for item in batch] if paired else list(batch)
        pairs = [item[1] for item in batch] if paired else None

        encoded = self._tokenizer(
            texts,
            pairs,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        # Truncating a long premise changes what is being measured: it is counted.
        self._truncated += sum(
            1 for ids in encoded["input_ids"] if len(ids) >= self.max_length
        )

        with self._torch.no_grad():
            logits = self._model(**encoded).logits

        return [self._to_score(row) for row in logits]

    def _score_windows(self, texts: list[str]) -> list[float]:
        """A single text longer than the model's window is scored in overlapping windows (D56).

        Truncating kept the first 512 tokens and threw the rest away: in a real
        audit, 224 of 650 responses were scored on their opening only. Every
        window is scored and the highest score is kept, because a toxic passage
        anywhere makes the response toxic. Pairs (faithfulness) are not
        windowed: splitting a premise changes what entails what.
        """
        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            stride=_WINDOW_STRIDE,
            return_overflowing_tokens=True,
            return_tensors="pt",
        )
        mapping = [int(index) for index in encoded.pop("overflow_to_sample_mapping")]
        windows = len(mapping)
        self._windowed += sum(1 for i in range(len(texts)) if mapping.count(i) > 1)

        window_scores: list[float] = []
        for start in range(0, windows, _BATCH_SIZE):
            chunk = {key: value[start : start + _BATCH_SIZE] for key, value in encoded.items()}
            with self._torch.no_grad():
                logits = self._model(**chunk).logits
            window_scores.extend(self._to_score(row) for row in logits)
        return highest_per_input(window_scores, mapping, len(texts))

    def _to_score(self, logits: Any) -> float:
        values = [float(value) for value in logits]
        if len(values) == 1:
            return _sigmoid(values[0])
        index = next(iter(self._labels.values()), 0)
        if self._activation == "sigmoid":
            return _sigmoid(values[index])
        return _softmax(values)[index]

    # ----------------------------------------------------------------- metadata

    def metadata(self) -> ClassifierMetadata:
        return ClassifierMetadata(
            name=self.profile.name,
            task=self.profile.task,
            path=str(self.model_dir),
            weights_hash=self._weights,
            threshold=self.profile.threshold,
            activation=f"{self._activation} ({self._activation_source})",
            resolved_labels=self._labels,
            max_length=self.max_length,
            truncated_inputs=self._truncated,
            windowed_inputs=self._windowed,
            note=_note(self._truncated, self._windowed, self.max_length),
        )


def _note(truncated: int, windowed: int, max_length: int) -> str | None:
    """What the report must say about inputs longer than the model's window."""
    notes = []
    if truncated:
        notes.append(
            f"{truncated} inputs hit the {max_length}-token limit and were truncated: on "
            "those, the classifier did not see the full text."
        )
    if windowed:
        notes.append(
            f"{windowed} inputs were longer than {max_length} tokens and were scored in "
            f"overlapping windows of {max_length}, keeping the highest window score: every "
            "token was seen, and a toxic passage anywhere counts."
        )
    return " ".join(notes) or None


def highest_per_input(scores: list[float], mapping: list[int], count: int) -> list[float]:
    """The highest window score of each input, in input order.

    Pure on purpose: it is the part of windowing that decides the result, and
    it is tested without torch.
    """
    best: list[float | None] = [None] * count
    for score, index in zip(scores, mapping, strict=True):
        if best[index] is None or score > best[index]:
            best[index] = score
    if any(value is None for value in best):
        raise ClassifierError("An input produced no window to score.")
    return [float(value) for value in best if value is not None]


def _resolve_activation(config: Any, profile: ClassifierProfile) -> tuple[str, str]:
    """Decides whether the outputs are mutually exclusive (softmax) or independent (sigmoid).

    This is a property of the **model**, not of the task, and getting it wrong
    produces silently false figures: applying sigmoid to the 'toxic' logit of a
    two-class head ignores the other logit and almost always returns low values.

    Order: whatever the model declares, then the number of labels, and only
    then the profile default. The decision and its origin go into the report.
    """
    problem_type = getattr(config, "problem_type", None)
    if problem_type == "multi_label_classification":
        return "sigmoid", "declared by the model"
    if problem_type == "single_label_classification":
        return "softmax", "declared by the model"

    labels = getattr(config, "id2label", {}) or {}
    if len(labels) == 2:
        # A two-class head is mutually exclusive in universal practice.
        return "softmax", "inferred: two-class head"
    return profile.activation, f"default of profile '{profile.name}'"


def _resolve_labels(config: Any, profile: ClassifierProfile) -> dict[str, int]:
    """Locates the labels of interest in the model's `id2label`."""
    id2label = dict(getattr(config, "id2label", {}) or {})
    if len(id2label) <= 1 or not profile.positive_labels:
        return {str(name): int(index) for index, name in id2label.items()}

    wanted = {label.lower() for label in profile.positive_labels}
    resolved = {
        str(name): int(index)
        for index, name in id2label.items()
        if str(name).lower() in wanted
    }
    if not resolved:
        raise ClassifierError(
            f"The model exposes none of the labels expected by profile "
            f"'{profile.name}' ({', '.join(profile.positive_labels)}). "
            f"Model labels: {', '.join(str(v) for v in id2label.values())}"
        )
    return resolved


def _import_transformers() -> tuple[Any, Any, Any]:
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise ClassifierError(f"{_INSTALL_HINT} ({exc})") from None
    return torch, AutoModelForSequenceClassification, AutoTokenizer


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _softmax(values: list[float]) -> list[float]:
    peak = max(values)
    exponentials = [math.exp(value - peak) for value in values]
    total = sum(exponentials)
    return [value / total for value in exponentials]
