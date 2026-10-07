"""GPU telemetry via ``nvidia-smi``.

Deliberately dependency-free (no pynvml): ``nvidia-smi`` is present on any host
with NVIDIA drivers and behaves the same on Linux and Windows. If it is not
available, everything returns *not available* with an explicit reason — never
zero.

Continuous sampling lives in `telemetry.sampler`, which combines this with RAM.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

_QUERY_FIELDS = (
    "index",
    "name",
    "memory.used",
    "memory.total",
    "utilization.gpu",
    "power.draw",
    "driver_version",
)

_NOT_AVAILABLE = "nvidia-smi not found: GPU telemetry unavailable on this host"


@dataclass(slots=True)
class GpuSnapshot:
    """Instantaneous reading of the set of visible GPUs."""

    gpus: list[dict[str, float | str | None]] = field(default_factory=list)
    note: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.gpus)

    @property
    def vram_used_mb(self) -> float | None:
        return _sum_field(self.gpus, "memory_used_mb")

    @property
    def vram_total_mb(self) -> float | None:
        return _sum_field(self.gpus, "memory_total_mb")

    @property
    def power_w(self) -> float | None:
        return _sum_field(self.gpus, "power_w")

    @property
    def gpu_util_pct(self) -> float | None:
        values = [g.get("utilization_pct") for g in self.gpus]
        numeric = [v for v in values if isinstance(v, (int, float))]
        if not numeric:
            return None
        return sum(numeric) / len(numeric)


def _sum_field(gpus: list[dict[str, float | str | None]], key: str) -> float | None:
    numeric = [g.get(key) for g in gpus]
    values = [v for v in numeric if isinstance(v, (int, float))]
    return sum(values) if values else None


def _to_float(raw: str) -> float | None:
    raw = raw.strip()
    # nvidia-smi returns "[N/A]" or "[Not Supported]" on GPUs with no power sensor.
    if not raw or raw.startswith("["):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def query_gpus(timeout_s: float = 5.0) -> GpuSnapshot:
    """Reads the current GPU state. Never raises: it reports the reason for failure."""
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return GpuSnapshot(note=_NOT_AVAILABLE)

    cmd = [binary, f"--query-gpu={','.join(_QUERY_FIELDS)}", "--format=csv,noheader,nounits"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GpuSnapshot(note=f"nvidia-smi failed: {exc}")

    if proc.returncode != 0:
        return GpuSnapshot(
            note=f"nvidia-smi exited with code {proc.returncode}: {proc.stderr.strip()}"
        )

    gpus: list[dict[str, float | str | None]] = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_QUERY_FIELDS):
            continue
        gpus.append(
            {
                "index": parts[0],
                "name": parts[1],
                "memory_used_mb": _to_float(parts[2]),
                "memory_total_mb": _to_float(parts[3]),
                "utilization_pct": _to_float(parts[4]),
                "power_w": _to_float(parts[5]),
                "driver_version": parts[6] or None,
            }
        )

    if not gpus:
        return GpuSnapshot(note="nvidia-smi reported no GPUs")
    return GpuSnapshot(gpus=gpus)
