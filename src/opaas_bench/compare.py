"""Comparison across runs: block C's variance between quantizations.

FP16 and Q4 of the same model do not fit on one GPU at the same time, so they
are not compared within a run but across runs. Each configuration is evaluated
separately and the reports are cross-referenced here.

The rule that governs the comparison: **a difference whose confidence
intervals overlap is not a difference**. Saying "Q4 loses 3 points against
FP16" when both intervals span 60% to 80% is inventing precision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opaas_bench.hashing import file_digest, short_hash


class ComparisonError(ValueError):
    """The reports cannot be compared."""


@dataclass(slots=True)
class LoadedReport:
    path: Path
    report: dict[str, Any]
    digest: str
    #: Already-disambiguated label; filled in by `compare()`.
    display_label: str = ""

    @property
    def is_baseline(self) -> bool:
        return self.report.get("referenceType") == "cloud-baseline"

    @property
    def label(self) -> str:
        model = self.report.get("model", {})
        quantization = model.get("quantization") or "no declared quantization"
        suffix = " [cloud reference]" if self.is_baseline else ""
        return f"{model.get('name')} · {quantization}{suffix}"

    def suite(self, name: str) -> dict[str, Any] | None:
        for result in self.report.get("suites", []):
            if result.get("suite") == name and result.get("status") == "completed":
                return result.get("metrics", {})
        return None


def load_reports(paths: list[Path]) -> list[LoadedReport]:
    if len(paths) < 2:
        raise ComparisonError("At least two reports are needed to compare")
    reports = []
    for path in paths:
        if not path.exists():
            raise ComparisonError(f"Report does not exist: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ComparisonError(f"Invalid JSON in {path}: {exc}") from None
        if "runId" not in payload:
            raise ComparisonError(f"{path} does not look like an OPAAS-Bench report")
        reports.append(LoadedReport(path=path, report=payload, digest=file_digest(path)))
    return reports


def _distinct_labels(reports: list[LoadedReport]) -> list[str]:
    """Readable labels and, above all, labels distinct from each other.

    Comparing two configurations of the same model — the same quantization
    with a different prompt, for instance — produces identical labels, and a
    table with two identical columns does not say which is which. When they
    collide the tie is broken with the config hash, which is exactly what sets
    them apart.
    """
    labels = [report.label for report in reports]
    return [
        f"{label} ({short_hash(str(report.report.get('configuration', {}).get('configHash', '')))})"
        if labels.count(label) > 1
        else label
        for label, report in zip(labels, reports, strict=True)
    ]


def compare(reports: list[LoadedReport]) -> dict[str, Any]:
    """Cross-references the reports and flags which differences are significant."""
    labels = _distinct_labels(reports)
    for report, label in zip(reports, labels, strict=True):
        report.display_label = label

    return {
        "opaasBenchComparison": "1.0",
        "baseline": labels[0],
        "runs": [
            {
                "runId": r.report.get("runId"),
                "label": label,
                "model": r.report.get("model", {}).get("name"),
                "quantization": r.report.get("model", {}).get("quantization"),
                "backend": r.report.get("configuration", {}).get("backend"),
                "hardware": r.report.get("configuration", {}).get("hardware"),
                "reportHash": r.digest,
                "source": str(r.path),
            }
            for r, label in zip(reports, labels, strict=True)
        ],
        "warnings": _comparability_warnings(reports),
        "performance": _compare_performance(reports),
        "capability": _compare_capability(reports),
        "consistency": _compare_consistency(reports),
    }


def _comparability_warnings(reports: list[LoadedReport]) -> list[str]:
    """Warns when things that are not comparable are being compared."""
    warnings: list[str] = []

    baselines = [r for r in reports if r.is_baseline]
    if baselines and len(baselines) < len(reports):
        warnings.append(
            f"{len(baselines)} of {len(reports)} reports are CLOUD REFERENCES. Their quality "
            "is comparable — that is what they are for: they tell you how much you lose by "
            "deploying locally — but their performance figures describe no deployment of yours."
        )

    hardware = {
        r.report.get("configuration", {}).get("hardware") for r in reports if not r.is_baseline
    }
    if len(hardware) > 1:
        warnings.append(
            "The reports come from different hardware: the performance metrics are not "
            f"comparable with each other ({', '.join(sorted(str(h) for h in hardware))})."
        )
    backends = {r.report.get("configuration", {}).get("backend") for r in reports}
    if len(backends) > 1:
        warnings.append(
            "Different inference engines: part of the difference comes from the engine, not "
            f"from the model ({', '.join(sorted(str(b) for b in backends))})."
        )

    datasets = {
        metrics.get("dataset", {}).get("datasetHash")
        for r in reports
        if (metrics := r.suite("capability"))
    }
    if len(datasets) > 1:
        warnings.append(
            "The reports use different datasets in block B: their scores cannot be "
            "compared. Re-evaluate with the same dataset."
        )

    styles = {
        metrics.get("answerStyle")
        for r in reports
        if (metrics := r.suite("capability"))
    }
    if len(styles) > 1:
        listed = ", ".join(sorted(map(str, styles)))
        warnings.append(
            f"Different answer styles in block B ({listed}): not comparable."
        )
    return warnings


def _compare_performance(reports: list[LoadedReport]) -> list[dict[str, Any]]:
    """Throughput, TTFT, VRAM and energy per equivalent scenario."""
    rows: list[dict[str, Any]] = []
    scenario_keys: list[tuple[int, int]] = []
    per_run: list[dict[tuple[int, int], dict[str, Any]]] = []

    for report in reports:
        # A cloud baseline contributes no rows to block A: its numbers would come
        # from someone else's shared hardware, and putting them in the same table
        # would imply they are comparable.
        metrics = {} if report.is_baseline else (report.suite("performance") or {})
        indexed = {
            (s["promptWords"], s["concurrency"]): s for s in metrics.get("scenarios", [])
        }
        per_run.append(indexed)
        for key in indexed:
            if key not in scenario_keys:
                scenario_keys.append(key)

    for words, concurrency in sorted(scenario_keys):
        entry: dict[str, Any] = {"promptWords": words, "concurrency": concurrency, "runs": []}
        for report, indexed in zip(reports, per_run, strict=True):
            scenario = indexed.get((words, concurrency))
            if scenario is None:
                entry["runs"].append(
                    {"label": report.display_label or report.label, "measured": False}
                )
                continue
            entry["runs"].append(
                {
                    "label": report.display_label or report.label,
                    "measured": True,
                    "outputTokensPerS": scenario["throughput"]["outputTokensPerS"],
                    "ttftP50S": scenario["ttftS"]["p50"],
                    "vramPeakMb": scenario["resources"].get("vramPeakMb"),
                    "whPerMillionTokens": scenario["energy"].get("whPerMillionTokens"),
                }
            )
        rows.append(entry)
    return rows


def _compare_capability(reports: list[LoadedReport]) -> list[dict[str, Any]]:
    """Accuracy per category, with a statistical significance verdict."""
    categories: list[str] = []
    per_run: list[dict[str, dict[str, Any]]] = []

    for report in reports:
        metrics = report.suite("capability") or {}
        indexed = {task["category"]: task for task in metrics.get("tasks", [])}
        per_run.append(indexed)
        for category in indexed:
            if category not in categories:
                categories.append(category)

    rows = []
    for category in categories:
        measurements = []
        for report, indexed in zip(reports, per_run, strict=True):
            task = indexed.get(category)
            measurements.append(
                {
                    "label": report.display_label or report.label,
                    "accuracy": task["accuracy"] if task else None,
                    "items": task["items"] if task else 0,
                }
            )
        rows.append(
            {
                "category": category,
                "runs": measurements,
                "verdict": _significance(measurements),
            }
        )
    return rows


def _compare_consistency(reports: list[LoadedReport]) -> list[dict[str, Any]]:
    rows = []
    for report in reports:
        metrics = report.suite("consistency")
        if metrics is None:
            rows.append({"label": report.display_label or report.label, "measured": False})
            continue
        determinism = metrics.get("determinism", {})
        context = metrics.get("context", {})
        rows.append(
            {
                "label": report.display_label or report.label,
                "measured": True,
                "identicalRate": determinism.get("identicalRate", {}).get("point"),
                "distinctOutputs": determinism.get("distinctOutputs"),
                "largestVerifiedContextWords": context.get("largestVerifiedWords"),
                "contextLossDetected": context.get("contextLossDetected"),
                "silentTruncations": metrics.get("robustness", {}).get("silentTruncations"),
            }
        )
    return rows


def _significance(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """There is a difference only if the confidence intervals do not overlap."""
    usable = [
        m
        for m in measurements
        if m["accuracy"] and m["accuracy"].get("point") is not None
    ]
    if len(usable) < 2:
        return {"comparable": False, "detail": "There are not two measurements to compare"}

    best = max(usable, key=lambda m: m["accuracy"]["point"])
    worst = min(usable, key=lambda m: m["accuracy"]["point"])
    gap = best["accuracy"]["point"] - worst["accuracy"]["point"]

    if best["accuracy"]["low"] > worst["accuracy"]["high"]:
        return {
            "comparable": True,
            "significant": True,
            "detail": (
                f"{best['label']} beats {worst['label']} by {gap:.1%} and the confidence "
                "intervals do not overlap: the difference is real."
            ),
        }
    return {
        "comparable": True,
        "significant": False,
        "detail": (
            f"The observed difference is {gap:.1%}, but the confidence intervals overlap: "
            "with this sample there is no basis to claim a difference. Enlarge the dataset "
            "if you need to tell them apart."
        ),
    }
