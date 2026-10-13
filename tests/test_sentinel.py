import json
import random
from typing import Any

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.adapters.base import BackendError, GenerationResult
from opaas.bench.runner import RunRequest, execute
from opaas.cli.main import app
from opaas.core.stats import intervals_separate, median_interval
from opaas.sentinel.drift import (
    CANARY_PROMPTS,
    EXIT_CODES,
    SentinelError,
    append_watch_log,
    capture_baseline,
    check_canaries,
    check_fingerprint,
    check_latency,
    load_watch_log,
    watch,
)

from .conftest import FakeBackend
from .test_cli import PASSPHRASE, _flat, _sign_flow
from .test_runner import OPTIONS

runner = CliRunner()


@pytest.fixture(autouse=True)
def _register_fake():
    register_backend(FakeBackend)


def _loaded(backend_cls=FakeBackend, **kwargs):
    backend = backend_cls(**kwargs)
    backend.load("fake/model-1b", {})
    return backend


class ChangedOutputBackend(FakeBackend):
    """The same deployment after something underneath changed the text it produces."""

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        result = super().generate(prompt, **params)
        result.text = "a different answer"
        return result


class NoisyBackend(FakeBackend):
    """A deployment that is not deterministic at temperature 0."""

    def generate(self, prompt: str, **params: Any) -> GenerationResult:
        result = super().generate(prompt, **params)
        result.text = f"answer {random.random()}"
        return result


def _fingerprint(**fields):
    base = {
        "engine": "Fake Engine",
        "engineVersion": "1.0",
        "model": "fake/model-1b",
        "modelDigest": None,
        "chatTemplateHash": None,
        "gpus": [],
        "gpuDrivers": [],
        "weightsHash": None,
        "tokenizerFilesHash": None,
    }
    return {"fields": {**base, **fields}, "unknown": {}, "hash": "sha256:x"}


# ------------------------------------------------------------------ statistics


def test_the_median_interval_at_twenty_samples_runs_from_the_sixth_to_the_fifteenth():
    interval = median_interval(range(1, 21))
    assert (interval["low"], interval["high"]) == (6, 15)


def test_too_few_samples_give_an_unqualified_median():
    interval = median_interval([1.0, 2.0, 3.0, 4.0, 5.0])
    assert interval["point"] == 3.0
    assert interval["low"] is None


def test_overlapping_intervals_are_not_a_difference():
    assert intervals_separate({"low": 1, "high": 3}, {"low": 2, "high": 4}) is False
    assert intervals_separate({"low": 1, "high": 2}, {"low": 3, "high": 4}) is True
    assert intervals_separate({"low": None, "high": 2}, {"low": 3, "high": 4}) is None


# -------------------------------------------------------------------- baseline


def test_the_baseline_records_canaries_and_a_latency_interval():
    baseline = capture_baseline(_loaded())
    assert baseline["deterministic"] is True
    assert len(baseline["canaries"]) == len(CANARY_PROMPTS) == 20
    assert baseline["latency"]["medianS"]["low"] is not None


def test_a_deployment_that_disagrees_with_itself_cannot_use_canaries():
    """Exact-output comparison on a non-deterministic stack would flag noise as drift."""
    baseline = capture_baseline(_loaded(NoisyBackend))
    assert baseline["deterministic"] is False
    assert check_canaries(_loaded(NoisyBackend), baseline)["status"] == "disabled"


def test_every_run_signs_a_baseline_and_a_cloud_baseline_does_not(tmp_path):
    report = json.loads(
        execute(
            RunRequest(model="fake/model-1b", backend="fake", suites=["performance"],
                       suite_options=OPTIONS, output=tmp_path / "a" / "report.json")
        ).report_path.read_text(encoding="utf-8")
    )
    assert report["sentinelBaseline"]["available"] is True

    cloud = json.loads(
        execute(
            RunRequest(model="fake/model-1b", backend="fake", suites=["consistency"],
                       suite_options={"consistency": {"context_probe_words": [16]}},
                       output=tmp_path / "b" / "report.json", reference_type="cloud-baseline")
        ).report_path.read_text(encoding="utf-8")
    )
    assert cloud["sentinelBaseline"]["available"] is False


# ---------------------------------------------------------------------- checks


def test_a_changed_fingerprint_field_is_drift():
    result = check_fingerprint(_fingerprint(engineVersion="1.0"), _fingerprint(engineVersion="1.1"))
    assert result["status"] == "drifted"
    assert result["changed"] == [{"field": "engineVersion", "certified": "1.0", "current": "1.1"}]


def test_only_a_matching_model_build_lets_the_fingerprint_carry_current():
    same_digest = _fingerprint(modelDigest="sha256:a")
    assert check_fingerprint(same_digest, same_digest)["status"] == "current"
    # Same engine and GPU, nothing identifying the weights: not enough.
    assert check_fingerprint(_fingerprint(), _fingerprint())["status"] == "inconclusive"


def test_a_field_unknown_on_either_side_is_not_comparable_never_a_match():
    result = check_fingerprint(_fingerprint(modelDigest="sha256:a"), _fingerprint())
    assert "modelDigest" in [entry["field"] for entry in result["notComparable"]]
    assert "modelDigest" not in result["matched"]


def test_a_report_without_a_fingerprint_is_inconclusive():
    assert check_fingerprint({}, _fingerprint())["status"] == "inconclusive"


def test_different_canary_text_is_drift():
    baseline = capture_baseline(_loaded())
    assert check_canaries(_loaded(), baseline)["status"] == "current"
    drifted = check_canaries(_loaded(ChangedOutputBackend), baseline)
    assert drifted["status"] == "drifted"
    assert len(drifted["differing"]) == 20


