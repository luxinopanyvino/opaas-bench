import json
import subprocess
from typing import Any, ClassVar

import httpx
import pytest

from opaas.adapters import register_backend
from opaas.canonical import canonicalize
from opaas.fingerprint import deployment_fingerprint
from opaas.hashing import file_digest
from opaas.runner import RunRequest, execute
from opaas.telemetry import gpu as gpu_module
from opaas.web import build_site, load_entry

from .conftest import FakeBackend
from .test_ollama_adapter import MODEL, SHOW, TAGS, _handler, _load
from .test_runner import OPTIONS
from .test_web import DID, _credential, _report

GPUS = {"gpus": [{"name": "NVIDIA RTX 4090", "vramTotalMb": 24564, "driverVersion": "560.35"}]}


class DigestBackend(FakeBackend):
    """A deployment that publishes its model digest and chat template."""

    name: ClassVar[str] = "fake-digest"
    digest: ClassVar[str] = "sha256:aaaa"

    def model_digest(self) -> str | None:
        return self.digest

    def chat_template_hash(self) -> str | None:
        return "sha256:template"

    def get_backend_metadata(self) -> dict[str, Any]:
        return {**super().get_backend_metadata(), "engineVersion": "1.2.3"}


class RepulledBackend(DigestBackend):
    """The same model id re-pulled onto different weights."""

    name: ClassVar[str] = "fake-repulled"
    digest: ClassVar[str] = "sha256:bbbb"


def _fingerprint(backend_cls=DigestBackend, environment=GPUS, **metadata):
    backend = backend_cls()
    backend.load("fake/model-1b", {})
    return deployment_fingerprint(
        backend, {**backend.get_backend_metadata(), **metadata}, environment
    )


# ---------------------------------------------------------------- the record


def test_the_fingerprint_records_what_is_serving():
    fields = _fingerprint()["fields"]
    assert fields == {
        "engine": "Fake Engine",
        "engineVersion": "1.2.3",
        "model": "fake/model-1b",
        "modelDigest": "sha256:aaaa",
        "chatTemplateHash": "sha256:template",
        "gpus": ["NVIDIA RTX 4090"],
        "gpuDrivers": ["560.35"],
    }


def test_a_repulled_model_changes_the_fingerprint():
    assert _fingerprint()["hash"] != _fingerprint(RepulledBackend)["hash"]


def test_an_upgraded_driver_changes_the_fingerprint():
    upgraded = {"gpus": [{**GPUS["gpus"][0], "driverVersion": "565.57"}]}
    assert _fingerprint()["hash"] != _fingerprint(environment=upgraded)["hash"]


def test_what_the_operator_declared_is_not_evidence_of_what_is_serving():
    """A declared quantization or a prompt mode OPAAS chose is not an observation (D14)."""
    plain = _fingerprint()
    declared = _fingerprint(quantization="GGUF Q8_0", promptMode="raw", numCtx=8192)
    assert plain == declared
    assert "quantization" not in plain["fields"]


def test_unknown_fields_are_listed_with_their_reason_never_guessed():
    fingerprint = _fingerprint(FakeBackend, environment={"gpuTelemetryNote": "nvidia-smi absent"})
    unknown = fingerprint["unknown"]
    assert fingerprint["fields"]["modelDigest"] is None
    assert "re-pulled" in unknown["modelDigest"]
    assert "chatTemplateHash" in unknown
    assert "engineVersion" in unknown
    assert unknown["gpus"] == unknown["gpuDrivers"] == "nvidia-smi absent"


# -------------------------------------------------------------- run and credential


@pytest.fixture(autouse=True)
def _digest_backend():
    register_backend(DigestBackend)


def _run(tmp_path, name):
    request = RunRequest(
        model="fake/model-1b",
        backend="fake-digest",
        suites=["performance"],
        suite_options=OPTIONS,
        output=tmp_path / name / "report.json",
    )
    return json.loads(execute(request).report_path.read_text(encoding="utf-8"))


def test_the_config_hash_cannot_see_a_repulled_model_and_the_fingerprint_can(
    tmp_path, monkeypatch
):
    """The reason the fingerprint exists: configHash describes how OPAAS ran."""
    before = _run(tmp_path, "before")
    monkeypatch.setattr(DigestBackend, "digest", "sha256:bbbb")
    after = _run(tmp_path, "after")

    assert before["configuration"]["configHash"] == after["configuration"]["configHash"]
    assert after["deploymentFingerprint"]["fields"]["modelDigest"] == "sha256:bbbb"
    assert before["deploymentFingerprint"]["hash"] != after["deploymentFingerprint"]["hash"]


def test_the_credential_carries_the_deployment_hash(tmp_path):
    from opaas.credential import build_credential

    report = _run(tmp_path, "run")
    credential = build_credential(report, "sha256:x", issuer="did:web:x", key_id="did:web:x#1")
    assert (
        credential["credentialSubject"]["configuration"]["deploymentHash"]
        == report["deploymentFingerprint"]["hash"]
    )


def test_a_report_without_a_fingerprint_still_signs_and_says_so(tmp_path):
    from opaas.credential import build_credential

    report = {"runId": "r", "model": {"name": "m"}, "configuration": {"configHash": "h"}}
    credential = build_credential(report, "sha256:x", issuer="did:web:x", key_id="did:web:x#1")
    assert credential["credentialSubject"]["configuration"]["deploymentHash"] is None


# ----------------------------------------------------------------- sources


def test_ollama_publishes_the_digest_and_template_of_the_model_it_serves():
    tags = {"models": [{**TAGS["models"][0], "digest": "abc123"}]}
    show = {**SHOW, "template": "{{ .Prompt }}"}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json=show)
        return _handler(tags=tags).handle_request(request)

    backend = _load(httpx.MockTransport(handle))
    assert backend.model_id == MODEL
    assert backend.model_digest() == "sha256:abc123"
    assert backend.chat_template_hash().startswith("sha256:")


def test_ollama_without_a_digest_reports_it_unknown():
    backend = _load(_handler())
    assert backend.model_digest() is None
    assert backend.chat_template_hash() is None


def test_nvidia_smi_driver_version_is_read(monkeypatch):
    line = "0, NVIDIA RTX 4090, 1024, 24564, 3, 35.2, 560.35\n"
    monkeypatch.setattr(gpu_module.shutil, "which", lambda _: "nvidia-smi")
    monkeypatch.setattr(
        gpu_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=line, stderr=""),
    )
    snapshot = gpu_module.query_gpus()
    assert snapshot.gpus[0]["driver_version"] == "560.35"
    assert snapshot.gpus[0]["power_w"] == 35.2


# -------------------------------------------------------------------- site


def _site_page(tmp_path, report):
    report_path = tmp_path / "report.json"
    report_path.write_bytes(canonicalize(report))
    credential = _credential(file_digest(report_path))
    credential_path = tmp_path / "credential.json"
    credential_path.write_text(json.dumps(credential), encoding="utf-8")
    build_site([load_entry(report_path, credential_path)], tmp_path / "site", DID)
    return (tmp_path / "site" / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )


def test_the_site_lists_what_the_fingerprint_could_not_see(tmp_path):
    fingerprint = _fingerprint(FakeBackend, environment={})
    page = _site_page(tmp_path, _report(deploymentFingerprint=fingerprint))
    assert fingerprint["hash"] in page
    assert "Not exposed by the deployment" in page
    assert "modelDigest" in page


def test_the_site_says_when_a_report_predates_the_fingerprint(tmp_path):
    page = _site_page(tmp_path, _report())
    assert "predates the fingerprint" in page
