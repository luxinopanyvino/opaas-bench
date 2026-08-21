import json
from pathlib import Path

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


def test_the_generation_caps_reach_blocks_b_and_d(tmp_path):
    """They govern most of a run's wall clock and were unreachable: --max-tokens
    only ever fed block A."""
    result = _run(tmp_path, "--suite", "all", "--limit", "2", "--context-words", "64",
                  "--capability-max-tokens", "128", "--safety-max-tokens", "96")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    capability = next(s for s in report["suites"] if s["suite"] == "capability")
    safety = next(s for s in report["suites"] if s["suite"] == "safety")
    assert capability["metrics"]["options"]["instruction_max_tokens"] == 128
    assert safety["metrics"]["options"]["max_tokens"] == 96


def test_the_generation_caps_leave_block_a_alone(tmp_path):
    """--max-tokens is block A's own knob and must not be shadowed by them."""
    result = _run(tmp_path, "--suite", "all", "--limit", "2", "--context-words", "64",
                  "--max-tokens", "8", "--capability-max-tokens", "128")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    performance = next(s for s in report["suites"] if s["suite"] == "performance")
    assert performance["metrics"]["options"]["max_tokens"] == 8


@pytest.mark.parametrize("flag", ["--capability-max-tokens", "--safety-max-tokens"])
def test_non_positive_generation_caps_are_rejected(tmp_path, flag):
    result = _run(tmp_path, flag, "0")
    assert result.exit_code == 2
    assert "greater than 0" in _flat(result.output)


def test_suite_concurrency_reaches_blocks_b_and_d(tmp_path):
    """It was hardcoded to 4 and unreachable from the CLI: --concurrency only
    ever fed block A."""
    result = _run(tmp_path, "--suite", "all", "--limit", "2", "--context-words", "64",
                  "--suite-concurrency", "2")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    for name in ("capability", "safety"):
        suite = next(s for s in report["suites"] if s["suite"] == name)
        assert suite["metrics"]["options"]["concurrency"] == 2, name


def test_a_declared_concurrency_limit_reaches_the_report(tmp_path):
    result = _run(tmp_path, "--suite", "performance", "--max-concurrency", "4")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    performance = next(s for s in report["suites"] if s["suite"] == "performance")
    assert performance["metrics"]["declaredMaxConcurrency"] == 4


def test_without_a_declaration_the_unknown_limit_is_stated(tmp_path):
    result = _run(tmp_path, "--suite", "performance", "--concurrency", "1,2")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    performance = next(s for s in report["suites"] if s["suite"] == "performance")
    assert performance["metrics"]["declaredMaxConcurrency"] is None
    assert any("--max-concurrency" in lim for lim in performance["limitations"])


@pytest.mark.parametrize("flag,value", [("--max-concurrency", "0"), ("--suite-concurrency", "0")])
def test_non_positive_concurrency_flags_are_rejected(tmp_path, flag, value):
    result = _run(tmp_path, flag, value)
    assert result.exit_code == 2
    assert "greater than 0" in _flat(result.output)


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


def test_show_does_not_read_report_text_as_rich_markup(tmp_path):
    """Limitations are tagged '[faithfulness] ...'. Printed raw, Rich takes that
    for a style and swallows it, so the line loses the word that named the
    dimension — a renderer quietly dropping part of a limitation."""
    assert _run(tmp_path).exit_code == 0
    path = tmp_path / "result.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["limitations"] = ["[faithfulness] No faithfulness classifier."]
    path.write_text(json.dumps(report), encoding="utf-8")

    result = runner.invoke(app, ["show", str(path)])
    assert result.exit_code == 0
    assert "[faithfulness] No faithfulness classifier." in _flat(result.output)


def test_show_survives_a_report_with_broken_markup(tmp_path):
    """A third party's report is not ours to trust for rendering."""
    assert _run(tmp_path).exit_code == 0
    path = tmp_path / "result.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["limitations"] = ["unclosed [/] tag"]
    report["model"]["name"] = "model [bold]with markup"
    path.write_text(json.dumps(report), encoding="utf-8")

    result = runner.invoke(app, ["show", str(path)])
    assert result.exit_code == 0
    assert "unclosed [/] tag" in _flat(result.output)
    assert "model [bold]with markup" in _flat(result.output)


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


# ------------------------------------------------------------------- fetch


def test_fetch_lists_classifiers_among_the_available_sources(monkeypatch):
    """With the extra installed, 'toxicity'/'faithfulness' are valid `fetch`
    arguments, listed alongside the dataset sources."""
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "classifiers_available", lambda: True)
    result = runner.invoke(app, ["fetch", "made-up-source"])
    assert result.exit_code == 2
    assert "toxicity" in _flat(result.output)
    assert "faithfulness" in _flat(result.output)


