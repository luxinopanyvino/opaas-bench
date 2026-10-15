"""The system audit, phase S2: certifiability and the system certificate (D60)."""

import json

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.core.credential import CredentialError, build_system_credential, public_identifier
from opaas.core.evidence import envelope, write_document
from opaas.discover.inventory import fold_runners
from opaas.system import load_manifest
from opaas.system.report import Member, StepOutcome, build_report
from opaas.web import build_site, load_entry, render_printable
from opaas.web.badge import badge_state

from .conftest import FakeBackend
from .test_cli import _sign_flow
from .test_system import BENCH, SMITH

runner = CliRunner()


@pytest.fixture(autouse=True)
def _register_fake():
    """`_sign_flow` runs the fake backend; registries are restored after each test."""
    register_backend(FakeBackend)


PASSPHRASE = "test-pass-phrase-value"
MODEL = {"id": "llm", "kind": "model", "backend": "ollama", "model": "qwen2.5:7b-instruct"}
AGENT = {"id": "agent", "kind": "agent", "adapter": "bare-model"}
BOUND = {"bound": True, "inventoryHash": "sha256:inv"}
INVENTORY = {"kind": "inventory", "environmentFingerprint": BOUND,
             "inventory": {"engines": [], "hash": "sha256:inv", "enginesHash": "sha256:eng"}}
CERTIFIABLE_SMITH = {**SMITH, "certifiable": True, "environmentFingerprint": BOUND}


def _bench(**overrides):
    return {**BENCH, "environmentInventory": {"hash": "sha256:eng"}, **overrides}


def _manifest(components=(MODEL, AGENT)):
    return load_manifest({"format": 1, "id": "assistant", "purpose": "Drafts summaries.",
                          "components": list(components),
                          "flows": [{"from": "agent", "to": "llm"}]},
                         known_adapters=("bare-model", "http"))


def _body(tmp_path, members, steps=(), components=(MODEL, AGENT)):
    paths = []
    for index, (component, document) in enumerate(members):
        path = tmp_path / f"member-{index}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(Member(component, "step", path))
    return build_report(_manifest(components), manifest_hash="sha256:m", members=paths,
                        steps=list(steps))


def _reasons(body):
    return " ".join(body["certifiability"]["reasons"])


# ------------------------------------------------------------ certifiability


def test_a_system_meeting_every_rule_is_certifiable(tmp_path):
    body = _body(tmp_path, [("system", INVENTORY), ("llm", _bench()),
                            ("agent", CERTIFIABLE_SMITH)])
    assert body["environments"]["consistent"] is True
    assert body["certifiability"] == {"certifiable": True, "reasons": []}


@pytest.mark.parametrize("members, steps, expected", [
    ([("agent", CERTIFIABLE_SMITH)], (), "Model llm has no bench report"),
    ([("llm", _bench(status="partial")), ("agent", CERTIFIABLE_SMITH)], (), "is partial"),
    ([("llm", _bench(referenceType="cloud-baseline")), ("agent", CERTIFIABLE_SMITH)], (),
     "cloud baseline"),
    ([("llm", _bench()), ("agent", SMITH)], (), "only OPAAS's reference scenarios"),
    ([("llm", _bench())], (), "Agent agent has no smith report"),
    ([("llm", _bench())], [StepOutcome("smith", "agent", "pending", "no adapter")],
     "adapter is not implemented"),
    ([("llm", _bench()), ("agent", CERTIFIABLE_SMITH)],
     [StepOutcome("inquest", "system", "failed", "exit 2")], "Step inquest failed"),
])
def test_every_rule_a_system_fails_is_named(tmp_path, members, steps, expected):
    body = _body(tmp_path, [("system", INVENTORY), *members], steps)
    assert body["certifiability"]["certifiable"] is False
    assert expected in _reasons(body)


