"""Progress and elapsed time of a run.

They are decoration, not measurement: what these tests protect is that the
decoration never lies (a % that goes backwards or never reaches the end) and
that it never gets in the way of the run.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from opaas.adapters import register_backend
from opaas.cli import main
from opaas.runner import RunEvent, RunRequest, execute, format_elapsed
from opaas.suites import register_suite
from opaas.suites.base import Suite

from .conftest import FakeBackend

OPTIONS = {
    "performance": {
        "concurrency": [1],
        "prompt_words": [32],
        "max_tokens": 8,
        "requests_per_setting": 2,
        "warmup_requests": 1,
        "sample_interval_s": 5.0,
        "idle_seconds": 0.0,
    },
    "compliance": {},
}


@pytest.fixture(autouse=True)
def _register_fake():
    register_backend(FakeBackend)


def _events(tmp_path, suites=("performance", "compliance")) -> list[RunEvent]:
    collected: list[RunEvent] = []
    execute(
        RunRequest(
            model="fake/model-1b",
            backend="fake",
            suites=list(suites),
            suite_options=OPTIONS,
            output=tmp_path / "result.json",
        ),
        on_event=collected.append,
    )
    return collected


def test_progress_never_goes_backwards_and_finishes_at_100(tmp_path):
    percents = [event.percent for event in _events(tmp_path)]
    assert percents == sorted(percents)
    assert percents[-1] == pytest.approx(100.0)


def test_every_suite_reports_progress_while_it_runs(tmp_path):
    events = _events(tmp_path)
    reporting = {event.suite for event in events if event.kind == "progress"}
    assert reporting == {"performance", "compliance"}
    # Not only at the boundaries: the point is seeing movement inside the block.
    assert sum(1 for event in events if event.kind == "progress") > 4


def test_the_elapsed_time_grows_and_reads_as_a_clock(tmp_path):
    events = _events(tmp_path)
    elapsed = [event.elapsed_s for event in events]
    assert elapsed == sorted(elapsed)
    assert elapsed[0] >= 0.0
    assert re.match(r"^\[\s*\d+\.\d% · \d\d:\d\d\] ", str(events[-1]))


def test_suites_are_weighed_equally(tmp_path):
    events = _events(tmp_path)
    ends = [event.percent for event in events if event.kind == "suite-end"]
    assert ends == [pytest.approx(50.0), pytest.approx(100.0)]


def test_each_event_says_which_block_of_how_many_it_belongs_to(tmp_path):
    events = _events(tmp_path)
    positions = {
        (event.suite, event.index) for event in events if event.kind != "info"
    }
    assert positions == {("performance", 1), ("compliance", 2)}
    assert all(event.total == 2 for event in events)


def test_a_failed_suite_still_advances_the_run(tmp_path):
    register_suite(_Broken)
    events = _events(tmp_path, suites=["compliance", "broken"])
    assert any(event.kind == "suite-end" and "failed" in event.message for event in events)
    assert events[-1].percent == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00"), (9.9, "00:09"), (75, "01:15"), (3600, "1:00:00"), (3725, "1:02:05")],
)
def test_format_elapsed(seconds, expected):
    assert format_elapsed(seconds) == expected


class _Countdown(Suite):
    name = "countdown"
    block = "A"

    def evaluate(self, backend):  # pragma: no cover - driven by the tests below
        return {}, []


class _Broken(Suite):
    name = "broken"
    block = "A"

    def evaluate(self, backend):
        raise RuntimeError("the block blew up halfway through")


def test_step_stays_within_the_block_and_keeps_the_last_detail():
    seen: list[tuple[float, str]] = []
    suite = _Countdown()
    suite.set_progress_callback(lambda fraction, detail: seen.append((fraction, detail)))

    suite.plan_steps(2, "starting")
    suite.step(detail="first half")
    suite.step()
    # More steps than planned: an estimate that falls short must not report 150%.
    suite.step()

    assert seen == [
        (0.0, "starting"),
        (0.5, "first half"),
        (1.0, "first half"),
        (1.0, "first half"),
    ]


def test_a_suite_without_a_callback_runs_the_same():
    suite = _Countdown()
    suite.plan_steps(3)
    suite.step()  # No callback: nothing is emitted and nothing blows up.
    assert suite._steps_done == 1


def test_without_a_terminal_the_progress_lines_are_throttled(monkeypatch):
    """A run redirected to a log must not write one line per request."""
    buffer = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buffer, width=120))

    with main._progress_reporter(1) as report:
        report(RunEvent("loading the model", "info"))
        for index in range(50):
            report(RunEvent(f"item {index}", "progress", percent=2.0 * index))
        report(RunEvent("Suite 'performance' completed in 1s", "suite-end", 100.0))

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3
    assert "loading the model" in lines[0]
    assert "item 0" in lines[1]
    assert "completed" in lines[2]
