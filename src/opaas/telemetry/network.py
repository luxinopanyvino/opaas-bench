"""Detection of outbound traffic from the process serving the model.

Block E asks whether the deployment can operate air-gapped. The strong answer
comes from an isolated network namespace or a packet capture; here we do what
can be done without privileges or root: sample the server process's connections
during inference and classify the destinations.

**Accepted and declared limitation**: this is sampling, not capture. A very
short connection between two samples can go unnoticed, and UDP traffic (DNS)
does not show up reliably. It is good for spotting obvious telemetry or
downloads, not for certifying isolation. That last one requires isolating the
network and checking that the engine still works.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass, field

import psutil

LOOPBACK = "loopback"
PRIVATE = "private"
EXTERNAL = "external"
UNKNOWN = "unknown"


def classify(address: str) -> str:
    """Classifies a remote destination by its network scope."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return UNKNOWN
    if parsed.is_loopback:
        return LOOPBACK
    if parsed.is_private or parsed.is_link_local:
        return PRIVATE
    return EXTERNAL


@dataclass(slots=True)
class Endpoint:
    address: str
    port: int
    scope: str

    def as_dict(self) -> dict[str, object]:
        return {"address": self.address, "port": self.port, "scope": self.scope}


@dataclass
class EgressReport:
    available: bool = False
    endpoints: list[Endpoint] = field(default_factory=list)
    samples: int = 0
    duration_s: float = 0.0
    note: str | None = None

    @property
    def external(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.scope == EXTERNAL]

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "samples": self.samples,
            "durationS": round(self.duration_s, 3),
            "externalConnections": [e.as_dict() for e in self.external],
            "allRemoteEndpoints": [e.as_dict() for e in self.endpoints],
            "method": "sampling of the process's connections (not packet capture)",
            "note": self.note,
        }


def sample_endpoints(pid: int) -> tuple[set[tuple[str, int]], str | None]:
    """Remote destinations the process has a connection to right now."""
    try:
        process = psutil.Process(pid)
        connections = process.net_connections(kind="inet")
        for child in process.children(recursive=True):
            try:
                connections += child.net_connections(kind="inet")
            except psutil.Error:
                continue
    except psutil.NoSuchProcess:
        return set(), f"Process {pid} no longer exists"
    except psutil.AccessDenied:
        return set(), f"No permission to inspect the connections of process {pid}"
    except psutil.Error as exc:
        return set(), f"Could not read the connections: {exc}"

    return {
        (connection.raddr.ip, connection.raddr.port)
        for connection in connections
        if connection.raddr
    }, None


class EgressWatcher:
    """Watches the server process's outbound connections during a workload."""

    def __init__(self, pid: int | None, interval_s: float = 0.25) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen: set[tuple[str, int]] = set()
        self._samples = 0
        self._note: str | None = None
        self._started_at = 0.0
        self._duration_s = 0.0

    def start(self) -> EgressWatcher:
        self._started_at = time.monotonic()
        if self.pid is None:
            self._note = (
                "The server process was not identified: nothing can be claimed about its "
                "outbound traffic. With a remote endpoint, this check does not apply."
            )
            return self
        self._sample_once()
        self._thread = threading.Thread(target=self._run, name="opaas-egress", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> EgressReport:
        self._duration_s = time.monotonic() - self._started_at
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=self.interval_s * 4)
            self._thread = None
        return self.report()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._sample_once()

    def _sample_once(self) -> None:
        endpoints, note = sample_endpoints(self.pid)  # type: ignore[arg-type]
        with self._lock:
            self._samples += 1
            self._seen |= endpoints
            if note:
                self._note = note

    def report(self) -> EgressReport:
        with self._lock:
            endpoints = [
                Endpoint(address=address, port=port, scope=classify(address))
                for address, port in sorted(self._seen)
            ]
            return EgressReport(
                available=self.pid is not None and self._note is None,
                endpoints=endpoints,
                samples=self._samples,
                duration_s=self._duration_s or (time.monotonic() - self._started_at),
                note=self._note,
            )

    def __enter__(self) -> EgressWatcher:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
