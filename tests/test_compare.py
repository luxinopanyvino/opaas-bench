import json

import pytest

from opaas.bench.compare import ComparisonError, compare, load_reports


def _report(tmp_path, name, *, quantization, accuracy, hardware="1x RTX 4090 24GB", **overrides):
    """Minimal report with what the comparator looks at."""
    payload = {
        "runId": f"2026-{name}",
        "status": "evaluated",
        "model": {"name": "Vega-7B", "quantization": quantization},
        "configuration": {"backend": "vLLM 0.9.1", "hardware": hardware},
        "suites": [
            {
                "suite": "performance",
                "status": "completed",
                "metrics": {
                    "scenarios": [
                        {
                            "promptWords": 128,
                            "concurrency": 1,
                            "throughput": {"outputTokensPerS": overrides.get("tps", 50.0)},
                            "ttftS": {"p50": 0.1},
                            "resources": {"vramPeakMb": overrides.get("vram", 14000)},
                            "energy": {"whPerMillionTokens": 400.0},
                        }
                    ]
                },
            },
            {
                "suite": "capability",
                "status": "completed",
                "metrics": {
                    "answerStyle": overrides.get("style", "cot"),
                    "dataset": {"datasetHash": overrides.get("dataset_hash", "sha256:abc")},
                    "tasks": [
                        {
                            "category": "reasoning",
                            "items": accuracy[2],
                            "accuracy": {
                                "point": accuracy[0],
                                "low": accuracy[1],
                                "high": accuracy[2] if len(accuracy) < 4 else accuracy[3],
                                "n": 200,
                            },
                        }
                    ],
                },
            },
        ],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_at_least_two_reports_are_required(tmp_path):
    path = _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.75, 200, 0.85))
    with pytest.raises(ComparisonError, match="At least two"):
        load_reports([path])


def test_a_file_that_is_not_a_report_is_rejected(tmp_path):
    bad = tmp_path / "whatever.json"
    bad.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    good = _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.75, 200, 0.85))
    with pytest.raises(ComparisonError, match="does not look like an OPAAS report"):
        load_reports([good, bad])


def test_identical_configurations_get_distinguishable_labels(tmp_path):
    """Comparing two configurations of the same model used to give two identical columns."""
    paths = [
        _report(tmp_path, "a", quantization="Q4", accuracy=(0.52, 0.44, 200, 0.60)),
        _report(tmp_path, "b", quantization="Q4", accuracy=(0.69, 0.62, 200, 0.76)),
    ]
    # Same model and quantization, different configuration.
    for path, config_hash in zip(paths, ("sha256:aaaa1111", "sha256:bbbb2222"), strict=True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["configuration"]["configHash"] = config_hash
        path.write_text(json.dumps(payload), encoding="utf-8")

    labels = [run["label"] for run in compare(load_reports(paths))["runs"]]
    assert labels[0] != labels[1]
    assert "aaaa1111" in labels[0] and "bbbb2222" in labels[1]


def test_distinct_configurations_keep_the_readable_label(tmp_path):
    paths = [
        _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9)),
        _report(tmp_path, "b", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9)),
    ]
    labels = [run["label"] for run in compare(load_reports(paths))["runs"]]
    assert labels == ["Vega-7B · FP16", "Vega-7B · Q4"]


def test_runs_are_labelled_by_quantization(tmp_path):
    paths = [
        _report(tmp_path, "fp16", quantization="FP16", accuracy=(0.80, 0.74, 200, 0.86)),
        _report(tmp_path, "q4", quantization="GGUF Q4_K_M", accuracy=(0.78, 0.72, 200, 0.84)),
    ]
    comparison = compare(load_reports(paths))
    assert [run["quantization"] for run in comparison["runs"]] == ["FP16", "GGUF Q4_K_M"]
    assert all(run["reportHash"].startswith("sha256:") for run in comparison["runs"])


def test_overlapping_intervals_are_not_called_a_difference(tmp_path):
    """The comparator's core: 2 points with overlapping intervals is not a difference."""
    paths = [
        _report(tmp_path, "fp16", quantization="FP16", accuracy=(0.80, 0.74, 200, 0.86)),
        _report(tmp_path, "q4", quantization="Q4", accuracy=(0.78, 0.72, 200, 0.84)),
    ]
    verdict = compare(load_reports(paths))["capability"][0]["verdict"]
    assert verdict["comparable"] is True
    assert verdict["significant"] is False
    assert "intervals overlap" in verdict["detail"]


def test_separated_intervals_are_called_a_real_difference(tmp_path):
    paths = [
        _report(tmp_path, "fp16", quantization="FP16", accuracy=(0.90, 0.86, 200, 0.94)),
        _report(tmp_path, "q2", quantization="Q2", accuracy=(0.50, 0.44, 200, 0.56)),
    ]
    verdict = compare(load_reports(paths))["capability"][0]["verdict"]
    assert verdict["significant"] is True
    assert "do not overlap" in verdict["detail"]


def test_performance_rows_line_up_by_scenario(tmp_path):
    paths = [
        _report(tmp_path, "fp16", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9), tps=40.0),
        _report(tmp_path, "q4", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9), tps=95.0),
    ]
    row = compare(load_reports(paths))["performance"][0]
    assert row["promptWords"] == 128
    assert [entry["outputTokensPerS"] for entry in row["runs"]] == [40.0, 95.0]


def test_different_hardware_is_flagged_as_not_comparable(tmp_path):
    paths = [
        _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9)),
        _report(
            tmp_path, "b", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9),
            hardware="1x A100 80GB",
        ),
    ]
    warnings = compare(load_reports(paths))["warnings"]
    assert any("different hardware" in warning for warning in warnings)


def test_different_datasets_invalidate_the_quality_comparison(tmp_path):
    paths = [
        _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9)),
        _report(
            tmp_path, "b", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9),
            dataset_hash="sha256:other",
        ),
    ]
    warnings = compare(load_reports(paths))["warnings"]
    assert any("different datasets" in warning for warning in warnings)


def test_different_answer_styles_are_flagged(tmp_path):
    paths = [
        _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9)),
        _report(
            tmp_path, "b", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9), style="direct"
        ),
    ]
    assert any("Different answer styles" in w for w in compare(load_reports(paths))["warnings"])


def test_a_run_without_consistency_is_reported_as_not_measured(tmp_path):
    paths = [
        _report(tmp_path, "a", quantization="FP16", accuracy=(0.8, 0.7, 200, 0.9)),
        _report(tmp_path, "b", quantization="Q4", accuracy=(0.8, 0.7, 200, 0.9)),
    ]
    assert all(row["measured"] is False for row in compare(load_reports(paths))["consistency"])