def test_fetch_downloads_a_named_classifier(monkeypatch):
    import opaas_bench.cli.main as main_module

    calls: list = []
    monkeypatch.setattr(main_module, "is_default_classifier_cached", lambda task: False)
    monkeypatch.setattr(
        main_module,
        "ensure_default_classifier",
        lambda task, **k: calls.append(task) or Path(f"/cache/classifiers/{task}"),
    )
    result = runner.invoke(app, ["fetch", "toxicity"])
    assert result.exit_code == 0, result.output
    assert calls == ["toxicity"]
    assert "toxicity" in _flat(result.output)


def test_fetch_does_not_redownload_an_already_cached_classifier(monkeypatch):
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "is_default_classifier_cached", lambda task: True)
    monkeypatch.setattr(
        main_module,
        "ensure_default_classifier",
        lambda *a, **k: pytest.fail("must not redownload an already-cached classifier"),
    )
    result = runner.invoke(app, ["fetch", "toxicity"])
    assert result.exit_code == 0, result.output
    assert "already cached" in _flat(result.output)


def test_fetch_with_no_argument_leaves_classifiers_alone_without_the_extra(monkeypatch):
    """A plain dataset-only install should not suddenly pull the classifier
    weights just because `fetch` ran with no argument."""
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "classifiers_available", lambda: False)
    monkeypatch.setattr(main_module, "is_cached", lambda name: True)
    monkeypatch.setattr(
        main_module,
        "ensure_default_classifier",
        lambda *a, **k: pytest.fail("must not be attempted without the extra"),
    )
    result = runner.invoke(app, ["fetch"])
    assert result.exit_code == 0, result.output


def test_limit_is_refused_on_a_classifier_instead_of_silently_ignored(monkeypatch):
    """--limit trims a dataset's items; a classifier has none. Accepting the
    flag and doing nothing is the silent no-op this tool exists to catch."""
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(
        main_module,
        "ensure_default_classifier",
        lambda *a, **k: pytest.fail("must not download when the flags are contradictory"),
    )
    result = runner.invoke(app, ["fetch", "toxicity", "--limit", "5"])
    assert result.exit_code == 2
    assert "does not apply" in _flat(result.output)


def test_limit_with_everything_says_which_half_it_applies_to(monkeypatch):
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "classifiers_available", lambda: True)
    monkeypatch.setattr(main_module, "is_cached", lambda name: True)
    monkeypatch.setattr(main_module, "is_default_classifier_cached", lambda task: True)
    result = runner.invoke(app, ["fetch", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "applies to the datasets" in _flat(result.output)


def test_fetch_reports_a_classifier_download_failure(monkeypatch):
    import opaas_bench.cli.main as main_module
    from opaas_bench.classifiers import ClassifierError

    def _boom(task, **kwargs):
        raise ClassifierError("network unreachable")

    monkeypatch.setattr(main_module, "is_default_classifier_cached", lambda task: False)
    monkeypatch.setattr(main_module, "ensure_default_classifier", _boom)
    result = runner.invoke(app, ["fetch", "toxicity"])
    assert result.exit_code == 1
    assert "network unreachable" in _flat(result.output)


def test_a_classifier_error_keeps_its_install_hint_intact(monkeypatch):
    """The hint names 'opaas-bench[classifiers]'. Unescaped, Rich reads that
    bracket as a style tag and swallows the part that says what to install."""
    import opaas_bench.cli.main as main_module
    from opaas_bench.classifiers import ClassifierError

    def _boom(task, **kwargs):
        raise ClassifierError("huggingface-hub is missing: pip install 'opaas-bench[classifiers]'")

    monkeypatch.setattr(main_module, "is_default_classifier_cached", lambda task: False)
    monkeypatch.setattr(main_module, "ensure_default_classifier", _boom)
    result = runner.invoke(app, ["fetch", "toxicity"])
    assert "opaas-bench[classifiers]" in _flat(result.output)


# ---------------------------------------------------------------- datasets


def test_datasets_lists_the_pinned_classifiers_when_the_extra_is_installed(monkeypatch):
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "classifiers_available", lambda: True)
    monkeypatch.setattr(
        main_module, "is_default_classifier_cached", lambda task: task == "toxicity"
    )
    result = runner.invoke(app, ["datasets"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "unitary/toxic-bert" in flat
    assert "cached" in flat


def test_datasets_says_how_to_install_the_extra_when_it_is_missing(monkeypatch):
    import opaas_bench.cli.main as main_module

    monkeypatch.setattr(main_module, "classifiers_available", lambda: False)
    result = runner.invoke(app, ["datasets"])

    assert result.exit_code == 0, result.output
    assert "opaas-bench[classifiers]" in _flat(result.output)
