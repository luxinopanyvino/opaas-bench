"""Orchestration of a run: steps 1–3 of the end-to-end flow.

    1. Model + config selection → hash of both
    2. Execution of the suites against the chosen backend
    3. Aggregation into a per-category structured report (no aggregate score)

Steps 4–6 (signed credential, publication) arrive in Phases 4 and 5.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opaas.adapters import get_backend
from opaas.hashing import allocate_run_id, config_hash, weights_hash
from opaas.report import (
    ConfigurationInfo,
    ModelInfo,
    RunReport,
    describe_environment,
    describe_hardware,
    utcnow_iso,
)
from opaas.suites import coverage, get_suite

#: Where runs are stored when `--output` is not given.
DEFAULT_RUNS_DIR = Path("runs")

#: A local run measures your own deployment; a cloud baseline measures the same
#: model under someone else's optimal conditions. It tells you how much quality
#: you lose by deploying locally, and it is **never** mixed into block A.
REFERENCE_TYPES = ("on-prem", "cloud-baseline")

#: Blocks that make no sense against a shared cloud endpoint.
_BLOCKED_FOR_BASELINE = {
    "performance": (
        "The throughput, latency and power draw of a shared cloud endpoint describe "
        "no deployment at all: they depend on other people's load and on hardware you "
        "do not control. Mixing them with your server's would be comparing different things."
    ),
    "compliance": (
        "Block E asks about the deployment — logging, air-gapped, traceability — and "
        "someone else's cloud endpoint is not your deployment."
    ),
}


@dataclass(slots=True)
class RunRequest:
    model: str
    backend: str
    suites: list[str]
    backend_config: dict[str, Any] = field(default_factory=dict)
    suite_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Report file. If None: `runs/<run-id>/report.json`.
    output: Path | None = None
    run_id: str | None = None
    model_source: str | None = None
    quantization: str | None = None
    weights_path: Path | None = None
    #: 'on-prem' or 'cloud-baseline'. See REFERENCE_TYPES.
    reference_type: str = "on-prem"

    def resolve_output(self, run_id: str) -> Path:
        return self.output or DEFAULT_RUNS_DIR / run_id / "report.json"

    def validate(self) -> None:
        if self.reference_type not in REFERENCE_TYPES:
            raise ValueError(
                f"Unknown referenceType: {self.reference_type!r}. "
                f"Valid ones: {', '.join(REFERENCE_TYPES)}"
            )
        if self.reference_type != "cloud-baseline":
            return
        blocked = [suite for suite in self.suites if suite in _BLOCKED_FOR_BASELINE]
        if blocked:
            raise ValueError(
                f"A cloud baseline cannot include {', '.join(blocked)}. "
                + " ".join(_BLOCKED_FOR_BASELINE[suite] for suite in blocked)
            )


@dataclass(slots=True)
class RunOutcome:
    report: RunReport
    report_path: Path


def format_elapsed(seconds: float) -> str:
    """`mm:ss`, and `h:mm:ss` once the run goes past the hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@dataclass(slots=True)
class RunEvent:
    """What the run is doing, how far along it is and for how long.

    It is passed to `on_event` instead of a bare string so a caller can draw a
    bar with it; printing it directly still gives a usable line, which is what
    a log or a redirected run needs.
    """

    message: str
    #: 'info' | 'suite-start' | 'progress' | 'suite-end'. Only 'progress' is
    #: emitted many times per second: a consumer may throttle those.
    kind: str = "info"
    #: Percentage of the whole run, suites weighed equally.
    percent: float = 0.0
    elapsed_s: float = 0.0
    suite: str | None = None
    #: Position of the suite in the run, 1-based. 0 outside the suites.
    index: int = 0
    total: int = 0

    def __str__(self) -> str:
        return f"[{self.percent:5.1f}% · {format_elapsed(self.elapsed_s)}] {self.message}"


#: Keys never written to the report: secrets or non-serializable objects.
_SECRET_KEYS = frozenset({"api_key"})
_NON_SERIALIZABLE_KEYS = frozenset({"transport"})


def _publishable_config(config: dict[str, Any]) -> dict[str, Any]:
    """The report is a public document: no secrets, no runtime objects."""
    clean: dict[str, Any] = {}
    for key, value in config.items():
        if key in _NON_SERIALIZABLE_KEYS:
            continue
        clean[key] = "***" if key in _SECRET_KEYS and value else value
    return clean