def test_a_slower_deployment_drifts_on_latency():
    baseline = capture_baseline(_loaded())
    assert check_latency(_loaded(), baseline)["status"] == "current"
    assert check_latency(_loaded(latency_s=0.02), baseline)["status"] == "drifted"


# ----------------------------------------------------------------------- watch


def _report(fingerprint, baseline):
    return {"deploymentFingerprint": fingerprint, "sentinelBaseline": baseline, "suites": []}


def test_a_changed_fingerprint_ends_the_watch_before_any_inference():
    backend = _loaded()
    report = _report(_fingerprint(engineVersion="1.0"), capture_baseline(_loaded()))
    calls_before = backend.calls
    result = watch(report, backend, _fingerprint(engineVersion="2.0"))
    assert result["state"] == "drifted"
    assert list(result["checks"]) == ["fingerprint"]
    assert backend.calls == calls_before


def test_decisive_checks_skip_the_statistical_ones_unless_asked():
    report = _report(_fingerprint(modelDigest="sha256:a"), capture_baseline(_loaded()))
    current = _fingerprint(modelDigest="sha256:a")
    quick = watch(report, _loaded(), current)
    assert quick["state"] == "current"
    assert quick["carriedBy"] == ["fingerprint", "canaries"]
    assert "latency" not in quick["checks"]

    full = watch(report, _loaded(), current, full=True)
    assert {"latency", "egress", "behaviour"} <= set(full["checks"])


def test_when_nothing_identifies_the_build_the_statistics_run_on_their_own():
    report = _report(_fingerprint(), capture_baseline(_loaded(NoisyBackend)))
    result = watch(report, _loaded(NoisyBackend), _fingerprint())
    assert result["checks"]["canaries"]["status"] == "disabled"
    assert "latency" in result["checks"]


def test_exit_codes_tell_a_changed_deployment_from_a_check_that_could_not_run():
    assert EXIT_CODES == {"current": 0, "drifted": 1, "expired": 1, "inconclusive": 3}


# ------------------------------------------------------------------------- log


def test_the_watch_log_is_a_chain_that_detects_edits(tmp_path):
    log = tmp_path / "watch.jsonl"
    for state in ("current", "drifted"):
        append_watch_log(log, {"state": state, "checkedAt": "2026-10-07T00:00:00Z",
                               "credentialId": "urn:x"})
    assert [entry.state for entry in load_watch_log(log)] == ["current", "drifted"]

    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(lines[0].replace('"current"', '"drifted"') + "\n" + lines[1] + "\n",
                   encoding="utf-8")
    with pytest.raises(SentinelError, match="altered"):
        append_watch_log(log, {"state": "current", "checkedAt": "x", "credentialId": "urn:x"})


# ------------------------------------------------------------------------- cli


def _watch(tmp_path, report, credential, keys, *extra):
    return runner.invoke(
        app,
        ["watch", "--report", str(report), "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem"),
         "--out", str(tmp_path / "watch.json"), *extra],
    )


def test_an_unchanged_deployment_is_current(tmp_path):
    report, credential, keys = _sign_flow(tmp_path)
    result = _watch(tmp_path, report, credential, keys, "--log", str(tmp_path / "w.jsonl"))
    assert result.exit_code == 0, result.output
    written = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
    assert written["state"] == "current"
    assert "canaries" in written["carriedBy"]
    assert len(load_watch_log(tmp_path / "w.jsonl")) == 1


def test_a_deployment_producing_different_text_has_drifted(tmp_path, monkeypatch):
    report, credential, keys = _sign_flow(tmp_path)
    original = FakeBackend.generate

    def changed(self, prompt, **params):
        result = original(self, prompt, **params)
        result.text = "a different answer"
        return result

    monkeypatch.setattr(FakeBackend, "generate", changed)
    result = _watch(tmp_path, report, credential, keys)
    assert result.exit_code == 1, result.output
    assert "DRIFTED" in result.output


def test_an_unreachable_endpoint_is_inconclusive_not_drift(tmp_path, monkeypatch):
    report, credential, keys = _sign_flow(tmp_path)

    def refuse(self, model_id, config):
        raise BackendError("connection refused")

    monkeypatch.setattr(FakeBackend, "load", refuse)
    result = _watch(tmp_path, report, credential, keys)
    assert result.exit_code == 3, result.output
    assert json.loads((tmp_path / "watch.json").read_text())["state"] == "inconclusive"


def test_an_expired_certificate_is_reported_without_checking_anything(tmp_path):
    report, _, keys = _sign_flow(tmp_path)
    expired = tmp_path / "expired.json"
    signed = runner.invoke(
        app,
        ["sign", "--input", str(report), "--key", str(keys / "opaas-root-ed25519.pem"),
         "--passphrase", PASSPHRASE, "--out", str(expired), "--validity-days", "0"],
    )
    assert signed.exit_code == 0, signed.output
    result = _watch(tmp_path, report, expired, keys)
    assert result.exit_code == 1, result.output
    written = json.loads((tmp_path / "watch.json").read_text())
    assert written["state"] == "expired"
    assert written["checks"] == {}


def test_a_certificate_that_does_not_verify_cannot_be_watched(tmp_path):
    report, credential, keys = _sign_flow(tmp_path)
    document = json.loads(credential.read_text(encoding="utf-8"))
    document["credentialSubject"]["configuration"]["hardware"] = "8x H100"
    credential.write_text(json.dumps(document), encoding="utf-8")
    result = _watch(tmp_path, report, credential, keys)
    assert result.exit_code == 2
    assert "nothing to watch against" in _flat(result.output)
    assert not (tmp_path / "watch.json").exists()

