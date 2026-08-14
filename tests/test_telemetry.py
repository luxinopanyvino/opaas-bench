import os
import socket
import threading

import psutil
import pytest

from opaas_bench.telemetry import ResourceSampler, query_ram, resolve_listening_pid
from opaas_bench.telemetry.system import RamSnapshot


def test_system_ram_is_reported():
    snapshot = query_ram()
    assert snapshot.available
    assert snapshot.total_mb > 0
    assert 0 < snapshot.used_mb <= snapshot.total_mb


def test_process_rss_is_reported_for_our_own_process():
    snapshot = query_ram(os.getpid())
    assert snapshot.process_rss_mb > 0
    assert snapshot.process_name
    assert snapshot.note is None


def test_dead_process_is_reported_not_crashed():
    """A server that dies mid-run must not bring the measurement down."""
    dead_pid = max(p.pid for p in psutil.process_iter()) + 100000
    snapshot = query_ram(dead_pid)
    assert snapshot.available  # system RAM is still there
    assert snapshot.process_rss_mb is None
    assert "no longer exists" in snapshot.note


def test_resolve_listening_pid_finds_a_local_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        pid, note = resolve_listening_pid(port)
        if pid is None:
            # En algunos sistemas enumerar conexiones exige privilegios.
            assert note
        else:
            assert pid == os.getpid()
    finally:
        server.close()


def test_resolve_listening_pid_on_a_free_port_explains_itself():
    pid, note = resolve_listening_pid(1)
    assert pid is None
    assert note


def test_sampler_collects_at_least_one_sample_in_a_short_window():
    """Short windows: without the immediate reading at startup they would stay at zero."""
    with ResourceSampler(interval_s=5.0) as sampler:
        pass
    stats = sampler.stats()
    assert stats.samples >= 1
    assert stats.ram_peak_mb is not None


def test_sampler_tracks_peak_and_mean():
    sampler = ResourceSampler(interval_s=0.02).start()
    threading.Event().wait(0.15)
    stats = sampler.stop()
    assert stats.samples >= 2
    assert stats.ram_peak_mb >= stats.ram_mean_mb
    assert stats.duration_s > 0


def test_sampler_serializes_both_gpu_and_ram_keys():
    with ResourceSampler(interval_s=5.0) as sampler:
        pass
    payload = sampler.stats().as_dict()
    for key in ("vramPeakMb", "ramPeakMb", "processRssPeakMb", "powerMeanW", "ramTotalMb"):
        assert key in payload


def test_sampler_survives_a_process_that_disappears():
    dead_pid = max(p.pid for p in psutil.process_iter()) + 100000
    with ResourceSampler(interval_s=5.0, pid=dead_pid) as sampler:
        pass
    stats = sampler.stats()
    assert stats.process_rss_peak_mb is None
    assert stats.ram_note


@pytest.mark.parametrize("snapshot", [RamSnapshot(), RamSnapshot(available=True, total_mb=1.0)])
def test_ram_snapshot_defaults_are_safe(snapshot):
    assert snapshot.process_rss_mb is None
