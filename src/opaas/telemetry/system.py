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

import os
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


def files_held_by(pid: int) -> tuple[set[str] | None, str | None]:
    """Files a process and its children have mapped or open. Returns (paths, reason).

    The children matter: Ollama serves from a runner process spawned by
    `ollama serve`, and vLLM runs its engine core in a separate process, so the
    process listening on the port is rarely the one holding the weights.

    Paths are normalised (`realpath`, `normcase`) so that the same file reached
    through a symlink, or spelled with different case on Windows, compares
    equal. Best effort, like the rest of this module: without permission, or on
    a platform without `memory_maps` (macOS), it returns None and says why.
    """
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return None, f"The server process {pid} no longer exists"
    except psutil.AccessDenied:
        return None, f"No permission to inspect the server process {pid}"

    held: set[str] = set()
    denied = 0
    for process in processes:
        try:
            if hasattr(process, "memory_maps"):
                held.update(m.path for m in process.memory_maps(grouped=True) if m.path)
            held.update(f.path for f in process.open_files())
        except (psutil.AccessDenied, psutil.ZombieProcess):
            denied += 1
        except psutil.NoSuchProcess:
            continue
    if denied == len(processes):
        return None, f"No permission to inspect the files of server process {pid}"
    return {normalise_path(path) for path in held}, None


def normalise_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))
