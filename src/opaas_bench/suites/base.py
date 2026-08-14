"""Common contract for the evaluation suites (blocks A–E)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from opaas_bench.adapters.base import InferenceBackend
from opaas_bench.report import SuiteResult, utcnow_iso


class Suite(ABC):
    """An evaluation block runnable against any `InferenceBackend`."""

    #: Name used in `--suite`.
    name: ClassVar[str] = "abstract"
    #: Block from the design document: A Performance, B Capability, C Consistency,
    #: D Behavior and safety, E Compliance readiness.
    block: ClassVar[str] = ""
    description: ClassVar[str] = ""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}

    @abstractmethod
    def evaluate(self, backend: InferenceBackend) -> tuple[dict[str, Any], list[str]]:
        """Runs the block. Returns (metrics, known limitations)."""

    def run(self, backend: InferenceBackend) -> SuiteResult:
        """Wraps `evaluate` with timing and error capture.

        A suite failure does not abort the run: it is recorded with
        `status: failed` so the report reflects what actually happened.
        """
        started_at = utcnow_iso()
        started = time.perf_counter()
        try:
            metrics, limitations = self.evaluate(backend)
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            metrics, limitations = {}, []
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        return SuiteResult(
            suite=self.name,
            block=self.block,
            status=status,
            started_at=started_at,
            finished_at=utcnow_iso(),
            duration_s=round(time.perf_counter() - started, 3),
            metrics=metrics,
            limitations=limitations,
            error=error,
        )
