"""Block A — Performance.

Measures throughput (prompt and generation separately), TTFT p50/p95/p99,
memory, degradation under concurrency and estimated energy cost per million
tokens.

Every figure comes from a `prompt length × concurrency` sweep; the
concurrency-1 case is the reference used to compute degradation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, ClassVar

from opaas_bench.adapters.base import GenerationResult, InferenceBackend
from opaas_bench.stats import mean, summarize
from opaas_bench.suites.base import Suite
from opaas_bench.telemetry.sampler import ResourceSampler

#: Neutral, repeatable text for building prompts of controlled length. Meaningful
#: content is not the point: only speed is measured here, not quality.
_FILLER = (
    "The evaluation system records every measurement together with the exact "
    "configuration of the deployment so the result is reproducible by a third party. "
)

_SECONDS_PER_HOUR = 3600.0


@dataclass(slots=True)
class PerformanceOptions:
    concurrency: list[int] = field(default_factory=lambda: [1, 4, 16])
    prompt_words: list[int] = field(default_factory=lambda: [128, 1024])
    max_tokens: int = 256
    requests_per_setting: int | None = None
    warmup_requests: int = 2
    temperature: float = 0.0
    seed: int | None = 1234
    sample_interval_s: float = 0.5
    #: Idle window, measured after warmup: model resident and not generating.
    #: It is the reference the memory peak is read against.
    idle_seconds: float = 3.0

    @classmethod
    def from_dict(cls, options: dict[str, Any]) -> PerformanceOptions:
        known = {f: options[f] for f in cls.__slots__ if f in options}
        return cls(**known)

    def requests_for(self, concurrency: int) -> int:
        if self.requests_per_setting is not None:
            return max(concurrency, self.requests_per_setting)
        # Enough requests for the concurrency level to hold throughout the
        # measurement window, not just in an instantaneous spike.
        return max(8, concurrency * 3)

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__slots__}


@dataclass(slots=True)
class _Attempt:
    result: GenerationResult | None
    error: str | None
    wall_s: float


class PerformanceSuite(Suite):
    name: ClassVar[str] = "performance"
    block: ClassVar[str] = "A"
    description: ClassVar[str] = "Throughput, latency, memory, concurrency and energy cost"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.opts = PerformanceOptions.from_dict(self.options)

    # -------------------------------------------------------------------- public

    def evaluate(self, backend: InferenceBackend) -> tuple[dict[str, Any], list[str]]:
        limitations: list[str] = []
        levels = sorted(set(self.opts.concurrency))

        if not backend.supports_concurrency and levels != [1]:
            limitations.append(
                f"Backend '{backend.name}' is in-process and exposes no real concurrency: "
                f"the sweep is limited to 1 simultaneous request (levels requested: {levels})."
            )
            levels = [1]
        if not backend.supports_streaming:
            limitations.append(
                f"Backend '{backend.name}' does not support streaming: TTFT is not measurable "
                "and is reported as null."
            )

        lengths = sorted(set(self.opts.prompt_words))
        # One unit per request that is going to be issued, plus warmup and idle:
        # the scenarios with high concurrency issue more, and weighing them the
        # same would make the bar stall on the longest one.
        self.plan_steps(
            max(0, self.opts.warmup_requests)
            + 1
            + len(lengths) * sum(self.opts.requests_for(c) for c in levels),
            "warmup",
        )

        self._warmup(backend)
        # Idle is measured AFTER warmup: engines like Ollama load the model on the
        # first request, so measuring before would give the memory of an empty GPU
        # rather than that of the resident model.
        idle = self._measure_idle(backend)

        scenarios: list[dict[str, Any]] = []
        for words in lengths:
            prompt = _build_prompt(words)
            for concurrency in levels:
                scenarios.append(self._run_scenario(backend, prompt, words, concurrency, idle))

        exact = all(s["tokenCountsExact"] for s in scenarios if s["tokenCountsExact"] is not None)
        if not exact:
            limitations.append(
                "The server did not return `usage` on some request: the token counts for "
                "those scenarios are estimated from characters."
            )
        samples = [s["resources"]["samples"] for s in scenarios]
        if not any(s["resources"].get("vramPeakMb") for s in scenarios):
            note = next(
                (s["resources"].get("gpuNote") for s in scenarios if s["resources"].get("gpuNote")),
                "no GPU telemetry",
            )
            limitations.append(f"VRAM and energy cost not measured: {note}.")
        if not any(samples):
            limitations.append("No resource samples at all within the measurement window.")
        elif min(samples) < 3:
            limitations.append(
                f"Some scenario finished in under {3 * self.opts.sample_interval_s:.1f}s: "
                "the VRAM peak and energy cost rest on very few samples. "
                "Raise --max-tokens or --requests for a representative measurement."
            )

        if idle["samples"] and not idle.get("ramPeakMb"):
            limitations.append(
                f"RAM could not be measured: {idle.get('ramNote') or 'reason unknown'}."
            )
        elif idle.get("processRssPeakMb") is None and idle.get("ramNote"):
            limitations.append(
                f"RAM measured at the system level, not the server process: {idle['ramNote']}. "
                "The figure includes everything running on the host."
            )

        degradation = _degradation(scenarios)
        metrics = {
            "options": self.opts.as_dict(),
            "idle": idle,
            "scenarios": scenarios,
            "concurrencyDegradation": degradation,
            # The raw ratios do not read on their own: this translates them into
            # what matters to whoever is sizing the deployment.
            "observations": _observations(scenarios, degradation),
        }
        return metrics, limitations

    # ------------------------------------------------------------------ internal

    def _warmup(self, backend: InferenceBackend) -> None:
        """Discards the first generations: kernel compilation and cold cache."""
        prompt = _build_prompt(min(self.opts.prompt_words))
        for _ in range(max(0, self.opts.warmup_requests)):
            self._attempt(backend, prompt, max_tokens=16)
            self.step(detail="warmup")

    def _attempt(self, backend: InferenceBackend, prompt: str, max_tokens: int) -> _Attempt:
        started = time.perf_counter()
        try:
            result = backend.generate(
                prompt,
                max_tokens=max_tokens,
                temperature=self.opts.temperature,
                seed=self.opts.seed,
                stream=backend.supports_streaming,
            )
            return _Attempt(result=result, error=None, wall_s=time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - failures are data about the run
            return _Attempt(
                result=None,
                error=f"{type(exc).__name__}: {exc}",
                wall_s=time.perf_counter() - started,
            )

    def _measure_idle(self, backend: InferenceBackend) -> dict[str, Any]:
        """Consumption with the model loaded and not generating."""
        self.step(0, detail=f"idle consumption ({self.opts.idle_seconds:.0f}s)")
        sampler = ResourceSampler(
            interval_s=min(self.opts.sample_interval_s, 0.5), pid=backend.server_pid()
        ).start()
        time.sleep(max(0.0, self.opts.idle_seconds))
        self.step(detail="idle consumption")
        return sampler.stop().as_dict()

    def _run_scenario(
        self,
        backend: InferenceBackend,
        prompt: str,
        words: int,
        concurrency: int,
        idle: dict[str, Any],
    ) -> dict[str, Any]:
        total_requests = self.opts.requests_for(concurrency)
        label = f"prompt {words} w · concurrency {concurrency}"
        self.step(0, detail=f"{label} · 0/{total_requests} requests")
        sampler = ResourceSampler(
            interval_s=self.opts.sample_interval_s, pid=backend.server_pid()
        ).start()

        attempts: list[_Attempt] = []
        wall_start = time.perf_counter()
        if concurrency == 1:
            for _ in range(total_requests):
                attempts.append(self._attempt(backend, prompt, self.opts.max_tokens))
                self.step(detail=f"{label} · {len(attempts)}/{total_requests} requests")
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [
                    pool.submit(self._attempt, backend, prompt, self.opts.max_tokens)
                    for _ in range(total_requests)
                ]
                for future in as_completed(futures):
                    attempts.append(future.result())
                    self.step(detail=f"{label} · {len(attempts)}/{total_requests} requests")
        wall_s = time.perf_counter() - wall_start

        resources = sampler.stop()
        return _aggregate(
            prompt_words=words,
            concurrency=concurrency,
            requested=total_requests,
            attempts=attempts,
            wall_s=wall_s,
            resources=resources.as_dict(),
            power_mean_w=resources.power_mean_w,
            idle=idle,
        )


def _build_prompt(words: int) -> str:
    """Synthetic prompt of ~`words` words. The real token count comes from the server."""
    filler_words = _FILLER.split()
    repetitions = (words // len(filler_words)) + 1
    return " ".join((filler_words * repetitions)[:words])


def _aggregate(
    *,
    prompt_words: int,
    concurrency: int,
    requested: int,
    attempts: list[_Attempt],
    wall_s: float,
    resources: dict[str, Any],
    power_mean_w: float | None,
    idle: dict[str, Any],
) -> dict[str, Any]:
    ok = [a.result for a in attempts if a.result is not None]
    errors = [a.error for a in attempts if a.error is not None]

    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    latencies = [r.total_s for r in ok]
    decode_rates = [r.output_tokens_per_s for r in ok if r.output_tokens_per_s is not None]

    prompt_tokens = sum(r.prompt_tokens for r in ok)
    completion_tokens = sum(r.completion_tokens for r in ok)
    output_tps = completion_tokens / wall_s if wall_s > 0 else None
    prompt_tps = prompt_tokens / wall_s if wall_s > 0 else None

    return {
        "promptWords": prompt_words,
        "concurrency": concurrency,
        "requestsRequested": requested,
        "requestsCompleted": len(ok),
        "requestsFailed": len(errors),
        "errorSamples": errors[:3],
        "wallS": round(wall_s, 4),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "promptPerRequestMean": _round(mean([r.prompt_tokens for r in ok])),
            "completionPerRequestMean": _round(mean([r.completion_tokens for r in ok])),
        },
        "tokenCountsExact": all(r.token_counts_exact for r in ok) if ok else None,
        "throughput": {
            # Aggregate system throughput: what the operator sees.
            "outputTokensPerS": _round(output_tps),
            "promptTokensPerS": _round(prompt_tps),
            "requestsPerS": _round(len(ok) / wall_s if wall_s > 0 else None),
            # Per-request decoding speed: what an end user perceives.
            "perRequestDecodeTokensPerS": summarize(decode_rates, digits=2),
        },
        "ttftS": summarize(ttfts),
        "requestLatencyS": summarize(latencies),
        "resources": resources,
        # What it costs to *serve*, minus what it costs to merely keep the model
        # loaded. This is the figure whoever sizes a server needs.
        "overIdle": {
            "vramMb": _delta(resources.get("vramPeakMb"), idle.get("vramMeanMb")),
            "ramMb": _delta(resources.get("ramPeakMb"), idle.get("ramMeanMb")),
            "processRssMb": _delta(
                resources.get("processRssPeakMb"), idle.get("processRssMeanMb")
            ),
            "powerW": _delta(resources.get("powerMeanW"), idle.get("powerMeanW")),
        },
        "energy": _energy(power_mean_w, output_tps),
    }


def _energy(power_mean_w: float | None, output_tps: float | None) -> dict[str, Any]:
    """Estimated energy cost per million tokens generated.

    It is mean GPU power × the time needed for 1M tokens at the measured
    throughput. It does not include CPU, cooling or PSU: it is a lower bound,
    and it is documented as such in the report.
    """
    if power_mean_w is None or not output_tps:
        return {
            "available": False,
            "note": "Requires measured mean GPU power and output throughput",
        }
    seconds_per_million = 1_000_000 / output_tps
    return {
        "available": True,
        "gpuPowerMeanW": round(power_mean_w, 2),
        "secondsPerMillionTokens": round(seconds_per_million, 1),
        "whPerMillionTokens": round(power_mean_w * seconds_per_million / _SECONDS_PER_HOUR, 2),
        "note": "GPU power only (nvidia-smi); excludes CPU, cooling and PSU losses",
    }


def _degradation(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compares each concurrency level against the 1-request case."""
    out: list[dict[str, Any]] = []
    by_words: dict[int, list[dict[str, Any]]] = {}
    for scenario in scenarios:
        by_words.setdefault(scenario["promptWords"], []).append(scenario)

    for words, group in sorted(by_words.items()):
        baseline = next((s for s in group if s["concurrency"] == 1), None)
        if baseline is None:
            continue
        base_ttft = baseline["ttftS"]["p50"]
        base_decode = baseline["throughput"]["perRequestDecodeTokensPerS"]["p50"]
        base_output = baseline["throughput"]["outputTokensPerS"]
        for scenario in sorted(group, key=lambda s: s["concurrency"]):
            out.append(
                {
                    "promptWords": words,
                    "concurrency": scenario["concurrency"],
                    "outputTokensPerS": scenario["throughput"]["outputTokensPerS"],
                    "throughputSpeedupVsC1": _ratio(
                        scenario["throughput"]["outputTokensPerS"], base_output
                    ),
                    "ttftP50SlowdownVsC1": _ratio(scenario["ttftS"]["p50"], base_ttft),
                    "perRequestDecodeRetentionVsC1": _ratio(
                        scenario["throughput"]["perRequestDecodeTokensPerS"]["p50"], base_decode
                    ),
                }
            )
    return out


