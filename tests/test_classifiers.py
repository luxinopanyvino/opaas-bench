from pathlib import Path
from types import SimpleNamespace

import pytest

from opaas.bench.classifiers import (
    ClassifierError,
    available_profiles,
    describe_missing,
    resolve_profile,
    validate_model_dir,
)
from opaas.bench.classifiers.base import ClassifierMetadata
from opaas.bench.classifiers.transformers_backend import (
    SequenceClassifier,
    _resolve_activation,
    _resolve_labels,
    _sigmoid,
    _softmax,
)


def _config(id2label):
    return SimpleNamespace(id2label=id2label, problem_type=None)


# ------------------------------------------------------------------- profiles


def test_profiles_cover_the_two_unlocked_dimensions():
    assert available_profiles() == ["hhem", "nli", "toxicity"]
    assert resolve_profile("nli").task == "faithfulness"
    assert resolve_profile("toxicity").task == "toxicity"


def test_unknown_profile_lists_the_available_ones():
    with pytest.raises(ClassifierError, match="toxicity"):
        resolve_profile("made-up")


def test_threshold_can_be_overridden_and_is_validated():
    assert resolve_profile("toxicity", 0.8).threshold == 0.8
    with pytest.raises(ClassifierError, match=r"\[0, 1\]"):
        resolve_profile("toxicity", 1.5)


def test_hhem_requires_remote_code_by_default():
    """Running third-party code to measure safety deserves an explicit decision."""
    assert resolve_profile("hhem").trust_remote_code is True
    assert resolve_profile("nli").trust_remote_code is False


# --------------------------------------------------------- weights validation


def test_a_missing_explicit_path_says_it_is_never_downloaded(tmp_path):
    """An explicit --toxicity-model / --faithfulness-model is used as-is: a typo
    must not silently fall back to fetching the pinned default."""
    with pytest.raises(ClassifierError, match="never downloaded"):
        validate_model_dir(tmp_path / "missing")


def test_a_file_instead_of_a_model_dir_is_rejected(tmp_path):
    path = tmp_path / "model.bin"
    path.write_bytes(b"x")
    with pytest.raises(ClassifierError, match="Expected a model directory"):
        validate_model_dir(path)


def test_a_directory_without_config_is_rejected(tmp_path):
    with pytest.raises(ClassifierError, match="config.json"):
        validate_model_dir(tmp_path)


def test_a_valid_model_dir_passes(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    validate_model_dir(tmp_path)  # does not raise


def test_remote_code_needs_an_explicit_opt_in(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ClassifierError, match="--allow-remote-code"):
        SequenceClassifier(tmp_path, resolve_profile("hhem"), allow_remote_code=False)


# --------------------------------------------------------------------- labels


def test_labels_are_resolved_case_insensitively():
    labels = _resolve_labels(_config({0: "ENTAILMENT", 1: "neutral", 2: "contradiction"}),
                             resolve_profile("nli"))
    assert labels == {"ENTAILMENT": 0}


def test_a_model_without_the_expected_labels_fails_loudly():
    with pytest.raises(ClassifierError, match="exposes none of the labels"):
        _resolve_labels(_config({0: "positive", 1: "negative"}), resolve_profile("nli"))


def test_a_single_output_model_needs_no_label_mapping():
    """HHEM returns a single score: there is no label to look for."""
    assert _resolve_labels(_config({0: "consistency"}), resolve_profile("hhem")) == {
        "consistency": 0
    }


def test_toxicity_labels_match_toxic_bert():
    labels = _resolve_labels(
        _config({0: "toxic", 1: "severe_toxic", 2: "obscene"}), resolve_profile("toxicity")
    )
    assert labels == {"toxic": 0}


# ------------------------------------------------------------------ activation


def test_a_two_class_head_uses_softmax_even_in_the_toxicity_profile():
    """Applying sigmoid to the 'toxic' logit of a binary head ignores the other
    logit and almost always returns low values: silently false figures."""
    activation, source = _resolve_activation(
        _config({0: "non-toxic", 1: "toxic"}), resolve_profile("toxicity")
    )
    assert activation == "softmax"
    assert "two-class head" in source


def test_a_multi_label_head_keeps_sigmoid():
    config = SimpleNamespace(
        id2label={0: "toxic", 1: "obscene", 2: "insult", 3: "threat", 4: "identity_hate"},
        problem_type=None,
    )
    activation, source = _resolve_activation(config, resolve_profile("toxicity"))
    assert activation == "sigmoid"
    assert "profile" in source


def test_the_model_declaration_wins_over_the_heuristic():
    config = SimpleNamespace(
        id2label={0: "a", 1: "b"}, problem_type="multi_label_classification"
    )
    activation, source = _resolve_activation(config, resolve_profile("toxicity"))
    assert activation == "sigmoid"
    assert source == "declared by the model"


def test_nli_three_class_head_stays_softmax():
    config = SimpleNamespace(
        id2label={0: "entailment", 1: "neutral", 2: "contradiction"}, problem_type=None
    )
    assert _resolve_activation(config, resolve_profile("nli"))[0] == "softmax"


def test_sigmoid_and_softmax():
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert _sigmoid(10.0) > 0.99
    probabilities = _softmax([1.0, 2.0, 3.0])
    assert sum(probabilities) == pytest.approx(1.0)
    assert probabilities[2] > probabilities[0]


def test_softmax_is_numerically_stable_with_large_logits():
    probabilities = _softmax([1000.0, 1001.0])
    assert sum(probabilities) == pytest.approx(1.0)


# ------------------------------------------------------------------- metadata


def test_metadata_publishes_the_weights_hash_and_threshold():
    metadata = ClassifierMetadata(
        name="nli",
        task="faithfulness",
        path="/models/nli",
        weights_hash={"hash": "sha256:abc", "fileCount": 2},
        threshold=0.6,
    )
    payload = metadata.as_dict()
    assert payload["weightsHash"] == "sha256:abc"
    assert payload["threshold"] == 0.6
    assert payload["task"] == "faithfulness"


def test_describe_missing_states_the_reason():
    missing = describe_missing("toxicity", "no weights")
    assert missing == {"task": "toxicity", "available": False, "reason": "no weights"}


def test_load_classifier_without_transformers_is_actionable(tmp_path, monkeypatch):
    """Without torch installed, the message says how to install it, not a raw ImportError."""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    import opaas.bench.classifiers.transformers_backend as backend

    def _boom():
        raise ClassifierError(
            "torch and transformers are missing. Install them with: pip install ..."
        )

    monkeypatch.setattr(backend, "_import_transformers", _boom)
    with pytest.raises(ClassifierError, match="pip install"):
        SequenceClassifier(Path(tmp_path), resolve_profile("nli"))
