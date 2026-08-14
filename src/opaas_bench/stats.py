"""Minimal statistics shared by the suites."""

from __future__ import annotations

import math
from collections.abc import Iterable


def percentile(values: Iterable[float], p: float) -> float | None:
    """Percentile by linear interpolation (same convention as numpy `linear`).

    `p` in [0, 100]. Returns None if there are no values.
    """
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"Percentile out of range: {p}")
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * (p / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(data) - 1)
    weight = position - lower
    return data[lower] * (1 - weight) + data[upper] * weight


def mean(values: Iterable[float]) -> float | None:
    data = [v for v in values if v is not None]
    return sum(data) / len(data) if data else None


def summarize(values: Iterable[float], digits: int = 4) -> dict[str, float | int | None]:
    """p50/p95/p99 summary used across every latency metric."""
    data = [v for v in values if v is not None]
    if not data:
        return {"count": 0, "mean": None, "min": None, "p50": None, "p95": None, "p99": None,
                "max": None}
    return {
        "count": len(data),
        "mean": _round(mean(data), digits),
        "min": _round(min(data), digits),
        "p50": _round(percentile(data, 50), digits),
        "p95": _round(percentile(data, 95), digits),
        "p99": _round(percentile(data, 99), digits),
        "max": _round(max(data), digits),
    }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


#: z for a two-sided 95% confidence interval.
_Z95 = 1.959963984540054


def wilson_interval(successes: int, total: int, z: float = _Z95) -> dict[str, float | None]:
    """Wilson interval for a proportion.

    With small subsets — which is all of block B — reporting "62% accuracy"
    without an interval is misleading: at n=50 that 62% is compatible with
    anything between 48% and 74%. The interval goes in the report next to the
    score.

    Wilson is used instead of the normal approximation because the latter
    breaks down exactly where it is needed most: small n and proportions close
    to 0 or 1.
    """
    if total <= 0:
        return {"point": None, "low": None, "high": None, "n": 0}
    p = successes / total
    denominator = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
    return {
        "point": round(p, 4),
        "low": round(max(0.0, center - margin), 4),
        "high": round(min(1.0, center + margin), 4),
        "n": total,
    }