def test_a_bench_report_is_compared_by_its_engines_with_the_inventory(tmp_path):
    """A bench report inventories engines only: its hash is the inventory's enginesHash."""
    same = _body(tmp_path, [("system", INVENTORY), ("llm", _bench()),
                            ("agent", CERTIFIABLE_SMITH)])
    assert same["environments"]["consistent"] is True
    other = _body(tmp_path, [("system", INVENTORY),
                             ("llm", _bench(environmentInventory={"hash": "sha256:else"})),
                             ("agent", CERTIFIABLE_SMITH)])
    assert "other engines and models" in _reasons(other)


def test_ollamas_model_runner_is_not_a_second_engine():
    """While a model is loaded Ollama runs llama-server; the inventory must not change."""
    ollama = {"kind": "ollama", "port": 11434, "models": [{"name": "qwen"}]}
    runner_engine = {"kind": "openai-compatible", "port": 59249, "bind": "loopback",
                     "process": "llama-server.exe",
                     "models": [{"name": "C:\\Users\\u\\.ollama\\models\\blobs\\sha256-2bad"}]}
    other = {"kind": "vllm", "port": 8000, "process": "python", "models": [{"name": "m"}]}
    folded = fold_runners([ollama, runner_engine, other])
    assert [e["kind"] for e in folded] == ["ollama", "vllm"]
    assert folded[0]["runners"][0]["port"] == 59249
    assert fold_runners([runner_engine]) == [runner_engine], "no Ollama: kept as found"


# --------------------------------------------------------------- credential


def _system_report(tmp_path, certifiable=True):
    smith = CERTIFIABLE_SMITH if certifiable else SMITH
    body = _body(tmp_path, [("system", INVENTORY), ("llm", _bench()), ("agent", smith)])
    path = tmp_path / "system-report.json"
    write_document(path, envelope("system-report", body, environment=None))
    return path


def test_the_system_credential_carries_coverage_and_certifiability(tmp_path):
    path = _system_report(tmp_path, certifiable=False)
    report = json.loads(path.read_text(encoding="utf-8"))
    credential = build_system_credential(report, "sha256:abc", issuer="did:key:z6Mk",
                                         key_id="did:key:z6Mk#z6Mk")
    subject = credential["credentialSubject"]
    assert credential["type"] == ["VerifiableCredential", "OpaasSystemCertificate"]
    assert subject["system"]["reportHash"] == "sha256:abc"
    assert subject["certifiable"] is False and subject["notCertifiableBecause"]
    assert subject["coverage"]["llm"]["capability"] == "measured"
    assert public_identifier(credential).startswith("opaas:system:assistant:")


def test_a_system_credential_refuses_what_is_not_a_certifiable_subject(tmp_path):
    with pytest.raises(CredentialError, match="not a system report"):
        build_system_credential(BENCH, "sha256:x", issuer="i", key_id="k")
    report = json.loads(_system_report(tmp_path).read_text(encoding="utf-8"))
    report["members"][1]["referenceType"] = "cloud-baseline"
    with pytest.raises(CredentialError, match="CLOUD REFERENCE"):
        build_system_credential(report, "sha256:x", issuer="i", key_id="k")


def _keys(tmp_path):
    keys = tmp_path / "keys"
    result = runner.invoke(app, ["keygen", "--out", str(keys), "--passphrase", PASSPHRASE])
    assert result.exit_code == 0, result.output
    return keys


def _sign(tmp_path, report, keys):
    credential = tmp_path / "system-credential.json"
    result = runner.invoke(app, ["sign", "--input", str(report), "--key",
                                 str(keys / "opaas-root-ed25519.pem"), "--passphrase",
                                 PASSPHRASE, "--out", str(credential)])
    assert result.exit_code == 0, result.output
    return credential, result.output


