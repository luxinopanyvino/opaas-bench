"""Resource sampling (GPU + RAM) during a phase of the evaluation.

A single thread samples both: two independent samplers would give different
time windows and the VRAM peak could not be cross-referenced with the RAM one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from opaas_bench.telemetry.gpu import query_gpus
from opaas_bench.telemetry.system import query_ram


@dataclass
class ResourceStats:
    """Aggregates over a sampling window."""

    samples: int = 0
    duration_s: float = 0.0
    vram_peak_mb: float | None = None
    vram_mean_mb: float | None = None
    vram_total_mb: float | None = None
    power_peak_w: float | None = None
    power_mean_w: float | None = None
    gpu_util_mean_pct: float | None = None
    ram_peak_mb: float | None = None
    ram_mean_mb: float | None = None
    ram_total_mb: float | None = None
    process_rss_peak_mb: float | None = None
    process_rss_mean_mb: float | None = None
    process_name: str | None = None
    gpu_note: str | None = None
    ram_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "durationS": round(self.duration_s, 3),
            "vramPeakMb": _round(self.vram_peak_mb),
            "vramMeanMb": _round(self.vram_mean_mb),
            "vramTotalMb": _round(self.vram_total_mb),
            "powerPeakW": _round(self.power_peak_w),
            "powerMeanW": _round(self.power_mean_w),
            "gpuUtilMeanPct": _round(self.gpu_util_mean_pct),
            "ramPeakMb": _round(self.ram_peak_mb),
            "ramMeanMb": _round(self.ram_mean_mb),
            "ramTotalMb": _round(self.ram_total_mb),
            "processRssPeakMb": _round(self.process_rss_peak_mb),
            "processRssMeanMb": _round(self.process_rss_mean_mb),
            "processName": self.process_name,
            "gpuNote": self.gpu_note,
            "ramNote": self.ram_note,
        }


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _peak(values: list[float]) -> float | None:
    return max(values) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class ResourceSampler:
    """Samples GPU and RAM on a separate thread while a workload runs.

    Usage::

        with ResourceSampler(interval_s=0.25, pid=server_pid) as sampler:
            ...  # workload
        stats = sampler.stats()
    """

    def __init__(self, interval_s: float = 0.5, pid: int | None = None) -> None:
        self.interval_s = interval_s
        self.pid = pid
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._vram: list[float] = []
        self._power: list[float] = []
        self._util: list[float] = []
        self._ram: list[float] = []
        self._rss: list[float] = []
        self._vram_total: float | None = None
        self._ram_total: float | None = None
        self._process_name: str | None = None
        self._gpu_note: str | None = None
        self._ram_note: str | None = None
        self._started_at: float = 0.0
        self._duration_s: float = 0.0

    def start(self) -> ResourceSampler:
        if self._thread is not None:
            return self
        self._started_at = time.monotonic()
        self._sample_once()  # one immediate reading: short windows do not stay at zero
        self._thread = threading.Thread(target=self._run, name="opaas-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> ResourceStats:
        self._duration_s = time.monotonic() - self._started_at
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=self.interval_s * 4)
            self._thread = None
        return self.stats()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._sample_once()

    def _sample_once(self) -> None:
        gpus = query_gpus()
        ram = query_ram(self.pid)
        with self._lock:
            if gpus.available:
                if (vram := gpus.vram_used_mb) is not None:
                    self._vram.append(vram)
                if (power := gpus.power_w) is not None:
                    self._power.append(power)
                if (util := gpus.gpu_util_pct) is not None:
                    self._util.append(util)
                if self._vram_total is None:
                    self._vram_total = gpus.vram_total_mb
            else:
                self._gpu_note = gpus.note

            if ram.available:
                if ram.used_mb is not None:
                    self._ram.append(ram.used_mb)
                if ram.process_rss_mb is not None:
                    self._rss.append(ram.process_rss_mb)
                    self._process_name = ram.process_name
                if self._ram_total is None:
                    self._ram_total = ram.total_mb
            if ram.note:
                self._ram_note = ram.note

    def stats(self) -> ResourceStats:
        with self._lock:
            vram, power, util = list(self._vram), list(self._power), list(self._util)
            ram, rss = list(self._ram), list(self._rss)
            stats = ResourceStats(
                samples=max(len(vram), len(ram)),
                duration_s=self._duration_s or (time.monotonic() - self._started_at),
                vram_peak_mb=_peak(vram),
                vram_mean_mb=_mean(vram),
                vram_total_mb=self._vram_total,
                power_peak_w=_peak(power),
                power_mean_w=_mean(power),
                gpu_util_mean_pct=_mean(util),
                ram_peak_mb=_peak(ram),
                ram_mean_mb=_mean(ram),
                ram_total_mb=self._ram_total,
                process_rss_peak_mb=_peak(rss),
                process_rss_mean_mb=_mean(rss),
                process_name=self._process_name,
                gpu_note=self._gpu_note,
                ram_note=self._ram_note,
            )
        return stats

    def __enter__(self) -> ResourceSampler:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
