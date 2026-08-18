"""Pinned default weights for the block D classifiers.

The same idea as `datasets/sources.py`, applied to model weights instead of
benchmark files: one entry per dimension, a **fixed commit revision** rather
than "main" or "latest". A certificate has to name exactly what measured it,
and a moving tag would make two runs on different days silently use different
weights under the identical command.

These are only ever fetched when the operator did not pass `--toxicity-model`
/ `--faithfulness-model` explicitly, and only if the `classifiers` extra is
installed — see `classifiers.cache.ensure_default_classifier` and
docs/decisions.md (D21).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClassifierSource:
    """One pinned model, downloadable from HuggingFace at a fixed revision."""

    task: str
    #: Matches a key in `classifiers.base.PROFILES`: the default is only used
    #: when the operator's chosen profile is the one this weight was picked for.
    profile: str
    repo_id: str
    #: Full commit SHA, not a branch. Resolved by hand against the HuggingFace
    #: API when this entry was pinned; bumping it is a deliberate, reviewed
    #: change, exactly like updating a dataset's sha256 in `sources.py`.
    revision: str
    license: str
    #: Only these are fetched: the two duplicate weight formats each repo also
    #: carries (`pytorch_model.bin`, `flax_model.msgpack`) are skipped, they
    #: are not needed alongside `.safetensors` and roughly double the download.
    allow_patterns: tuple[str, ...]
    description: str


#: One entry per block D classifier dimension.
DEFAULT_CLASSIFIERS: dict[str, ClassifierSource] = {
    "toxicity": ClassifierSource(
        task="toxicity",
        profile="toxicity",
        repo_id="unitary/toxic-bert",
        revision="4d6c22e74ba2fdd26bc4f7238f50766b045a0d94",
        license="Apache-2.0",
        allow_patterns=("*.json", "*.txt", "*.safetensors"),
        description=(
            "unitary/toxic-bert: a BERT head fine-tuned on six labels "
            "(toxic, severe_toxic, obscene, threat, insult, identity_hate). "
            "Only 'toxic' is used, matching the 'toxicity' profile."
        ),
    ),
    "faithfulness": ClassifierSource(
        task="faithfulness",
        profile="nli",
        repo_id="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
        revision="6f5cf0a2b59cabb106aca4c287eed12e357e90eb",
        license="MIT",
        allow_patterns=("*.json", "*.safetensors", "spm.model", "tokenizer.json"),
        description=(
            "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli: a DeBERTa-v3 NLI head. "
            "The passage is the premise and the response the hypothesis, matching "
            "the 'nli' profile."
        ),
    ),
}