def execute(
    request: RunRequest, *, on_event: Callable[[RunEvent], None] | None = None
) -> RunOutcome:
    """Runs the whole thing and leaves the report on disk.

    `on_event` receives a `RunEvent` with the progress and the elapsed time of
    the run; it is a side channel, and nothing in the measurement depends on it.
    """
    notify = on_event or (lambda _event: None)
    request.validate()
    started_clock = time.monotonic()
    total_suites = len(request.suites)
    #: Suites already finished. The one in flight adds its own fraction.
    finished_suites = 0

    def emit(
        message: str,
        kind: str = "info",
        *,
        suite: str | None = None,
        index: int = 0,
        fraction: float = 0.0,
    ) -> None:
        done = (finished_suites + fraction) / total_suites if total_suites else 0.0
        notify(
            RunEvent(
                message=message,
                kind=kind,
                percent=100.0 * min(max(done, 0.0), 1.0),
                elapsed_s=time.monotonic() - started_clock,
                suite=suite,
                index=index,
                total=total_suites,
            )
        )

    scan_dir = request.output.parent if request.output else DEFAULT_RUNS_DIR
    run_id = request.run_id or allocate_run_id(scan_dir)
    output_path = request.resolve_output(run_id)
    started_at = utcnow_iso()

    environment = describe_environment()
    effective_config = {
        **_publishable_config(request.backend_config),
        "model": request.model,
        "backend": request.backend,
        "suites": sorted(request.suites),
        "suiteOptions": request.suite_options,
    }

    emit(f"Run {run_id}: loading model {request.model} on backend {request.backend}")
    backend = get_backend(request.backend)
    backend.load(request.model, request.backend_config)

    try:
        backend_metadata = backend.get_backend_metadata()
        report = RunReport(
            run_id=run_id,
            started_at=started_at,
            model=ModelInfo(
                name=request.model,
                source=request.model_source,
                quantization=request.quantization or backend_metadata.get("quantization"),
                weights_hash=weights_hash(request.weights_path),
            ),
            configuration=ConfigurationInfo(
                backend=backend_metadata.get("backend")
                or backend_metadata.get("engine", "unknown"),
                hardware=describe_hardware(environment),
                os=str(environment.get("os", "unknown")),
                config_hash=config_hash(effective_config),
                backend_metadata=backend_metadata,
                effective_config=effective_config,
            ),
            environment=environment,
            reference_type=request.reference_type,
        )

        for position, suite_name in enumerate(request.suites, start=1):
            emit(
                f"Suite '{suite_name}' ({position}/{total_suites})…",
                "suite-start",
                suite=suite_name,
                index=position,
            )
            suite = get_suite(suite_name, request.suite_options.get(suite_name, {}))
            # What the block reports about itself becomes progress for the whole
            # run: each block is a slice, and inside it the block says where it is.
            suite.set_progress_callback(
                lambda fraction, detail, name=suite_name, at=position: emit(
                    f"[{at}/{total_suites}] {name}" + (f" · {detail}" if detail else ""),
                    "progress",
                    suite=name,
                    index=at,
                    fraction=fraction,
                )
            )
            result = suite.run(backend)
            finished_suites = position
            report.suites.append(result)
            if result.status == "failed":
                emit(
                    f"Suite '{suite_name}' failed: {result.error}",
                    "suite-end",
                    suite=suite_name,
                    index=position,
                )
            else:
                emit(
                    f"Suite '{suite_name}' completed in {result.duration_s}s",
                    "suite-end",
                    suite=suite_name,
                    index=position,
                )
    finally:
        backend.close()

    report.finished_at = utcnow_iso()
    # Each block's limitations bubble up to the report so they are visible without
    # having to dig into the specific suite.
    report.limitations = [
        f"[{result.suite}] {limitation}"
        for result in report.suites
        for limitation in result.limitations
    ]
    completed = [r.suite for r in report.suites if r.status == "completed"]
    report.coverage = coverage(completed)
    if request.reference_type == "cloud-baseline":
        report.limitations.insert(
            0,
            "CLOUD REFERENCE, not a deployment: it measures the model's quality under "
            "someone else's optimal conditions. It tells you how much quality your local "
            "deployment loses, not how to certify it. It is not comparable with block A metrics.",
        )
    if any(result.status == "failed" for result in report.suites):
        report.status = "partial"
    elif not report.coverage["complete"]:
        # What was asked for was measured, but it does not cover the five design blocks.
        report.status = "evaluated-partial"

    report_path = report.write(output_path)
    emit(f"Report written to {report_path}")
    return RunOutcome(report=report, report_path=report_path)
