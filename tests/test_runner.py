import json
from pathlib import Path

import pytest

from opaas.adapters import register_backend
from opaas.hashing import file_digest
from opaas.runner import RunRequest, _publishable_config, execute

from .conftest import FakeBackend

OPTIONS = {
    "performance": {
        "concurrency": [1],
        "prompt_words": [32],
        "max_tokens": 8,
        "requests_per_setting": 2,
        "warmup_requests": 0,
        "sample_interval_s": 5.0,
        "idle_seconds": 0.0,
    }
}


@pytest.fixture(autouse=True)
def _register_fake():
    register_backend(FakeBackend)


def _request(tmp_path, **overrides):
    defaults = dict(
        model="fake/model-1b",
        backend="fake",
        suites=["performance"],
        suite_options=OPTIONS,
        output=tmp_path / "runs" / "result.json",
    )
    return RunRequest(**{**defaults, **overrides})


def test_end_to_end_writes_report(tmp_path):
    outcome = execute(_request(tmp_path))
    assert outcome.report_path == tmp_path / "runs" / "result.json"
    report = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert report["model"]["name"] == "fake/model-1b"
    assert report["configuration"]["configHash"].startswith("sha256:")
    assert report["suites"][0]["suite"] == "performance"
    assert report["suites"][0]["block"] == "A"


def test_report_file_is_bare_and_self_hashing(tmp_path):
    """The file contains the report and nothing else: its hash is the file's hash."""
    outcome = execute(_request(tmp_path))
    report = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert "report" not in report and "reportHash" not in report
    assert file_digest(outcome.report_path) == outcome.report.content_hash()


def test_default_output_is_a_run_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    outcome = execute(_request(tmp_path, output=None))
    assert outcome.report_path == Path("runs") / outcome.report.run_id / "report.json"
    assert outcome.report_path.exists()


def test_incomplete_coverage_is_declared(tmp_path):
    """Only block A was requested: the report cannot say a plain 'evaluated'."""
    report = execute(_request(tmp_path)).report
    assert report.status == "evaluated-partial"
    assert report.coverage["complete"] is False
    assert report.coverage["blocksEvaluated"] == ["A"]
    assert {b["block"] for b in report.coverage["blocksMissing"]} == {"B", "C", "D", "E"}


def test_an_implemented_but_unrequested_block_still_counts_as_not_evaluated(tmp_path):
    """All five suites exist, but what is not run has not been measured."""
    missing = {b["block"]: b for b in execute(_request(tmp_path)).report.coverage["blocksMissing"]}
    assert set(missing) == {"B", "C", "D", "E"}
    assert all(entry["status"] == "not run" for entry in missing.values())


def test_a_planned_but_unimplemented_block_is_labelled_differently(monkeypatch):
    """If a block is ever designed before being implemented, it is distinguished."""
    from opaas import suites

    monkeypatch.setitem(
        suites.PLANNED_SUITES,
        "future",
        {"block": "C", "phase": "Phase 7", "description": "something to come"},
    )
    missing = {b["block"]: b for b in suites.coverage(["performance"])["blocksMissing"]}
    assert missing["C"]["status"] == "not implemented"
    assert missing["C"]["plannedIn"] == "Phase 7"
    assert missing["D"]["status"] == "not run"


def test_report_has_no_aggregate_score(tmp_path):
    """The design is explicit: per-category report, no aggregate score and no tiers."""
    report = execute(_request(tmp_path)).report.payload()
    serialized = json.dumps(report).lower()
    for forbidden in ("overallscore", '"score"', '"tier"', '"grade"'):
        assert forbidden not in serialized


def test_report_hash_is_stable_for_same_content(tmp_path):
    report = execute(_request(tmp_path)).report
    assert report.content_hash() == report.content_hash()


def test_explicit_run_id_is_honoured(tmp_path):
    explicit = execute(_request(tmp_path, run_id="custom-001")).report
    assert explicit.run_id == "custom-001"


def test_limitations_bubble_up_to_report(tmp_path):
    report = execute(_request(tmp_path)).report
    for limitation in report.limitations:
        assert limitation.startswith("[performance]")


def test_backend_is_closed_after_run(tmp_path, monkeypatch):
    created: list[FakeBackend] = []

    def _factory():
        backend = FakeBackend()
        created.append(backend)
        return backend

    monkeypatch.setattr("opaas.runner.get_backend", lambda name: _factory())
    execute(_request(tmp_path))
    assert created[0].closed is True


def test_secrets_never_reach_the_report(tmp_path):
    outcome = execute(_request(tmp_path, backend_config={"api_key": "supersecret"}))
    assert "supersecret" not in outcome.report_path.read_text(encoding="utf-8")


def test_a_cloud_baseline_refuses_the_performance_block(tmp_path):
    """Measuring throughput on a shared endpoint describes no deployment at all."""
    with pytest.raises(ValueError, match="cannot include performance"):
        execute(_request(tmp_path, reference_type="cloud-baseline"))


def test_a_cloud_baseline_refuses_the_compliance_block(tmp_path):
    with pytest.raises(ValueError, match="cannot include compliance"):
        execute(
            _request(
                tmp_path,
                suites=["compliance"],
                reference_type="cloud-baseline",
                suite_options={"compliance": {}},
            )
        )


def test_a_cloud_baseline_runs_the_quality_blocks(tmp_path):
    """That is what it is for: knowing how much quality your local deployment loses."""
    report = execute(
        _request(
            tmp_path,
            suites=["capability"],
            reference_type="cloud-baseline",
            suite_options={"capability": {"dataset": "opaas-smoke", "limit": 1, "concurrency": 1}},
        )
    ).report

    assert report.reference_type == "cloud-baseline"
    assert any("CLOUD REFERENCE" in limitation for limitation in report.limitations)


def test_an_unknown_reference_type_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown referenceType"):
        execute(_request(tmp_path, reference_type="made-up"))


def test_the_default_run_is_on_prem(tmp_path):
    report = execute(_request(tmp_path)).report
    assert report.reference_type == "on-prem"
    assert not any("CLOUD REFERENCE" in lim for lim in report.limitations)


def test_publishable_config_drops_runtime_objects():
    clean = _publishable_config({"transport": object(), "api_key": "x", "api": "chat"})
    assert clean == {"api_key": "***", "api": "chat"}
