"""Admission rules for a public registry of reports.

A leaderboard is worth exactly what its admission criteria are worth. Fed
submissions from whoever wants to look good — hardware vendors benchmarking
their own configurations, most of all — a registry that accepts everything
ranks nothing: the top row goes to whoever tuned hardest against the
measurement.

The defence is already in the report. Every setting that changes what a figure
means is recorded there, so a third party can decide **mechanically** whether a
submission is comparable with the others, without trusting the submitter and
without reading it by hand. This module is that decision.

It lives in code rather than in prose for the reason D22 gives about the cloud
baseline: a rule written in the documentation gets broken, one the code rejects
does not. It is also why a submitter can run it on their own report before
submitting — the criteria are not a secret, they are the product.

Three verdicts:

* **rejected** — the report does not describe something that can be ranked at
  all. This is not a judgement about the deployment: a smoke dataset or a cloud
  baseline are perfectly valid runs that simply say nothing about how one
  deployment compares with another.
* **flagged** — comparable, with a caveat that has to travel with the row.
* **accepted** — nothing found. Never "good": this module says nothing about
  the figures themselves, only about whether they can sit in a table next to
  someone else's.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

#: A rate whose 95% interval is wider than this cannot separate two
#: submissions, so ranking on it is theatre. At 69% accuracy it corresponds to
#: roughly 300 items; below that the interval swallows the differences a
#: leaderboard exists to show.
MAX_RANKABLE_INTERVAL = 0.10

REJECTED, FLAGGED, ACCEPTED = "rejected", "flagged", "accepted"


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule that fired, and why it matters."""

    rule: str
    verdict: str
    detail: str


@dataclass(slots=True)
class Admission:
    verdict: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.verdict == REJECTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [
                {"rule": f.rule, "verdict": f.verdict, "detail": f.detail} for f in self.findings
            ],
        }


def admit(report: dict[str, Any], *, known_dataset_hashes: set[str] | None = None) -> Admission:
    """Decides whether a report can join a public comparison, and says why not."""
    findings = [
        *_disqualifying(report),
        *_caveats(report, known_dataset_hashes or set()),
    ]
    if any(finding.verdict == "reject" for finding in findings):
        return Admission(REJECTED, findings)
    return Admission(FLAGGED if findings else ACCEPTED, findings)


def _disqualifying(report: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = []

    if report.get("referenceType") == "cloud-baseline":
        found.append(
            Finding(
                "cloud-baseline",
                "reject",
                "A baseline measures somebody else's endpoint under their conditions. It "
                "describes no deployment, which is why it cannot be signed either.",
            )
        )

    if report.get("status") == "partial":
        found.append(
            Finding(
                "suite-failed",
                "reject",
                "A suite failed during the run: the report is incomplete for reasons that "
                "have nothing to do with the deployment's quality.",
            )
        )

    for metrics in _suite_metrics(report):
        if (metrics.get("dataset") or {}).get("syntheticSmoke"):
            found.append(
                Finding(
                    "smoke-dataset",
                    "reject",
                    "Evaluated against a smoke dataset, which the report itself declares is "
                    "not a measurement and must not be certified.",
                )
            )
            break

    metadata = (report.get("configuration") or {}).get("backendMetadata") or {}
    if metadata.get("promptMode") == "raw":
        found.append(
            Finding(
                "raw-prompt-mode",
                "reject",
                "Without the chat template the model completes text instead of following "
                "instructions. Measured on the same model: 52% against 69% on IFEval, and "
                "15.6% against 75% on refusal of harmful requests.",
            )
        )

    return found


def _caveats(report: dict[str, Any], known_hashes: set[str]) -> list[Finding]:
    found: list[Finding] = []

    if report.get("status") == "evaluated-partial":
        missing = ", ".join(
            str(block.get("block"))
            for block in (report.get("coverage") or {}).get("blocksMissing", [])
        )
        found.append(
            Finding(
                "partial-coverage",
                "flag",
                f"Not all five blocks ran: {missing or 'coverage incomplete'}. The row can "
                "only be compared on the blocks it covers.",
            )
        )

    for metrics in _suite_metrics(report):
        if limit := (metrics.get("options") or {}).get("limit"):
            found.append(
                Finding(
                    "trimmed-sample",
                    "flag",
                    f"Each task was trimmed to {limit} items, so the intervals describe that "
                    "sample and not the dataset.",
                )
            )
            break

    if known_hashes:
        for metrics in _suite_metrics(report):
            dataset = metrics.get("dataset") or {}
            digest = dataset.get("datasetHash")
            if digest and digest not in known_hashes:
                found.append(
                    Finding(
                        "unrecognised-dataset",
                        "flag",
                        f"Evaluated against {dataset.get('name')!r}, whose hash is not one the "
                        "registry knows. Valid, and not comparable with rows that used a "
                        "different one.",
                    )
                )
                break

    for name, interval in _rates(report):
        width = float(interval["high"]) - float(interval["low"])
        if width > MAX_RANKABLE_INTERVAL:
            found.append(
                Finding(
                    "interval-too-wide",
                    "flag",
                    f"{name} carries a 95% interval {width * 100:.1f} points wide "
                    f"(n = {interval.get('n')}). Two submissions cannot be ordered on it.",
                )
            )

    if unmeasured := _unmeasured(report):
        found.append(
            Finding(
                "unmeasured-dimensions",
                "flag",
                f"Declared unmeasured: {', '.join(unmeasured)}. Absent, not zero — the row "
                "must not read as if those had been measured and come out badly.",
            )
        )

    return found


def _suite_metrics(report: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for result in report.get("suites") or []:
        if result.get("status") == "completed":
            yield result.get("metrics") or {}


def _rates(report: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every published rate that carries a confidence interval."""
    for metrics in _suite_metrics(report):
        for task in metrics.get("tasks") or []:
            for key in ("accuracy", "rate"):
                if _has_interval(task.get(key)):
                    yield f"{task.get('category') or task.get('name')} {key}", task[key]
        for key, entry in (metrics.get("safetyBalance") or {}).items():
            if _has_interval(entry):
                yield key, entry


def _has_interval(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("low"), int | float)
        and isinstance(value.get("high"), int | float)
    )


def _unmeasured(report: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for metrics in _suite_metrics(report):
        for entry in metrics.get("notImplemented") or []:
            names.append(str(entry.get("dimension")))
        for key in ("faithfulness", "toxicity"):
            measure = metrics.get(key) or {}
            if measure and not measure.get("available"):
                names.append(str(measure.get("task", key)))
    return names
