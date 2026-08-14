import json

import pytest
from typer.testing import CliRunner

from opaas_bench.adapters import register_backend
from opaas_bench.cli.main import app

from .conftest import FakeBackend

runner = CliRunner()

#: Test passphrase. keygen no longer takes it by argument except here.
PASSPHRASE = "test-pass-phrase-value"


def _keygen(out):
    """Without `--passphrase`, keygen asks at the keyboard, and there is no keyboard here."""
    return runner.invoke(app, ["keygen", "--out", str(out), "--passphrase", PASSPHRASE])


def _flat(text: str) -> str:
    """Rich wraps to the terminal width: normalize before searching for strings."""
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _register_fake():
    register_backend(FakeBackend)


def _run(tmp_path, *extra):
    return runner.invoke(
        app,
        [
            "run",
            "--model", "fake/model-1b",
            "--backend", "fake",
            "--concurrency", "1",
            "--prompt-words", "32",
            "--max-tokens", "8",
            "--requests", "2",
            "--warmup", "0",
            "--idle-seconds", "0",
            # Explicit: the real default downloads, and the tests do not touch the network.
            "--dataset", "opaas-smoke",
            "--output", str(tmp_path / "result.json"),
            *extra,
        ],
    )


def test_version_reports_tool_and_methodology():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "opaas-bench" in result.output
    assert "methodology" in result.output


def test_backends_listing_includes_vllm():
    result = runner.invoke(app, ["backends"])
    assert result.exit_code == 0
    assert "vllm" in result.output


def test_suites_listing_covers_the_five_blocks():
    result = runner.invoke(app, ["suites"])
    assert result.exit_code == 0
    flat = _flat(result.output)
    for suite in ("performance", "capability", "consistency", "safety", "compliance"):
        assert suite in flat


def test_run_writes_report_to_the_given_file(tmp_path):
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["status"] == "evaluated-partial"
    assert report["coverage"]["blocksEvaluated"] == ["A"]


def test_suite_all_now_gives_complete_coverage(tmp_path):
    """With all five blocks implemented, --suite all really is a complete evaluation."""
    result = _run(tmp_path, "--suite", "all", "--limit", "2", "--context-words", "64")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["coverage"]["blocksEvaluated"] == ["A", "B", "C", "D", "E"]
    assert report["coverage"]["complete"] is True
    assert report["status"] == "evaluated"


def test_unknown_suite_lists_the_available_ones(tmp_path):
    result = _run(tmp_path, "--suite", "telepathy")
    assert result.exit_code == 2
    assert "Available" in _flat(result.output)


def test_invalid_concurrency_is_rejected(tmp_path):
    result = runner.invoke(
        app,
        ["run", "--model", "m", "--backend", "fake", "--concurrency", "one",
         "--output", str(tmp_path / "result.json")],
    )
    assert result.exit_code != 0


def test_show_prints_existing_report(tmp_path):
    assert _run(tmp_path).exit_code == 0
    result = runner.invoke(app, ["show", str(tmp_path / "result.json")])
    assert result.exit_code == 0
    assert "Report hash" in _flat(result.output)


def test_show_missing_report_exits_with_error(tmp_path):
    result = runner.invoke(app, ["show", str(tmp_path / "does-not-exist.json")])
    assert result.exit_code == 2


def test_keygen_creates_root_of_trust(tmp_path):
    result = _keygen(tmp_path / "keys")
    assert result.exit_code == 0
    assert (tmp_path / "keys" / "opaas-root-ed25519.pem").exists()
    assert (tmp_path / "keys" / "did.json").exists()


def test_keygen_without_a_terminal_refuses_instead_of_hanging(tmp_path):
    """With no keyboard it cannot ask, and an unencrypted key is not a silent default."""
    result = runner.invoke(app, ["keygen", "--out", str(tmp_path / "keys")])
    assert result.exit_code == 2
    assert "interactive terminal" in _flat(result.output)
    assert "OPAAS_KEY_PASSPHRASE" in _flat(result.output)
    assert not (tmp_path / "keys").exists()


