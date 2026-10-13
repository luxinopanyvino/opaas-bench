import json
import subprocess
from typing import Any, ClassVar

import httpx
import pytest

from opaas.adapters import register_backend
from opaas.bench.runner import RunRequest, execute
from opaas.core.canonical import canonicalize
from opaas.core.fingerprint import deployment_fingerprint
from opaas.core.hashing import file_digest
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
        "weightsHash": None,
        "tokenizerFilesHash": None,
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
    from opaas.core.credential import build_credential

    report = _run(tmp_path, "run")
    credential = build_credential(report, "sha256:x", issuer="did:web:x", key_id="did:web:x#1")
    assert (
        credential["credentialSubject"]["configuration"]["deploymentHash"]
        == report["deploymentFingerprint"]["hash"]
    )


def test_a_report_without_a_fingerprint_still_signs_and_says_so(tmp_path):
    from opaas.core.credential import build_credential

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


# ------------------------------------------------- weights the server holds (D35)


class LocalServerBackend(DigestBackend):
    """A deployment whose serving process is on this host."""

    name: ClassVar[str] = "fake-local"

    def server_pid(self) -> int | None:
        return 4242


def _model_dir(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    for name in ("model-00001.safetensors", "model-00002.safetensors"):
        (model / name).write_bytes(name.encode())
    (model / "tokenizer_config.json").write_text('{"chat_template": "{{ m }}"}')
    return model


def _weights_fingerprint(model, held, backend_cls=LocalServerBackend, monkeypatch=None):
    from opaas.core import fingerprint as module
    from opaas.core.hashing import tokenizer_files_hash, weights_hash
    from opaas.telemetry.system import normalise_path

    monkeypatch.setattr(
        module, "files_held_by", lambda pid: ({normalise_path(str(p)) for p in held}, None)
    )
    backend = backend_cls()
    backend.load("fake/model-1b", {})
    weights, tokenizer = weights_hash(model), tokenizer_files_hash(model)
    return deployment_fingerprint(
        backend, backend.get_backend_metadata(), GPUS,
        weights_path=model, weights=weights, tokenizer_files=tokenizer,
    ), weights, tokenizer


def test_weights_the_server_holds_enter_the_fingerprint(tmp_path, monkeypatch):
    model = _model_dir(tmp_path)
    fingerprint, weights, tokenizer = _weights_fingerprint(
        model, model.glob("*.safetensors"), monkeypatch=monkeypatch
    )
    assert fingerprint["weightsCheck"]["status"] == "confirmed"
    assert fingerprint["fields"]["weightsHash"] == weights["hash"]
    assert fingerprint["fields"]["tokenizerFilesHash"] == tokenizer["hash"]
    assert "weightsHash" not in fingerprint["unknown"]


def test_declared_weights_the_server_is_not_seen_holding_stay_out(tmp_path, monkeypatch):
    """Hashing files proves what is in them, not that the engine loaded them (D14)."""
    model = _model_dir(tmp_path)
    fingerprint, _, _ = _weights_fingerprint(model, [], monkeypatch=monkeypatch)
    assert fingerprint["weightsCheck"]["status"] == "not-observed"
    assert fingerprint["fields"]["weightsHash"] is None
    assert fingerprint["fields"]["tokenizerFilesHash"] is None
    assert "vLLM" in fingerprint["unknown"]["weightsHash"]


def test_a_server_holding_only_some_of_the_files_is_partial(tmp_path, monkeypatch):
    model = _model_dir(tmp_path)
    fingerprint, _, _ = _weights_fingerprint(
        model, [model / "model-00001.safetensors"], monkeypatch=monkeypatch
    )
    assert fingerprint["weightsCheck"]["status"] == "partial"
    assert fingerprint["fields"]["weightsHash"] is None


def test_a_server_that_cannot_be_located_cannot_confirm_anything(tmp_path, monkeypatch):
    model = _model_dir(tmp_path)
    fingerprint, _, _ = _weights_fingerprint(
        model, model.glob("*.safetensors"), backend_cls=DigestBackend, monkeypatch=monkeypatch
    )
    assert fingerprint["weightsCheck"]["status"] == "unverifiable"
    assert fingerprint["fields"]["weightsHash"] is None


def test_without_weights_path_there_is_nothing_to_confirm():
    fingerprint = _fingerprint()
    assert fingerprint["weightsCheck"]["status"] == "not-declared"
    assert fingerprint["fields"]["weightsHash"] is None


def test_tokenizer_files_are_hashed_beside_a_model_directory(tmp_path):
    from opaas.core.hashing import tokenizer_files_hash

    model = _model_dir(tmp_path)
    (model / "chat_template.jinja").write_text("{{ messages }}")
    result = tokenizer_files_hash(model)
    assert [f["path"] for f in result["files"]] == ["chat_template.jinja", "tokenizer_config.json"]

    (model / "chat_template.jinja").write_text("{{ messages | reverse }}")
    assert tokenizer_files_hash(model)["hash"] != result["hash"]


def test_a_gguf_file_carries_its_own_template(tmp_path):
    from opaas.core.hashing import tokenizer_files_hash

    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    result = tokenizer_files_hash(gguf)
    assert result["available"] is False
    assert "embeds its template" in result["note"]


def test_files_held_by_sees_a_file_this_process_has_open(tmp_path):
    """Real psutil against our own process: no network, no other process."""
    import os

    from opaas.telemetry.system import files_held_by, normalise_path

    target = tmp_path / "held.safetensors"
    target.write_bytes(b"x")
    with target.open("rb"):
        held, reason = files_held_by(os.getpid())
    assert reason is None
    assert normalise_path(str(target)) in held


def test_the_runner_hashes_the_weights_once_and_records_the_check(tmp_path, monkeypatch):
    from opaas.bench import runner
    from opaas.core import hashing

    model = _model_dir(tmp_path)
    calls = []
    original = hashing.weights_hash
    monkeypatch.setattr(runner, "weights_hash", lambda p: calls.append(p) or original(p))
    request = RunRequest(
        model="fake/model-1b", backend="fake-digest", suites=["performance"],
        suite_options=OPTIONS, output=tmp_path / "r" / "report.json", weights_path=model,
    )
    report = json.loads(execute(request).report_path.read_text(encoding="utf-8"))
    assert calls == [model]
    assert report["model"]["weightsHash"]["available"] is True
    assert report["model"]["tokenizerFilesHash"]["available"] is True
    assert report["deploymentFingerprint"]["weightsCheck"]["status"] == "unverifiable"


# ---------------------------------------------- quantization provenance (D37)


class QuantizedBackend(DigestBackend):
    name: ClassVar[str] = "fake-quantized"

    def reported_quantization(self) -> str | None:
        return "Q4_K_M"


def _quantized_run(tmp_path, quantization=None):
    register_backend(QuantizedBackend)
    request = RunRequest(
        model="fake/model-1b", backend="fake-quantized", suites=["performance"],
        suite_options=OPTIONS, output=tmp_path / "q" / "report.json", quantization=quantization,
    )
    return json.loads(execute(request).report_path.read_text(encoding="utf-8"))


def test_the_engine_reported_quantization_is_labelled_as_the_engines(tmp_path):
    model = _quantized_run(tmp_path)["model"]
    assert (model["quantization"], model["quantizationSource"]) == ("Q4_K_M", "engine")


def test_a_consistent_declaration_keeps_both_values(tmp_path):
    report = _quantized_run(tmp_path, "GGUF Q4_K_M")
    model = report["model"]
    assert model["quantizationSource"] == "declared"
    assert model["quantizationReported"] == "Q4_K_M"
    assert not any("contradicts" in item for item in report["limitations"])


def test_a_declaration_the_engine_contradicts_becomes_a_limitation(tmp_path):
    report = _quantized_run(tmp_path, "Q8_0")
    assert "contradicts what the engine reports (Q4_K_M)" in report["limitations"][0]


def test_ollama_reports_the_quantization_of_the_model_it_loaded():
    assert _load(_handler()).reported_quantization() == "Q4_K_M"
