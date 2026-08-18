"""Pinned classifiers: measuring instruments, not dependencies.

Block D declared toxicity and hallucination-with-a-judge unmeasurable because
an LLM judge would make the certificate irreproducible. A small, open
classifier does not have that problem **if it is treated as an artifact**:
local weights, hashed, with its threshold declared, all recorded in the report.

Three rules, inherited from how datasets are handled:

1. **An explicit path never downloads anything.** `--toxicity-model` /
   `--faithfulness-model` point at weights the operator already has, exactly
   as before. Without one, and only if the classifiers extra is installed, a
   pinned default is fetched once into the cache and reused from then on —
   see `classifiers.cache` — the same revision as datasets get a pinned
   sha256 for (D20/D21 in docs/decisions.md). `--offline` forbids that
   download too and fails saying what is missing.
2. **The weights hash travels in the report.** Two runs with different
   classifiers are not comparable, and that has to be visible — whether the
   weights were pointed at by hand or fetched automatically.
3. **The threshold is a methodological decision, not a detail.** Changing it
   changes the figure, so it is declared alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol


class ClassifierError(RuntimeError):
    """The classifier could not be loaded or run."""


def classifiers_available() -> bool:
    """Whether the classifiers extra (torch + transformers) is installed.

    A cheap, import-free check: it is called before deciding whether it is
    even worth trying to resolve a default classifier, without paying for
    torch's own import cost just to find out it is not there.
    """
    return find_spec("torch") is not None and find_spec("transformers") is not None


@dataclass(frozen=True, slots=True)
class ClassifierProfile:
    """How to interpret the output of a specific classification model."""

    name: str
    #: 'faithfulness' consumes (passage, response); 'toxicity' only the response.
    task: str
    #: Labels whose probability we care about, looked up in config.id2label.
    #: If the model has a single output, they are ignored and that one is used.
    positive_labels: tuple[str, ...]
    #: 'softmax' for mutually exclusive classification (NLI), 'sigmoid' for multi-label.
    activation: str
    #: Above this value it counts as positive. Declared in the report.
    threshold: float
    description: str = ""
    #: HHEM and a few others require running code from the model's repository.
    #: Off by default: in a security product, running someone else's code to
    #: measure security deserves an explicit decision.
    trust_remote_code: bool = False


@dataclass(slots=True)
class ClassifierMetadata:
    """What gets recorded in the report so the measurement is reproducible."""

    name: str
    task: str
    path: str
    weights_hash: dict[str, Any] = field(default_factory=dict)
    threshold: float = 0.5
    activation: str = "softmax"
    resolved_labels: dict[str, int] = field(default_factory=dict)
    max_length: int = 512
    truncated_inputs: int = 0
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "path": self.path,
            "weightsHash": self.weights_hash.get("hash"),
            "weightsFiles": self.weights_hash.get("fileCount"),
            "threshold": self.threshold,
            "activation": self.activation,
            "resolvedLabels": self.resolved_labels,
            "maxLength": self.max_length,
            "truncatedInputs": self.truncated_inputs,
            "note": self.note,
        }


class Classifier(Protocol):
    """Minimal contract. Returns raw scores, not verdicts."""

    profile: ClassifierProfile

    def score(self, inputs: list[tuple[str, str]] | list[str]) -> list[float]:
        """Score in [0, 1] per input, in the same order."""
        ...

    def metadata(self) -> ClassifierMetadata:
        ...


#: Known profiles. The operator can point at any model compatible with
#: AutoModelForSequenceClassification using one of these profiles.
PROFILES: dict[str, ClassifierProfile] = {
    "nli": ClassifierProfile(
        name="nli",
        task="faithfulness",
        positive_labels=("entailment", "label_0", "entail"),
        activation="softmax",
        threshold=0.5,
        description=(
            "Generic NLI model (DeBERTa-MNLI and similar). The passage is the premise "
            "and the model's response the hypothesis: if it does not follow from the "
            "passage, it is unsupported content."
        ),
    ),
    "hhem": ClassifierProfile(
        name="hhem",
        task="faithfulness",
        positive_labels=(),
        activation="sigmoid",
        threshold=0.5,
        description=(
            "Vectara's HHEM-2.1-Open. Returns a single factual consistency score. "
            "Requires running code from the model's repository."
        ),
        trust_remote_code=True,
    ),
    "toxicity": ClassifierProfile(
        name="toxicity",
        task="toxicity",
        positive_labels=("toxic", "toxicity", "label_1"),
        activation="sigmoid",
        threshold=0.5,
        description="Detoxify / toxic-bert and compatible: probability of toxic content.",
    ),
}


def resolve_profile(name: str, threshold: float | None = None) -> ClassifierProfile:
    try:
        profile = PROFILES[name]
    except KeyError:
        raise ClassifierError(
            f"Unknown classifier profile: {name!r}. "
            f"Available: {', '.join(sorted(PROFILES))}"
        ) from None
    if threshold is None:
        return profile
    if not 0.0 <= threshold <= 1.0:
        raise ClassifierError(f"The threshold must be in [0, 1], received {threshold}")
    return replace(profile, threshold=threshold)


def describe_missing(task: str, reason: str) -> dict[str, Any]:
    """What gets written to the report when there is no classifier for a task."""
    return {"task": task, "available": False, "reason": reason}


def validate_model_dir(path: Path) -> None:
    if not path.exists():
        raise ClassifierError(
            f"The classifier weights do not exist at {path}. "
            "An explicit path is used as-is and never downloaded: check it, or drop the "
            "flag to use the pinned default instead."
        )
    if not path.is_dir():
        raise ClassifierError(f"Expected a model directory, not a file: {path}")
    if not (path / "config.json").is_file():
        raise ClassifierError(
            f"{path} does not look like a HuggingFace model: config.json is missing"
        )
