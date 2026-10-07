import json
from datetime import datetime, timedelta, timezone

import pytest

from opaas.web import SiteError, badge_state, build_site, esc, load_entry, render_badge

DID = {
    "id": "did:web:onpremialabs.com",
    "verificationMethod": [
        {
            "id": "did:web:onpremialabs.com#key-1",
            "type": "Ed25519VerificationKey2020",
            "publicKeyMultibase": "z6MkfakeKeyForTests",
        }
    ],
}


def _report(**overrides):
    return {
        "runId": "20260813-001",
        "opaasVersion": "1.0-draft",
        "status": "evaluated",
        "model": {"name": "Vega-7B", "quantization": "GGUF Q4_K_M"},
        "configuration": {
            "backend": "vLLM 0.9.1",
            "hardware": "1x RTX 4090 24GB",
            "configHash": "sha256:1a3c",
        },
        "coverage": {"complete": True, "blocksEvaluated": ["A", "B", "C", "D", "E"],
                     "blocksMissing": []},
        "limitations": ["The dataset is a smoke set."],
        "suites": [
            {
                "suite": "capability",
                "status": "completed",
                "metrics": {
                    "answerStyle": "cot",
                    "dataset": {"name": "ifeval", "version": "1", "datasetHash": "sha256:d1"},
                    "tasks": [
                        {
                            "category": "reasoning",
                            "items": 200,
                            "accuracy": {"point": 0.82, "low": 0.76, "high": 0.87},
                        }
                    ],
                },
            }
        ],
        **overrides,
    }


def _credential(report_hash, *, complete=True, expires_days=180):
    now = datetime.now(timezone.utc)
    return {
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        "id": "urn:opaas:cert:vega-7b:1a3c5e7f:20260813-001",
        "type": ["VerifiableCredential", "OpaasModelCertificate"],
        "issuer": "did:web:onpremialabs.com",
        "issuanceDate": now.isoformat(),
        "expirationDate": (now + timedelta(days=expires_days)).isoformat(),
        "credentialSubject": {
            "model": {"name": "Vega-7B"},
            "configuration": {"backend": "vLLM 0.9.1", "hardware": "1x RTX 4090 24GB",
                              "configHash": "sha256:1a3c"},
            "evalRun": {
                "runId": "20260813-001",
                "reportHash": report_hash,
                "completeEvaluation": complete,
                "blocksEvaluated": ["A", "B", "C", "D", "E"] if complete else ["A"],
                "blocksNotEvaluated": [] if complete else ["B", "C", "D", "E"],
            },
        },
        "proof": {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "proofValue": "z3fake",
            "verificationMethod": "did:web:onpremialabs.com#key-1",
        },
    }


def _write_pair(tmp_path, report=None, *, complete=True, expires_days=180):
    from opaas.canonical import canonicalize
    from opaas.hashing import file_digest

    report_path = tmp_path / "report.json"
    report_path.write_bytes(canonicalize(report or _report()))
    credential_path = tmp_path / "credential.json"
    credential_path.write_text(
        json.dumps(_credential(file_digest(report_path), complete=complete,
                               expires_days=expires_days)),
        encoding="utf-8",
    )
    return report_path, credential_path


# ------------------------------------------------------------------ escaping


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("<script>", "&lt;script&gt;"), ("a&b", "a&amp;b"), ('"x"', "&quot;x&quot;")],
)
def test_html_is_escaped(raw, expected):
    assert esc(raw) == expected