def test_a_signed_system_certificate_verifies_offline_and_a_model_one_still_does(tmp_path):
    """S2 is done when both kinds verify: nothing issued before D60 is orphaned."""
    keys = _keys(tmp_path)
    report = _system_report(tmp_path)
    credential, output = _sign(tmp_path, report, keys)
    assert "system   : assistant" in output
    verified = runner.invoke(app, ["verify", "--credential", str(credential), "--report",
                                   str(report), "--key",
                                   str(keys / "opaas-root-ed25519.pub.pem")])
    assert verified.exit_code == 0, verified.output
    assert "NOT certifiable" not in verified.output

    model_report, model_credential, model_keys = _sign_flow(tmp_path / "model")
    model = runner.invoke(app, ["verify", "--credential", str(model_credential), "--report",
                                str(model_report), "--key",
                                str(model_keys / "opaas-root-ed25519.pub.pem")])
    assert model.exit_code == 0, model.output

    report.write_bytes(report.read_bytes().replace(b"assistant", b"assistanT", 1))
    tampered = runner.invoke(app, ["verify", "--credential", str(credential), "--report",
                                   str(report), "--key",
                                   str(keys / "opaas-root-ed25519.pub.pem")])
    assert tampered.exit_code == 1


def test_verify_warns_when_the_system_is_not_certifiable(tmp_path):
    keys = _keys(tmp_path)
    report = _system_report(tmp_path, certifiable=False)
    credential, output = _sign(tmp_path, report, keys)
    assert "NOT certifiable" in output
    verified = runner.invoke(app, ["verify", "--credential", str(credential), "--key",
                                   str(keys / "opaas-root-ed25519.pub.pem")])
    assert verified.exit_code == 0
    assert "authentic but the system is NOT certifiable" in " ".join(verified.output.split())


# -------------------------------------------------------------- rendering


def test_the_printable_system_certificate_shows_cells_never_a_score(tmp_path):
    keys = _keys(tmp_path)
    report = _system_report(tmp_path, certifiable=False)
    credential, _ = _sign(tmp_path, report, keys)
    page = render_printable(load_entry(report, credential),
                            verify_url="https://example.org/cert/x/index.html")
    assert "Certificate of System Audit" in page
    assert "Not certifiable." in page and "reference scenarios" in page
    assert "class='coverage'" in page and "<svg" in page
    lowered = page.lower()
    for word in ("overall score", "grade:", "tier"):
        assert word not in lowered.replace("no overall score or grade", "")


def test_the_site_publishes_a_system_next_to_a_model(tmp_path):
    keys = _keys(tmp_path)
    report = _system_report(tmp_path)
    credential, _ = _sign(tmp_path, report, keys)
    model_report, model_credential, _ = _sign_flow(tmp_path / "model")
    did = json.loads((keys / "did.json").read_text(encoding="utf-8"))
    entries = [load_entry(report, credential), load_entry(model_report, model_credential)]
    build_site(entries, tmp_path / "site", did)
    system_entry = entries[0]
    page = (tmp_path / "site" / "cert" / system_entry.slug / "index.html").read_text(
        encoding="utf-8")
    assert "Certifiability" in page and "class='coverage'" in page
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "system · 2 components" in index
    assert badge_state(system_entry.credential)[0] == "system certifiable"


# ------------------------------------------------------- environment binding


def test_overhead_and_swarm_bind_to_the_inventory(tmp_path):
    """N5: without it, no system with a proxy or flows could ever be certifiable."""
    inventory = tmp_path / "inventory.json"
    assert runner.invoke(app, ["discover", "scan", "--no-containers", "-o",
                               str(inventory)]).exit_code == 0
    overhead = tmp_path / "overhead.json"
    result = runner.invoke(app, ["sentinel", "overhead", "-n", "10", "--inventory",
                                 str(inventory), "-o", str(overhead)])
    assert result.exit_code == 0, result.output
    binding = json.loads(overhead.read_text(encoding="utf-8"))["environmentFingerprint"]
    assert binding["bound"] is True and binding["inventoryHash"]
