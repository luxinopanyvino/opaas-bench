from opaas_bench.suites.performance import PerformanceSuite, _build_prompt

from .conftest import FakeBackend, NoConcurrencyBackend

OPTIONS = {
    "concurrency": [1, 4],
    "prompt_words": [32],
    "max_tokens": 16,
    "requests_per_setting": 4,
    "warmup_requests": 1,
    "sample_interval_s": 5.0,
    "idle_seconds": 0.0,
}


def test_build_prompt_length():
    assert len(_build_prompt(50).split()) == 50


def test_sweep_covers_every_combination(fake_backend):
    result = PerformanceSuite(OPTIONS).run(fake_backend)
    assert result.status == "completed", result.error
    scenarios = result.metrics["scenarios"]
    assert {(s["promptWords"], s["concurrency"]) for s in scenarios} == {(32, 1), (32, 4)}


def test_scenario_reports_throughput_and_latency(fake_backend):
    result = PerformanceSuite(OPTIONS).run(fake_backend)
    scenario = result.metrics["scenarios"][0]
    assert scenario["requestsCompleted"] == 4
    assert scenario["requestsFailed"] == 0
    assert scenario["throughput"]["outputTokensPerS"] > 0
    assert scenario["throughput"]["promptTokensPerS"] > 0
    assert scenario["ttftS"]["p50"] is not None
    assert scenario["ttftS"]["count"] == 4
    assert scenario["tokens"]["completion"] == 4 * 16


def test_idle_baseline_is_measured_and_reported(fake_backend):
    """The standard asks for 'peak and idle' memory: idle is a measurement in its own right."""
    result = PerformanceSuite({**OPTIONS, "idle_seconds": 0.05}).run(fake_backend)
    idle = result.metrics["idle"]
    assert idle["samples"] >= 1
    assert idle["ramMeanMb"] is not None
    assert idle["ramTotalMb"] > 0


def test_ram_is_actually_measured_not_just_declared(fake_backend):
    result = PerformanceSuite(OPTIONS).run(fake_backend)
    resources = result.metrics["scenarios"][0]["resources"]
    assert resources["ramPeakMb"] is not None
    assert resources["ramTotalMb"] >= resources["ramPeakMb"]


def test_over_idle_deltas_isolate_the_cost_of_serving(fake_backend):
    result = PerformanceSuite({**OPTIONS, "idle_seconds": 0.05}).run(fake_backend)
    over_idle = result.metrics["scenarios"][0]["overIdle"]
    # The delta exists whenever there is an idle and a peak; its sign depends on the host.
    assert "vramMb" in over_idle and "ramMb" in over_idle
    assert over_idle["ramMb"] is not None


def test_energy_unavailable_without_gpu_telemetry(fake_backend):
    result = PerformanceSuite(OPTIONS).run(fake_backend)
    energy = result.metrics["scenarios"][0]["energy"]
    # There is no nvidia-smi in CI: it must say so, not invent a zero.
    if not energy["available"]:
        assert energy["note"]
        assert any("energy cost" in limitation for limitation in result.limitations)


def test_queueing_is_reported_as_a_finding_not_just_ratios():
    """An engine that serializes: flat throughput and growing TTFT."""
    from opaas_bench.suites.performance import _observations

    degradation = [
        {"promptWords": 128, "concurrency": 1, "throughputSpeedupVsC1": 1.0,
         "ttftP50SlowdownVsC1": 1.0},
        {"promptWords": 128, "concurrency": 16, "throughputSpeedupVsC1": 1.03,
         "ttftP50SlowdownVsC1": 128.2},
    ]
    observations = _observations([], degradation)
    assert len(observations) == 1
    assert observations[0]["kind"] == "no-concurrency-scaling"
    assert observations[0]["concurrency"] == 16
    assert "queueing" in observations[0]["detail"]


def test_real_scaling_produces_no_queueing_finding():
    from opaas_bench.suites.performance import _observations

    degradation = [
        {"promptWords": 128, "concurrency": 16, "throughputSpeedupVsC1": 7.4,
         "ttftP50SlowdownVsC1": 2.9},
    ]
    assert _observations([], degradation) == []


def test_vram_pressure_is_reported():
    from opaas_bench.suites.performance import _observations

    scenarios = [
        {
            "promptWords": 1024,
            "concurrency": 4,
            "requestsFailed": 0,
            "requestsRequested": 8,
            "resources": {"vramPeakMb": 15500.0, "vramTotalMb": 16311.0},
        }
    ]
    observations = _observations(scenarios, [])
    assert observations[0]["kind"] == "vram-at-limit"
    assert "95%" in observations[0]["detail"]


def test_failed_requests_surface_as_a_finding():
    from opaas_bench.suites.performance import _observations

    scenarios = [
        {
            "promptWords": 128,
            "concurrency": 16,
            "requestsFailed": 3,
            "requestsRequested": 16,
            "resources": {},
        }
    ]
    assert _observations(scenarios, [])[0]["kind"] == "failed-requests"


def test_observations_are_deduplicated_across_prompt_lengths():
    from opaas_bench.suites.performance import _observations

    degradation = [
        {"promptWords": words, "concurrency": 16, "throughputSpeedupVsC1": 1.0,
         "ttftP50SlowdownVsC1": 100.0}
        for words in (128, 1024)
    ]
    assert len(_observations([], degradation)) == 1


def test_degradation_is_relative_to_concurrency_one(fake_backend):
    result = PerformanceSuite(OPTIONS).run(fake_backend)
    degradation = result.metrics["concurrencyDegradation"]
    baseline = next(d for d in degradation if d["concurrency"] == 1)
    assert baseline["throughputSpeedupVsC1"] == 1.0
    assert len(degradation) == 2


def test_inprocess_backend_clamps_concurrency():
    backend = NoConcurrencyBackend()
    backend.load("fake/model-1b", {})
    result = PerformanceSuite(OPTIONS).run(backend)
    assert {s["concurrency"] for s in result.metrics["scenarios"]} == {1}
    assert any("in-process" in limitation for limitation in result.limitations)


def test_request_failures_are_recorded_not_swallowed():
    backend = FakeBackend(fail_after=2)
    backend.load("fake/model-1b", {})
    result = PerformanceSuite({**OPTIONS, "concurrency": [1], "warmup_requests": 0}).run(backend)
    scenario = result.metrics["scenarios"][0]
    assert scenario["requestsFailed"] == 2
    assert scenario["requestsCompleted"] == 2
    assert "simulated backend down" in scenario["errorSamples"][0]


def test_suite_failure_does_not_raise(monkeypatch, fake_backend):
    def _boom(backend):
        raise ValueError("boom")

    suite = PerformanceSuite(OPTIONS)
    monkeypatch.setattr(suite, "evaluate", _boom)
    result = suite.run(fake_backend)
    assert result.status == "failed"
    assert "ValueError: boom" in result.error
