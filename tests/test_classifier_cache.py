"""The pinned default classifiers: downloaded once, reused, offline-aware.

Mirrors test_cache.py's coverage of dataset downloads, but for classifier
weights: same "second call must not touch the network" guarantee, same
--offline contract, same "a corrupt/stale cache is not trusted silently".
"""

from __future__ import annotations

import json
import sys

import pytest

from opaas_bench.classifiers.base import PROFILES, ClassifierError
from opaas_bench.classifiers.cache import (
    classifier_dir,
    ensure_default_classifier,
    is_default_classifier_cached,
)
from opaas_bench.classifiers.registry import DEFAULT_CLASSIFIERS


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPAAS_CACHE_DIR", str(tmp_path / "cache"))


def _fake_snapshot_download(monkeypatch, *, calls=None, fail_with=None):
    """Installs a fake `huggingface_hub` in sys.modules: no real network, no
    real torch/transformers required to exercise the download bookkeeping."""

    def snapshot_download(*, repo_id, revision, local_dir, allow_patterns):
        if calls is not None:
            calls.append((repo_id, revision))
        if fail_with is not None:
            raise fail_with
        (local_dir).mkdir(parents=True, exist_ok=True)
        (local_dir / "config.json").write_text("{}", encoding="utf-8")
        return str(local_dir)

    fake_module = type(sys)("huggingface_hub")
    fake_module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)


# --------------------------------------------------------------------- registry


def test_every_default_profile_is_a_registered_one():
    """A typo here would resolve to the wrong activation/threshold silently."""
    for source in DEFAULT_CLASSIFIERS.values():
        assert source.profile in PROFILES


def test_every_default_pins_a_full_commit_sha():
    """A branch name ('main') would make two runs on different days use
    different weights under the identical command."""
    for source in DEFAULT_CLASSIFIERS.values():
        assert len(source.revision) == 40
        int(source.revision, 16)  # hex digits only


# ------------------------------------------------------------------------ cache


def test_classifier_dir_lives_under_the_shared_cache_root():
    from opaas_bench.datasets.cache import cache_dir

    assert classifier_dir("toxicity") == cache_dir() / "classifiers" / "toxicity"


def test_uncached_by_default():
    assert is_default_classifier_cached("toxicity") is False


def test_unknown_task_is_rejected():
    with pytest.raises(ClassifierError, match="No default classifier is pinned"):
        ensure_default_classifier("made-up-dimension")


def test_downloads_and_marks_itself_cached(monkeypatch):
    calls: list = []
    _fake_snapshot_download(monkeypatch, calls=calls)

    path = ensure_default_classifier("toxicity")

    assert path == classifier_dir("toxicity")
    assert (path / "config.json").is_file()
    assert is_default_classifier_cached("toxicity") is True
    assert len(calls) == 1


def test_the_second_call_does_not_touch_the_network(monkeypatch):
    calls: list = []
    _fake_snapshot_download(monkeypatch, calls=calls)

    ensure_default_classifier("toxicity")
    ensure_default_classifier("toxicity")

    assert len(calls) == 1, "the second call must not download again"


def test_a_marker_for_a_different_revision_triggers_a_redownload(monkeypatch):
    """Bumping the pinned revision in the registry must not silently keep
    serving the old weights from a stale cache directory."""
    calls: list = []
    _fake_snapshot_download(monkeypatch, calls=calls)
    destination = classifier_dir("toxicity")
    destination.mkdir(parents=True)
    (destination / "opaas-source.json").write_text(
        json.dumps({"repoId": "unitary/toxic-bert", "revision": "0" * 40}), encoding="utf-8"
    )

    assert is_default_classifier_cached("toxicity") is False
    ensure_default_classifier("toxicity")

    assert len(calls) == 1
    recorded = json.loads((destination / "opaas-source.json").read_text(encoding="utf-8"))
    assert recorded["revision"] == DEFAULT_CLASSIFIERS["toxicity"].revision


def test_offline_refuses_to_download_and_says_what_to_do(monkeypatch):
    _fake_snapshot_download(monkeypatch)
    with pytest.raises(ClassifierError) as exc:
        ensure_default_classifier("toxicity", offline=True)
    message = str(exc.value)
    assert "offline mode" in message
    assert "--toxicity-model" in message


def test_offline_works_once_the_cache_is_populated(monkeypatch):
    _fake_snapshot_download(monkeypatch)
    ensure_default_classifier("toxicity")

    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # any use now explodes
    assert ensure_default_classifier("toxicity", offline=True) == classifier_dir("toxicity")


def test_missing_huggingface_hub_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(ClassifierError, match=r"pip install 'opaas-bench\[classifiers\]'"):
        ensure_default_classifier("toxicity")


def test_a_download_failure_is_wrapped_with_actionable_advice(monkeypatch):
    _fake_snapshot_download(monkeypatch, fail_with=RuntimeError("connection reset"))
    with pytest.raises(ClassifierError) as exc:
        ensure_default_classifier("toxicity")
    message = str(exc.value)
    assert "unitary/toxic-bert" in message
    assert "--toxicity-model" in message
    assert "connection reset" in message
