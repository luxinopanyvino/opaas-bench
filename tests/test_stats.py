import pytest

from opaas_bench.stats import mean, percentile, summarize


def test_percentile_interpolates_linearly():
    data = [1, 2, 3, 4]
    assert percentile(data, 0) == 1
    assert percentile(data, 50) == 2.5
    assert percentile(data, 100) == 4


def test_percentile_single_value():
    assert percentile([7.5], 99) == 7.5


def test_percentile_empty_is_none():
    assert percentile([], 50) is None


def test_percentile_rejects_out_of_range():
    with pytest.raises(ValueError):
        percentile([1, 2], 101)


def test_summarize_shape_and_values():
    summary = summarize([10, 20, 30])
    assert summary["count"] == 3
    assert summary["mean"] == 20
    assert summary["min"] == 10
    assert summary["max"] == 30
    assert summary["p50"] == 20


def test_summarize_empty():
    summary = summarize([])
    assert summary["count"] == 0
    assert summary["p95"] is None


def test_mean_ignores_none():
    assert mean([1, None, 3]) == 2
