"""Memory of the system and of the process serving the model.

For GPU, `nvidia-smi` is enough and is present on any host with drivers. For
RAM there is no universal equivalent, and hand-rolling per-OS memory
introspection is more risk than dependency: `psutil` is used.

Two different measurements, and they should not be confused:

- **System RAM**: includes everything running on the machine. It is the honest
  figure for "does this deployment fit on this server?".
- **Server process RSS**: only the inference engine and its workers. More
  precise, but it requires being able to resolve which process listens on the
  port, which is not always possible without privileges.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

_MB = 1024 * 1024


@dataclass(slots=True)
class RamSnapshot:
    """Instantaneous memory reading."""

    available: bool = False
    total_mb: float | None = None
    used_mb: float | None = None
    process_rss_mb: float | None = None
    process_name: str | None = None
    note: str | None = None


def query_ram(pid: int | None = None) -> RamSnapshot:
    """System memory and, if the PID is known, the server process's."""
    try:
        memory = psutil.virtual_memory()
    except Exception as exc:  # noqa: BLE001 - psutil raises different things per OS
        return RamSnapshot(note=f"Could not read system memory: {exc}")

    snapshot = RamSnapshot(
        available=True,
        total_mb=memory.total / _MB,
        used_mb=(memory.total - memory.available) / _MB,
    )
    if pid is not None:
        rss, name, note = _process_rss_mb(pid)
        snapshot.process_rss_mb = rss
        snapshot.process_name = name
        snapshot.note = note
    return snapshot


def _process_rss_mb(pid: int) -> tuple[float | None, str | None, str | None]:
    """RSS of the process and its children: vLLM spreads the work across workers."""
    try:
        process = psutil.Process(pid)
        name = process.name()
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total / _MB, name, None
    except psutil.NoSuchProcess:
        return None, None, f"Process {pid} no longer exists"
    except psutil.AccessDenied:
        return None, None, f"No permission to inspect process {pid}"
    except psutil.Error as exc:
        return None, None, f"Could not measure process {pid}: {exc}"


def resolve_listening_pid(port: int) -> tuple[int | None, str | None]:
    """Which local process listens on a port. Returns (pid, reason for failure).

    Best effort: on several systems, enumerating other processes' connections
    requires privileges. If it cannot be done, we fall back to system RAM and
    document why the per-process figure is missing.
    """
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return None, "No permission to enumerate connections: the server process is not identified"
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not enumerate connections: {exc}"

    for connection in connections:
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and connection.laddr.port == port
            and connection.pid
        ):
            return connection.pid, None
    return None, f"No local process listens on port {port}: remote server?"