def test_passing_the_passphrase_as_an_argument_warns_about_the_history(tmp_path):
    """It stays in the shell history and in the process list."""
    result = _keygen(tmp_path / "keys")
    assert result.exit_code == 0
    assert "history" in _flat(result.output)


def test_signing_with_the_wrong_passphrase_fails_clearly(tmp_path):
    _, _, keys = _sign_flow(tmp_path)
    result = runner.invoke(
        app,
        [
            "sign",
            "--input", str(tmp_path / "result.json"),
            "--key", str(keys / "opaas-root-ed25519.pem"),
            "--passphrase", "the-wrong-passphrase",
            "--out", str(tmp_path / "other.json"),
        ],
    )
    assert result.exit_code == 2
    assert "private key" in _flat(result.output)


def test_keygen_refuses_second_run(tmp_path):
    _keygen(tmp_path / "keys")
    assert _keygen(tmp_path / "keys").exit_code == 2


def _sign_flow(tmp_path):
    """run → sign, returns (report, credential, keys directory)."""
    assert _run(tmp_path).exit_code == 0
    keys = tmp_path / "keys"
    keygen = _keygen(keys)
    assert keygen.exit_code == 0, keygen.output
    result = runner.invoke(
        app,
        [
            "sign",
            "--input", str(tmp_path / "result.json"),
            "--key", str(keys / "opaas-root-ed25519.pem"),
            "--passphrase", PASSPHRASE,
            "--out", str(tmp_path / "credential.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    return tmp_path / "result.json", tmp_path / "credential.json", keys


def test_sign_produces_a_verifiable_credential(tmp_path):
    report, credential, keys = _sign_flow(tmp_path)
    document = json.loads(credential.read_text(encoding="utf-8"))
    assert document["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert document["credentialSubject"]["evalRun"]["reportHash"].startswith("sha256:")

    result = runner.invoke(
        app,
        ["verify", "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem"),
         "--report", str(report)],
    )
    assert result.exit_code == 0, result.output
    assert "Credential valid" in _flat(result.output)


def test_verify_detects_a_tampered_credential(tmp_path):
    _, credential, keys = _sign_flow(tmp_path)
    document = json.loads(credential.read_text(encoding="utf-8"))
    document["credentialSubject"]["model"]["name"] = "some-better-model"
    credential.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(
        app,
        ["verify", "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem")],
    )
    assert result.exit_code == 1
    assert "NOT valid" in _flat(result.output)


def test_verify_detects_a_swapped_report(tmp_path):
    report, credential, keys = _sign_flow(tmp_path)
    report.write_text(json.dumps({"runId": "made-up"}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["verify", "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem"),
         "--report", str(report)],
    )
    assert result.exit_code == 1
    assert "The report is NOT the certified one" in _flat(result.output)


def test_verify_warns_that_evaluation_is_partial(tmp_path):
    _, credential, keys = _sign_flow(tmp_path)
    result = runner.invoke(
        app,
        ["verify", "--credential", str(credential),
         "--key", str(keys / "opaas-root-ed25519.pub.pem")],
    )
    assert result.exit_code == 0
    assert "NOT complete" in _flat(result.output)


def test_verify_without_trust_anchor_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(tmp_path / "no-anchors"))
    _, credential, _ = _sign_flow(tmp_path)
    result = runner.invoke(app, ["verify", "--credential", str(credential)])
    assert result.exit_code == 2
    assert "There is no trust anchor" in _flat(result.output)


def test_verify_accepts_a_did_document(tmp_path):
    _, credential, keys = _sign_flow(tmp_path)
    result = runner.invoke(
        app,
        ["verify", "--credential", str(credential), "--did-document", str(keys / "did.json")],
    )
    assert result.exit_code == 0, result.output