def test_a_malicious_model_name_cannot_inject_html(tmp_path):
    """The model name is chosen by whoever requests the certificate."""
    report = _report(model={"name": "<img src=x onerror=alert(1)>", "quantization": None})
    report_path, credential_path = _write_pair(tmp_path, report)
    build_site([load_entry(report_path, credential_path)], tmp_path / "site", DID)

    page = (tmp_path / "site" / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page


# --------------------------------------------------------------------- badge


def test_a_complete_evaluation_gets_a_green_badge():
    label, colour = badge_state(_credential("sha256:x", complete=True))
    assert label == "A-E evaluated"
    assert colour == "#12683f"


def test_a_partial_evaluation_never_looks_certified():
    """A green badge over half an evaluation is the lie that has to be avoided."""
    label, colour = badge_state(_credential("sha256:x", complete=False))
    assert label == "partial: A"
    assert colour == "#8a5a00"


def test_an_expired_credential_is_red_whatever_it_measured():
    label, colour = badge_state(_credential("sha256:x", complete=True, expires_days=-1))
    assert label == "expired"
    assert colour == "#b21f2d"


def test_the_badge_is_self_contained_svg():
    svg = render_badge(_credential("sha256:x"))
    assert svg.startswith("<svg xmlns=")
    assert "http://www.w3.org/2000/svg" in svg
    assert "<image" not in svg and "@import" not in svg


def test_badge_text_is_escaped():
    credential = _credential("sha256:x", complete=False)
    credential["credentialSubject"]["evalRun"]["blocksEvaluated"] = ["<b>"]
    assert "<b>" not in render_badge(credential)


# ------------------------------------------------- report/credential consistency


def test_a_credential_paired_with_the_wrong_report_is_refused(tmp_path):
    """Publishing a certificate next to somebody else's report is worse than not publishing."""
    report_path, credential_path = _write_pair(tmp_path)
    report_path.write_bytes(b'{"runId":"other"}')
    with pytest.raises(SiteError, match="is not the one"):
        load_entry(report_path, credential_path)


def test_a_missing_file_is_explicit(tmp_path):
    with pytest.raises(SiteError, match="Does not exist"):
        load_entry(tmp_path / "missing.json", tmp_path / "also-missing.json")


# ----------------------------------------------------------------------- site


def test_the_site_has_everything_needed_to_verify_offline(tmp_path):
    report_path, credential_path = _write_pair(tmp_path)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)

    cert = out / "cert" / "vega-7b-1a3c5e7f-20260813-001"
    assert (out / "index.html").exists()
    assert (out / ".well-known" / "did.json").exists()
    assert (out / "assets" / "verify.js").exists()
    for name in ("index.html", "report.json", "credential.json", "badge.svg"):
        assert (cert / name).exists()

    page = (cert / "index.html").read_text(encoding="utf-8")
    # Embedded data: verification depends on neither fetch nor the server.
    for block in ("credential-data", "did-data", "report-data"):
        assert f'id="{block}"' in page


def test_the_embedded_report_is_byte_identical(tmp_path):
    import base64
    import re

    report_path, credential_path = _write_pair(tmp_path)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)

    page = (out / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    embedded = re.search(r'id="report-data" type="text/plain">([^<]+)<', page).group(1)
    assert base64.b64decode(embedded) == report_path.read_bytes()


def test_a_partial_evaluation_is_stated_on_the_page(tmp_path):
    report = _report(
        coverage={
            "complete": False,
            "blocksEvaluated": ["A"],
            "blocksMissing": [{"block": "B", "name": "Capability", "status": "not run"}],
        }
    )
    report_path, credential_path = _write_pair(tmp_path, report, complete=False)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)

    page = (out / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Partial evaluation" in page
    assert "claims <strong>nothing</strong> about the unevaluated blocks" in page
    assert "B (Capability)" in page


def test_limitations_are_published_not_hidden(tmp_path):
    report_path, credential_path = _write_pair(tmp_path)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)
    page = (out / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Declared limitations" in page
    assert "The dataset is a smoke set." in page


def test_block_results_are_rendered_per_category(tmp_path):
    report_path, credential_path = _write_pair(tmp_path)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)
    page = (out / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Block B — Capability" in page
    assert "reasoning" in page
    assert "82.0%" in page


def test_no_aggregate_score_on_the_page(tmp_path):
    report_path, credential_path = _write_pair(tmp_path)
    out = tmp_path / "site"
    build_site([load_entry(report_path, credential_path)], out, DID)
    page = (out / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in ("aggregate score", "global score", "overall score"):
        assert forbidden not in page


def test_the_index_flags_how_many_are_partial(tmp_path):
    first = _write_pair(tmp_path / "a", complete=True)
    second = _write_pair(tmp_path / "b", complete=False)
    out = tmp_path / "site"
    build_site([load_entry(*first), load_entry(*second)], out, DID)

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "1 of 2 certificates" in index
    assert "partial" in index


def test_publishing_nothing_is_an_error(tmp_path):
    with pytest.raises(SiteError, match="no certificates to publish"):
        build_site([], tmp_path / "site", DID)


@pytest.fixture(autouse=True)
def _make_dirs(tmp_path):
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)


def test_the_certificate_page_heads_operator_notes_as_unverified(tmp_path):
    report_path, credential_path = _write_pair(
        tmp_path, _report(operatorNotes=["Engine restarted before block A."])
    )
    build_site([load_entry(report_path, credential_path)], tmp_path / "site", DID)
    page = (tmp_path / "site" / "cert" / "vega-7b-1a3c5e7f-20260813-001" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Declared by the operator" in page
    assert "not verified by OPAAS" in page
    assert "Engine restarted before block A." in page
