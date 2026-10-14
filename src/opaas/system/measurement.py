"""The measurement: one cell of one component's row, in one format for every suite (D60).

Each module writes its own document in its own vocabulary: block D says
`available: false`, `smith` says `measured: false`, `inquest` mixes both, and
none of them can say that a metric does not *apply* to what was audited. Read
side by side they cannot be compared, and a reader of the certificate cannot
tell a gap in the audit from a cell that has no meaning.

A measurement fixes that, and nothing more. It is read from a document, never
written instead of it: the document keeps its bytes and its hash (invariant
4), and the measurement names both.

**Three statuses, each with what it needs:**

- `measured` carries a `value`.
- `not-measured` carries a `reason`: it applies, and could not be measured
  here. A zero is never written in its place (invariant 2).
- `not-applicable` carries a `reason`: it does not apply to this component,
  such as the auditability of a model, which keeps no record of its own.

**Values are typed**, so a rate always travels with its interval (invariant
11) and a latency with its unit. A value is never combined with another
measurement's (invariant 1): there is no field to put a combination in.

A field the producer did not have is absent from the record, never defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUSES = ("measured", "not-measured", "not-applicable")

#: The two axes and their fixed dimensions. Which suite fills a cell depends on
#: the component's kind; these names do not (system-audit.md §3).
BEHAVIOUR = ("performance", "capability", "consistency", "safety", "compliance")
#: The five dimensions of Auditable Agents, named as the card and the gap rules
#: already name them, plus Q6, which the card measures from degraded audits.
AUDITABILITY = (
    "actionRecoverability",
    "lifecycleCoverage",
    "policyCheckability",
    "responsibilityAttribution",
    "evidenceIntegrity",
    "degradation",
)
AXES = {"behaviour": BEHAVIOUR, "auditability": AUDITABILITY}

VALUE_TYPES = ("rate", "number", "distribution", "verdict")
DIRECTIONS = ("higher-is-better", "lower-is-better")


class MeasurementError(ValueError):
    """A measurement record is malformed."""


@dataclass(frozen=True, slots=True)
class Measurement:
    component: str
    axis: str
    dimension: str
    #: What is measured, in words a reader of the certificate understands
    #: ("bias · Age", "edge agent:a → tool:search").
    aspect: str
    #: The metric's key in the source document, so the value can be found there.
    metric: str
    status: str
    #: Where it was read from: {kind, file, hash}.
    source: dict[str, Any]
    value: dict[str, Any] | None = None
    reason: str | None = None
    direction: str | None = None
    environment: dict[str, Any] | None = None
    #: Anything the source adds that a reader needs next to the value (a
    #: classifier's threshold, the concurrency of a load test).
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check(self.to_record())

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "component": self.component,
            "axis": self.axis,
            "dimension": self.dimension,
            "aspect": self.aspect,
            "metric": self.metric,
            "status": self.status,
            "source": self.source,
        }
        for name, value in (
            ("value", self.value),
            ("reason", self.reason),
            ("direction", self.direction),
            ("environment", self.environment),
            ("detail", self.detail or None),
        ):
            if value is not None:
                record[name] = value
        return record


def check(record: dict[str, Any]) -> None:
    """Raises MeasurementError naming what is wrong with a record."""
    where = f"{record.get('component')}/{record.get('metric')}"
    axis, dimension = record.get("axis"), record.get("dimension")
    if axis not in AXES:
        raise MeasurementError(f"{where}: axis must be one of {', '.join(AXES)}, got {axis!r}.")
    if dimension not in AXES[axis]:
        raise MeasurementError(
            f"{where}: {dimension!r} is not a dimension of the {axis} axis "
            f"({', '.join(AXES[axis])})."
        )
    status = record.get("status")
    if status not in STATUSES:
        raise MeasurementError(f"{where}: status must be one of {', '.join(STATUSES)}.")
    for name in ("component", "aspect", "metric"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise MeasurementError(f"{where}: '{name}' must be a non-empty string.")
    source = record.get("source")
    if not isinstance(source, dict) or not source.get("kind"):
        raise MeasurementError(f"{where}: 'source' must name the document kind it came from.")
    if record.get("direction") not in (None, *DIRECTIONS):
        raise MeasurementError(f"{where}: direction must be one of {', '.join(DIRECTIONS)}.")

    if status == "measured":
        if "reason" in record:
            raise MeasurementError(f"{where}: a measured cell has a value, not a reason.")
        _check_value(where, record.get("value"))
    else:
        if "value" in record:
            raise MeasurementError(
                f"{where}: a {status} cell carries no value: a zero there would read as "
                "measured (invariant 2)."
            )
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise MeasurementError(f"{where}: a {status} cell must say why.")


def _check_value(where: str, value: Any) -> None:
    if not isinstance(value, dict) or value.get("type") not in VALUE_TYPES:
        raise MeasurementError(
            f"{where}: a measured value must be an object with type one of "
            f"{', '.join(VALUE_TYPES)}."
        )
    kind = value["type"]
    if kind == "rate":
        # Invariant 11 compares intervals: a rate without one cannot be compared.
        for name in ("point", "low", "high", "n"):
            if not isinstance(value.get(name), int | float) or isinstance(value[name], bool):
                raise MeasurementError(f"{where}: a rate needs point, low, high and n.")
    elif kind == "number":
        if not isinstance(value.get("value"), int | float) or not value.get("unit"):
            raise MeasurementError(f"{where}: a number needs a value and its unit.")
    elif kind == "distribution":
        if not value.get("unit") or not any(
            isinstance(value.get(p), int | float) for p in ("p50", "point")
        ):
            raise MeasurementError(f"{where}: a distribution needs a centre and its unit.")
    elif not isinstance(value.get("verdict"), str) or not value["verdict"]:
        raise MeasurementError(f"{where}: a verdict needs its word.")


def rate(entry: dict[str, Any]) -> dict[str, Any] | None:
    """A Wilson interval as OPAAS writes it, as a rate value; None if it holds no count."""
    if not isinstance(entry, dict) or not entry.get("n"):
        return None
    if entry.get("point") is None or entry.get("low") is None or entry.get("high") is None:
        return None
    value = {"type": "rate", **{k: entry[k] for k in ("point", "low", "high", "n")}}
    if isinstance(entry.get("count"), int):
        value["count"] = entry["count"]
    return value


def number(value: Any, unit: str) -> dict[str, Any] | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return {"type": "number", "value": value, "unit": unit}


def distribution(entry: dict[str, Any], unit: str) -> dict[str, Any] | None:
    """A summary (p50/p95/p99) or a median interval (point/low/high), with its unit."""
    if not isinstance(entry, dict):
        return None
    keys = ("count", "min", "mean", "p50", "p95", "p99", "max", "point", "low", "high", "n")
    kept = {k: entry[k] for k in keys if isinstance(entry.get(k), int | float)}
    if not any(k in kept for k in ("p50", "point")):
        return None
    return {"type": "distribution", "unit": unit, **kept}


def verdict(word: str, **extra: Any) -> dict[str, Any]:
    return {"type": "verdict", "verdict": word, **extra}


__all__ = [
    "AUDITABILITY",
    "AXES",
    "BEHAVIOUR",
    "DIRECTIONS",
    "STATUSES",
    "VALUE_TYPES",
    "Measurement",
    "MeasurementError",
    "check",
    "distribution",
    "number",
    "rate",
    "verdict",
]
