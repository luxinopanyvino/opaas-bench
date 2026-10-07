import json

import pytest

from opaas.declaration import (
    DECLARED_REQUIREMENTS,
    REQUIREMENTS,
    DeclarationError,
    load_declaration,
    template,
)
from opaas.suites.compliance import ComplianceSuite
from opaas.telemetry.network import EXTERNAL, LOOPBACK, PRIVATE, classify

from .conftest import ScriptedBackend


def _run(options=None, responder=None):
    backend = ScriptedBackend(responder or (lambda prompt: "hello"))
    backend.load("fake/model-1b", {})
    return ComplianceSuite(options or {}).run(backend)


def _items(result):
    return {item["id"]: item for item in result.metrics["items"]}


def _write(tmp_path, items):
    path = tmp_path / "declaration.json"
    path.write_text(json.dumps({"provider": "ACME", "items": items}), encoding="utf-8")
    return path


# -------------------------------------------------------- IP classification


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", LOOPBACK),
        ("::1", LOOPBACK),
        ("192.168.1.10", PRIVATE),
        ("10.0.0.5", PRIVATE),
        ("172.16.0.1", PRIVATE),
        ("8.8.8.8", EXTERNAL),
        ("not-an-ip", "unknown"),
    ],
)
def test_address_scope_classification(address, expected):
    assert classify(address) == expected


# ---------------------------------------------------------------- declaration


def test_template_covers_every_declared_requirement():
    items = template()["items"]
    assert set(items) == {r.id for r in DECLARED_REQUIREMENTS}
    assert all(entry["status"] == "unknown" for entry in items.values())


def test_declaration_rejects_unknown_requirements(tmp_path):
    with pytest.raises(DeclarationError, match="unknown requirement"):
        load_declaration(_write(tmp_path, {"made-up": {"status": "yes"}}))


def test_declaration_rejects_invalid_status(tmp_path):
    with pytest.raises(DeclarationError, match="status"):
        load_declaration(_write(tmp_path, {"licenseDocumented": {"status": "maybe"}}))


def test_missing_declaration_file_is_explicit(tmp_path):
    with pytest.raises(DeclarationError, match="does not exist"):
        load_declaration(tmp_path / "missing.json")


# ---------------------------------------------------------------------- suite


def test_runs_without_a_declaration_and_says_what_is_missing():
    result = _run()
    assert result.status == "completed"
    assert any("No compliance declaration" in lim for lim in result.limitations)
    assert result.metrics["summary"]["declared"]["unknown"] == len(DECLARED_REQUIREMENTS)


def test_the_suggested_command_is_one_that_exists():
    """The limitation told the reader to run 'compliance --template', a flag that
    was never implemented: following the report's own advice failed."""
    from typer.main import get_command

    from opaas.cli.main import app

    advice = next(lim for lim in _run().limitations if "No compliance declaration" in lim)
    assert "--template" not in advice

    flags = [word.strip("'\",.") for word in advice.split() if word.startswith("--")]
    assert flags, "the advice names no flag to run"

    # Asked of the command tree, not of rendered `--help` text: help output is
    # wrapped by Rich to the terminal width and differs between a developer's
    # machine and CI, which is how this test first failed on a flag that exists.
    accepted = {
        option
        for command in get_command(app).commands.values()
        for parameter in command.params
        for option in (*parameter.opts, *parameter.secondary_opts)
    }
    for flag in flags:
        assert flag in accepted, f"the advice names {flag}, which no command accepts"


def test_declared_and_probed_are_never_summed():
    summary = _run().metrics["summary"]
    assert summary["declared"]["total"] == len(DECLARED_REQUIREMENTS)
    assert summary["probed"]["total"] == 3
    assert "not added together" in summary["note"]


def test_declared_items_are_marked_as_unverified(tmp_path):
    path = _write(
        tmp_path,
        {"licenseDocumented": {"status": "yes", "evidence": "https://example/license"}},
    )
    items = _items(_run({"declaration": str(path)}))
    assert items["licenseDocumented"]["status"] == "yes"
    assert items["licenseDocumented"]["verifiedByOpaasBench"] is False
    assert items["noExternalEgress"]["verifiedByOpaasBench"] is True


def test_a_claim_without_evidence_is_not_accepted(tmp_path):
    """Declaring 'yes' without supplying anything cannot be recorded as met."""
    path = _write(tmp_path, {"trainingDataDocumented": {"status": "yes", "evidence": ""}})
    items = _items(_run({"declaration": str(path)}))
    assert items["trainingDataDocumented"]["status"] == "unsupported"
    assert "NO evidence" in items["trainingDataDocumented"]["note"]


def test_every_requirement_maps_to_a_framework():
    for item in _run().metrics["items"]:
        assert item["frameworks"]
        assert item["why"]


