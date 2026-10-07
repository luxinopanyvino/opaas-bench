import json

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.web import SiteError, check_verify_url, load_entry, render_printable

from .conftest import FakeBackend
from .test_cli import _flat, _sign_flow
from .test_web import _report, _write_pair

runner = CliRunner()

@pytest.fixture(autouse=True)
def _register_fake():
    """`_sign_flow` runs the fake backend; registries are restored after each test."""
    register_backend(FakeBackend)


URL = "https://onpremialabs.com/cert/vega-7b-1a3c5e7f-20260813-001/index.html"


def _render(tmp_path, report=None, **kwargs):
    report_path, credential_path = _write_pair(tmp_path, report, **kwargs)
    return render_printable(load_entry(report_path, credential_path), verify_url=URL)


# ---------------------------------------------------------------- rendering


def test_the_certificate_names_the_deployment_and_its_validity(tmp_path):
    page = _render(tmp_path)
    assert "Certificate of Evaluation" in page
    assert "Vega-7B" in page
    assert "GGUF Q4_K_M" in page
    assert "vLLM 0.9.1" in page
    assert "1x RTX 4090 24GB" in page
    assert "until" in page


def test_each_block_gets_its_own_line_and_there_is_no_overall_mark(tmp_path):
    """A diploma has a final mark; this certificate must not invent one."""
    page = _render(tmp_path)
    for name in ("Performance", "Capability / Quality", "Consistency / Robustness",
                 "Behavior and safety", "Compliance readiness"):
        assert name in page
    assert "Accuracy on ifeval 1: reasoning 82%" in page
    assert "no overall score or grade" in page
    for word in ("Passed", "PASSED", "Grade:", "Overall score", "Tier"):
        assert word not in page


def test_a_block_without_results_says_it_was_not_evaluated(tmp_path):
    """Only block B has metrics in the fixture: the others cannot look measured."""
    page = _render(tmp_path)
    assert page.count("This certificate claims nothing about this block.") == 4


def test_a_partial_evaluation_lists_what_was_not_evaluated(tmp_path):
    page = _render(tmp_path, complete=False)
    assert "Partial evaluation." in page
    assert "B (Capability / Quality)" in page
    assert "E (Compliance readiness)" in page


def test_every_declared_limitation_is_printed(tmp_path):
    limitations = [f"Limitation number {n}." for n in range(12)]
    page = _render(tmp_path, _report(limitations=limitations))
    for limitation in limitations:
        assert limitation in page


def test_declared_and_probed_compliance_stay_separate(tmp_path):
    report = _report(
        coverage={"complete": True, "blocksEvaluated": ["A", "B", "C", "D", "E"],
                  "blocksMissing": []},
        suites=[{
            "suite": "compliance", "block": "E", "status": "completed",
            "metrics": {"summary": {"declared": {"total": 7, "yes": 5, "unknown": 1},
                                    "probed": {"total": 3, "pass": 2}}},
        }],
    )
    page = _render(tmp_path, report)
    assert "Verified by OPAAS: 2 of 3 pass · Declared by the operator: 5 of 7 met" in page


def test_a_malicious_model_name_cannot_inject_html(tmp_path):
    report = _report(model={"name": "<img src=x onerror=alert(1)>", "quantization": None})
    page = _render(tmp_path, report)
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page


def test_the_qr_appears_only_with_a_verification_url(tmp_path):
    report_path, credential_path = _write_pair(tmp_path)
    entry = load_entry(report_path, credential_path)
    assert "<svg" in render_printable(entry, verify_url=URL)
    assert "<svg" not in render_printable(entry)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "cert/index.html", "file:///c:/x"])
def test_the_qr_only_encodes_web_addresses(url):
    with pytest.raises(SiteError, match="not a web address"):
        check_verify_url(url)


# --------------------------------------------------------------------- cli


def test_the_command_writes_a_certificate_for_a_credential_that_verifies(tmp_path):
    report, credential, keys = _sign_flow(tmp_path)
    out = tmp_path / "certificate.html"
    result = runner.invoke(
        app,
        ["certificate", "--report", str(report), "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem"), "--url", URL, "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Certificate written" in _flat(result.output)
    page = out.read_text(encoding="utf-8")
    assert "fake/model-1b" in page
    assert "<svg" in page


def test_the_command_refuses_to_print_a_tampered_credential(tmp_path):
    """A printed certificate for a failing signature is a forgery with good typography."""
    report, credential, keys = _sign_flow(tmp_path)
    document = json.loads(credential.read_text(encoding="utf-8"))
    document["credentialSubject"]["configuration"]["hardware"] = "8x H100"
    credential.write_text(json.dumps(document), encoding="utf-8")

    out = tmp_path / "certificate.html"
    result = runner.invoke(
        app,
        ["certificate", "--report", str(report), "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem"), "--out", str(out)],
    )
    assert result.exit_code == 1
    assert "does NOT verify" in _flat(result.output)
    assert not out.exists()


def test_the_command_refuses_without_a_trust_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(tmp_path / "no-anchors"))
    report, credential, _ = _sign_flow(tmp_path)
    out = tmp_path / "certificate.html"
    result = runner.invoke(
        app,
        ["certificate", "--report", str(report), "--credential", str(credential),
         "--out", str(out)],
    )
    assert result.exit_code == 2
    assert not out.exists()