#: Below this throughput improvement, raising concurrency buys nothing.
_NO_SCALING_SPEEDUP = 1.2
#: Above this TTFT worsening, requests are waiting in a queue.
_QUEUEING_TTFT = 2.0
#: VRAM occupancy above which the deployment is running tight.
_VRAM_PRESSURE = 0.9


def _observations(
    scenarios: list[dict[str, Any]], degradation: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Translates the metrics into findings about the evaluated deployment.

    These are claims about the measured system, not limitations of the
    measurement: that is why they live here and not in `limitations`.
    """
    observations: list[dict[str, Any]] = []

    for row in degradation:
        speedup, slowdown = row["throughputSpeedupVsC1"], row["ttftP50SlowdownVsC1"]
        if row["concurrency"] == 1 or speedup is None or slowdown is None:
            continue
        if speedup < _NO_SCALING_SPEEDUP and slowdown > _QUEUEING_TTFT:
            observations.append(
                {
                    "kind": "no-concurrency-scaling",
                    "severity": "high",
                    "concurrency": row["concurrency"],
                    "promptWords": row["promptWords"],
                    "detail": (
                        f"At {row['concurrency']} simultaneous requests, throughput only improves "
                        f"x{speedup} while TTFT worsens x{slowdown}. The engine is queueing, not "
                        "serving in parallel: adding load only adds waiting. Check the server's "
                        "concurrent request limit."
                    ),
                }
            )

    for scenario in scenarios:
        peak = scenario["resources"].get("vramPeakMb")
        total = scenario["resources"].get("vramTotalMb")
        measurable = isinstance(peak, (int, float)) and isinstance(total, (int, float)) and total
        if measurable and peak / total >= _VRAM_PRESSURE:
            observations.append(
                {
                    "kind": "vram-at-limit",
                    "severity": "high",
                    "concurrency": scenario["concurrency"],
                    "promptWords": scenario["promptWords"],
                    "detail": (
                        f"VRAM peak {peak:.0f} MB out of {total:.0f} MB "
                        f"({peak / total:.0%}). No headroom for longer prompts or more "
                        "concurrency."
                    ),
                }
            )
        if scenario["requestsFailed"]:
            observations.append(
                {
                    "kind": "failed-requests",
                    "severity": "high",
                    "concurrency": scenario["concurrency"],
                    "promptWords": scenario["promptWords"],
                    "detail": (
                        f"{scenario['requestsFailed']} of {scenario['requestsRequested']} "
                        f"requests failed at concurrency {scenario['concurrency']}."
                    ),
                }
            )

    # The same symptom across several scenarios is reported once per kind and level.
    seen: set[tuple[str, int]] = set()
    unique = []
    for observation in observations:
        key = (observation["kind"], observation["concurrency"])
        if key not in seen:
            seen.add(key)
            unique.append(observation)
    return unique


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or not baseline:
        return None
    return round(value / baseline, 3)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _delta(peak: Any, idle: Any) -> float | None:
    if not isinstance(peak, (int, float)) or not isinstance(idle, (int, float)):
        return None
    return round(peak - idle, 2)