#: ENS measures the catalogue cites, with their official titles in annex II of
#: Real Decreto 311/2022. Citing a new one means checking it there first (D30):
#: two references in the first catalogue named measures about authentication
#: and personal data for requirements that had nothing to do with either.
CHECKED_ENS_MEASURES = {
    "mp.com.1": "Perímetro seguro",
    "op.exp.1": "Inventario de activos",
    "op.exp.8": "Registro de la actividad",
}


def test_ens_references_are_measures_checked_against_the_official_text():
    cited = {
        reference.removeprefix("ENS ")
        for requirement in REQUIREMENTS
        for reference in requirement.frameworks
        if reference.startswith("ENS ")
    }
    assert cited <= CHECKED_ENS_MEASURES.keys()


def test_ai_act_article_53_is_cited_to_the_paragraph():
    """Art. 53 lists several provider obligations; a bare 'art. 53' points at none."""
    for requirement in REQUIREMENTS:
        assert "AI Act art. 53" not in requirement.frameworks


def test_logprobs_probe_reports_absence_when_the_engine_returns_none():
    items = _items(_run())
    assert items["logprobsAvailable"]["status"] == "fail"


def test_egress_probe_is_unknown_without_an_identifiable_server_process():
    """With an in-process backend there is nothing to observe, and it says so."""
    result = _run()
    egress = _items(result)["noExternalEgress"]
    assert egress["status"] == "unknown"
    assert any("not verifiable" in lim for lim in result.limitations)


def test_airgap_contradiction_is_surfaced(tmp_path, monkeypatch):
    """Air-gapped is declared and the probe sees external traffic: it must be shouted."""
    path = _write(
        tmp_path, {"airGappedDeployment": {"status": "yes", "evidence": "isolated network"}}
    )

    def fake_probe(self, backend):
        return {
            "status": "fail",
            "detail": "connections to 8.8.8.8:443",
            "externalConnections": [{"address": "8.8.8.8", "port": 443, "scope": EXTERNAL}],
        }

    monkeypatch.setattr(ComplianceSuite, "_probe_egress", fake_probe)
    result = _run({"declaration": str(path)})
    assert any("CONTRADICTION" in lim for lim in result.limitations)


def test_no_contradiction_when_the_probe_agrees(tmp_path, monkeypatch):
    path = _write(
        tmp_path, {"airGappedDeployment": {"status": "yes", "evidence": "isolated network"}}
    )
    monkeypatch.setattr(
        ComplianceSuite,
        "_probe_egress",
        lambda self, backend: {"status": "pass", "detail": "no external connections"},
    )
    result = _run({"declaration": str(path)})
    assert not any("CONTRADICTION" in lim for lim in result.limitations)


# ------------------------------------------------- egress probe sample floor (D38)


class _Watcher:
    """Stands in for EgressWatcher: a watchable process with scripted observations."""

    samples = 20
    external: tuple = ()

    def __init__(self, pid, interval_s):
        self.pid = 4242

    def start(self):
        return self

    def stop(self):
        from opaas.telemetry.network import EgressReport, Endpoint

        endpoints = [Endpoint(address=a, port=443, scope="external") for a in self.external]
        return EgressReport(available=True, endpoints=endpoints, samples=self.samples,
                            duration_s=3.1)


def _probe(monkeypatch, *, samples=20, external=(), responder=None, **kwargs):
    from opaas.suites import compliance

    monkeypatch.setattr(
        compliance, "EgressWatcher",
        type("W", (_Watcher,), {"samples": samples, "external": external}),
    )
    backend = ScriptedBackend(responder or (lambda prompt: "OK"))
    backend.load("fake/model-1b", {})
    defaults = {"requests": 3, "interval_s": 0.25, "min_duration_s": 0.0}
    return compliance.probe_egress(backend, **{**defaults, **kwargs}), backend


def test_one_sample_is_not_evidence_of_no_egress(monkeypatch):
    """Three requests to a warm 7B model took 0.19 s and gave one sample, reported as pass."""
    result, _ = _probe(monkeypatch, samples=1)
    assert result["status"] == "unknown"
    assert "too few" in result["detail"]


def test_enough_samples_and_no_external_connections_pass(monkeypatch):
    result, _ = _probe(monkeypatch)
    assert result["status"] == "pass"
    assert result["requests"] == {"sent": 3, "succeeded": 3}


def test_an_external_connection_still_fails(monkeypatch):
    result, _ = _probe(monkeypatch, external=("203.0.113.9",))
    assert result["status"] == "fail"


def test_watching_an_engine_that_answered_nothing_proves_nothing(monkeypatch):
    def broken(prompt):
        raise RuntimeError("engine down")

    result, _ = _probe(monkeypatch, responder=broken)
    assert result["status"] == "unknown"
    assert "never observed" in result["detail"]


def test_the_probe_keeps_the_engine_busy_for_the_minimum_window(monkeypatch):
    result, backend = _probe(monkeypatch, min_duration_s=0.2)
    assert backend.calls > 3
    assert result["requests"]["sent"] == backend.calls
