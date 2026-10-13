"""Admission rules: what a public registry accepts, flags and refuses.

A leaderboard fed by whoever wants to look good is only worth its criteria, so
each rule is pinned here with the reason it exists rather than just its effect.
"""

from __future__ import annotations

import pytest

from opaas.bench.registry import ACCEPTED, FLAGGED, MAX_RANKABLE_INTERVAL, REJECTED, admit


def _report(**overrides):
    """A report that would be admitted, so each test changes exactly one thing."""
    base = {
        "runId": "20260101-001",
        "status": "evaluated",
        "referenceType": "on-prem",
        "configuration": {"backendMetadata": {"promptMode": "chat"}},
        "coverage": {"complete": True, "blocksEvaluated": ["A", "B", "C", "D", "E"]},
        "suites": [
            {
                "suite": "capability",
                "status": "completed",
                "metrics": {
                    "dataset": {
                        "name": "ifeval+xstest",
                        "datasetHash": "sha256:known",
                        "syntheticSmoke": False,
                    },
                    "options": {"limit": None},
                    "tasks": [
                        {
                            "category": "instruction-following",
                            "accuracy": {"point": 0.69, "low": 0.653, "high": 0.732, "n": 510},
                        }
                    ],
                },
            }
        ],
    }
    base.update(overrides)
    return base


def _rules(report, **kwargs):
    return {finding.rule for finding in admit(report, **kwargs).findings}


# ------------------------------------------------------------------ accepted


def test_a_clean_report_is_admitted_without_findings():
    result = admit(_report())
    assert result.verdict == ACCEPTED
    assert result.findings == []


def test_accepted_says_nothing_about_the_figures_being_good():
    """A 3% accuracy with a tight interval is admissible and terrible."""
    report = _report()
    report["suites"][0]["metrics"]["tasks"][0]["accuracy"] = {
        "point": 0.03, "low": 0.02, "high": 0.05, "n": 5000
    }
    assert admit(report).verdict == ACCEPTED


# ------------------------------------------------------------------ rejected


def test_a_smoke_dataset_cannot_be_ranked():
    """The report already declares it is not a measurement."""
    report = _report()
    report["suites"][0]["metrics"]["dataset"]["syntheticSmoke"] = True
    result = admit(report)
    assert result.verdict == REJECTED
    assert result.rejected
    assert "smoke-dataset" in _rules(report)


def test_a_cloud_baseline_describes_no_deployment():
    assert admit(_report(referenceType="cloud-baseline")).verdict == REJECTED


def test_a_failed_suite_rejects_the_whole_report():
    assert admit(_report(status="partial")).verdict == REJECTED


def test_raw_prompt_mode_is_measuring_something_else():
    """Without the chat template: 52% against 69% on the same model (D25)."""
    report = _report(configuration={"backendMetadata": {"promptMode": "raw"}})
    assert admit(report).verdict == REJECTED
    assert "raw-prompt-mode" in _rules(report)


def test_a_rejection_names_every_reason_not_just_the_first():
    report = _report(referenceType="cloud-baseline", status="partial")
    assert {"cloud-baseline", "suite-failed"} <= _rules(report)


# ------------------------------------------------------------------- flagged


def test_partial_coverage_is_comparable_only_where_it_covers():
    report = _report(
        status="evaluated-partial",
        coverage={"complete": False, "blocksMissing": [{"block": "D"}, {"block": "E"}]},
    )
    result = admit(report)
    assert result.verdict == FLAGGED
    detail = next(f.detail for f in result.findings if f.rule == "partial-coverage")
    assert "D, E" in detail


def test_a_trimmed_sample_is_flagged():
    report = _report()
    report["suites"][0]["metrics"]["options"]["limit"] = 50
    assert "trimmed-sample" in _rules(report)


def test_an_interval_too_wide_to_order_two_submissions():
    """Ranking on a rate whose interval swallows the difference is theatre."""
    report = _report()
    report["suites"][0]["metrics"]["tasks"][0]["accuracy"] = {
        "point": 0.69, "low": 0.56, "high": 0.81, "n": 50
    }
    result = admit(report)
    assert result.verdict == FLAGGED
    assert "interval-too-wide" in _rules(report)


def test_an_interval_at_the_threshold_still_ranks():
    report = _report()
    report["suites"][0]["metrics"]["tasks"][0]["accuracy"] = {
        "point": 0.69, "low": 0.65, "high": 0.65 + MAX_RANKABLE_INTERVAL, "n": 400
    }
    assert admit(report).verdict == ACCEPTED


def test_an_unknown_dataset_is_flagged_only_when_the_registry_has_a_list():
    report = _report()
    assert "unrecognised-dataset" not in _rules(report)
    assert "unrecognised-dataset" in _rules(report, known_dataset_hashes={"sha256:other"})
    assert "unrecognised-dataset" not in _rules(report, known_dataset_hashes={"sha256:known"})


def test_unmeasured_dimensions_must_travel_with_the_row():
    """Absent, not zero: a row that hides them reads as measured and bad."""
    report = _report()
    report["suites"][0]["metrics"]["notImplemented"] = [{"dimension": "bias"}]
    result = admit(report)
    assert "unmeasured-dimensions" in _rules(report)
    detail = next(f.detail for f in result.findings if f.rule == "unmeasured-dimensions")
    assert "not zero" in detail


def test_an_unavailable_classifier_counts_as_unmeasured():
    report = _report()
    report["suites"][0]["metrics"]["toxicity"] = {"available": False, "task": "toxicity"}
    assert "unmeasured-dimensions" in _rules(report)


# -------------------------------------------------------------------- shape


def test_a_rejection_outranks_any_number_of_flags():
    report = _report(referenceType="cloud-baseline")
    report["suites"][0]["metrics"]["options"]["limit"] = 10
    assert admit(report).verdict == REJECTED


def test_metrics_from_a_failed_suite_are_not_read():
    """A suite that did not complete has nothing trustworthy to inspect."""
    report = _report()
    report["suites"][0]["status"] = "failed"
    report["suites"][0]["metrics"]["dataset"]["syntheticSmoke"] = True
    assert admit(report).verdict == ACCEPTED


@pytest.mark.parametrize("payload", [{}, {"suites": []}, {"suites": [{"status": "completed"}]}])
def test_a_report_missing_everything_does_not_crash(payload):
    """Submissions arrive from strangers: malformed is not an exception."""
    assert admit(payload).verdict in (ACCEPTED, FLAGGED, REJECTED)


def test_the_verdict_serialises_for_a_submission_pipeline():
    payload = admit(_report(referenceType="cloud-baseline")).as_dict()
    assert payload["verdict"] == REJECTED
    assert payload["findings"][0]["rule"] == "cloud-baseline"
    assert set(payload["findings"][0]) == {"rule", "verdict", "detail"}
