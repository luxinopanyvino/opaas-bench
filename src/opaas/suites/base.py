"""Common contract for the evaluation suites (blocks A–E)."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

from opaas.adapters.base import InferenceBackend
from opaas.report import SuiteResult, utcnow_iso

#: Called with (fraction of the block already done in [0, 1], what it is doing now).
ProgressCallback = Callable[[float, str], None]


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
        self._progress: ProgressCallback | None = None
        self._steps_total = 0.0
        self._steps_done = 0.0
        self._steps_detail = ""
        #: Blocks B and D evaluate items from several threads: the counter is shared.
        self._steps_lock = threading.Lock()

    @abstractmethod
    def evaluate(self, backend: InferenceBackend) -> tuple[dict[str, Any], list[str]]:
        """Runs the block. Returns (metrics, known limitations)."""

    # ------------------------------------------------------------------- progress

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """The runner injects here how to report progress.

        Without a callback the suite runs exactly the same: reporting is an
        optional side channel, never a dependency of the measurement.
        """
        self._progress = callback

    def plan_steps(self, total: float, detail: str = "") -> None:
        """Declares how many units of work the block has, so they read as a %.

        An estimate is enough — what is being reported is 'how far along this
        is', not a promise. `step` never reports beyond 100%.
        """
        with self._steps_lock:
            self._steps_total = max(float(total), 1.0)
            self._steps_done = 0.0
            self._steps_detail = ""
        self._emit(detail)

    def step(self, amount: float = 1.0, detail: str = "") -> None:
        """Advances the counter.

        `amount=0` only refreshes what is being shown, to announce a long step
        before starting it. Without `detail` the last one stands: closing a step
        must not blank out what the run is doing.
        """
        if amount:
            with self._steps_lock:
                self._steps_done += amount
        self._emit(detail)

    def _emit(self, detail: str) -> None:
        if self._progress is None:
            return
        with self._steps_lock:
            if detail:
                self._steps_detail = detail
            fraction = (
                min(self._steps_done / self._steps_total, 1.0) if self._steps_total else 0.0
            )
            current = self._steps_detail
        self._progress(fraction, current)

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
