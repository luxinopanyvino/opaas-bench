"""Block B — Capability / Quality.

Runs a local dataset against any `InferenceBackend` and reports accuracy **per
category**, with a confidence interval. There is no aggregate score across
categories: mixing reasoning, science and instruction following into a single
number is exactly what the design rules out.

The dataset is a local, hashed artifact, not a download: the certificate page
shows exactly what was evaluated against and a third party can reproduce it
without a network.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import count
from typing import Any, ClassVar

from opaas.adapters.base import InferenceBackend
from opaas.bench.constraints import ConstraintError, check_all
from opaas.bench.datasets import Dataset, Item, Task, exclusion_limitations, load_dataset
from opaas.bench.scoring import (
    ANSWER_STYLES,
    ScoringError,
    extract_choice,
    prompt_template_hash,
    render_instruction,
    render_multiple_choice,
)
from opaas.bench.suites.base import Suite
from opaas.core.stats import wilson_interval

#: How many concrete failures are kept per task. Enough to debug without
#: turning the report into a dump of responses.
_FAILURE_SAMPLES = 5
_RESPONSE_EXCERPT = 240


#: Tokens per multiple-choice item depending on the style. In `direct` the
#: letter is enough; in `cot` there has to be room for the reasoning or it gets
#: cut off right before the conclusion, which is the worst possible error: it
#: looks like a model failure.
_CHOICE_TOKENS = {"direct": 24, "cot": 512}


@dataclass(slots=True)
class CapabilityOptions:
    #: The CLI default is a real dataset (see `datasets.DEFAULT_DATASET`).
    #: The smoke one is left here on purpose: programmatic use or a test that
    #: forgets to name a dataset must not hit the network unasked.
    dataset: str = "opaas-smoke"
    limit: int | None = None
    #: True forbids the network: cached datasets only.
    offline: bool = False
    #: 'cot' allows reasoning before answering; 'direct' demands the bare letter.
    answer_style: str = "cot"
    choice_max_tokens: int | None = None
    instruction_max_tokens: int = 400
    temperature: float = 0.0
    seed: int | None = 1234
    concurrency: int = 4

    def __post_init__(self) -> None:
        if self.answer_style not in ANSWER_STYLES:
            raise ScoringError(
                f"Unknown answer style: {self.answer_style!r}. "
                f"Available: {', '.join(ANSWER_STYLES)}"
            )
        if self.choice_max_tokens is None:
            self.choice_max_tokens = _CHOICE_TOKENS[self.answer_style]

    @classmethod
    def from_dict(cls, options: dict[str, Any]) -> CapabilityOptions:
        known = {f: options[f] for f in cls.__slots__ if options.get(f) is not None}
        return cls(**known)

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__slots__}


@dataclass(slots=True)
class _Outcome:
    """Result of one item: correct, incorrect, unparsed or error."""

    item_id: str
    correct: bool
    parsed: bool = True
    strategy: str = ""
    expected: str | None = None
    got: str | None = None
    excerpt: str = ""
    error: str | None = None
    constraint_details: list[dict[str, Any]] = field(default_factory=list)
    #: True when the engine stopped because it hit the token cap. The answer is
    #: cut, so a constraint it might have satisfied cannot be checked and the
    #: item counts as a miss for a reason that is the harness's, not the model's.
    truncated: bool = False


class CapabilitySuite(Suite):
    name: ClassVar[str] = "capability"
    block: ClassVar[str] = "B"
    description: ClassVar[str] = "Per-category accuracy on a local dataset, with 95% CI"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.opts = CapabilityOptions.from_dict(self.options)

    def evaluate(self, backend: InferenceBackend) -> tuple[dict[str, Any], list[str]]:
        dataset = load_dataset(self.opts.dataset, limit=self.opts.limit, offline=self.opts.offline)

        # Safety tasks live in the same dataset but are evaluated by block D.
        own_tasks = [
            task
            for task in dataset.tasks
            if task.type in ("multiple_choice", "instruction_following")
        ]
        if not own_tasks:
            raise ValueError(
                f"Dataset '{dataset.name}' has no multiple-choice or instruction-following "
                "tasks: block B has nothing to evaluate."
            )

        limitations = _dataset_limitations(dataset, own_tasks, self.opts)

        if backend.get_backend_metadata().get("promptMode") == "raw":
            limitations.append(
                "The backend is in 'raw' mode: the prompt does not go through the model's "
                "chat template, so this does NOT measure instruction following but text "
                "completion. Measured on IFEval with qwen2.5:7b, 88% with the template "
                "against 52% without it. Use --prompt-mode chat."
            )

        workers = self.opts.concurrency if backend.supports_concurrency else 1
        if workers != self.opts.concurrency:
            limitations.append(
                f"Backend '{backend.name}' does not support concurrency: items are evaluated "
                "serially. It does not affect the result, only the time."
            )

        self.plan_steps(sum(len(task.items) for task in own_tasks), "evaluating items")
        tasks = [self._run_task(backend, task, workers) for task in own_tasks]

        if failed := sum(task["errors"] for task in tasks):
            limitations.append(
                f"{failed} item(s) failed due to a backend error and are left out of the "
                "denominator: accuracy is computed only over the items that were evaluated."
            )
        for task in tasks:
            if task["truncated"]:
                # What a cut costs depends on the task: a multiple-choice answer
                # loses the letter and is scored unparsed; an instruction-following
                # one fails constraints it might have met. Saying the wrong one
                # misdescribes the very failure this line exists to report.
                cost = (
                    "the answer was cut before stating an option, so it is scored as unparsed"
                    if task["type"] == "multiple_choice"
                    else "a constraint the model might have satisfied cannot be checked, so the "
                    "item counts as a miss"
                )
                limitations.append(
                    f"[{task['name']}] {task['truncated']} of {task['items']} responses hit the "
                    f"token cap and were cut: {cost} — for a reason that is this harness's, not "
                    "the model's. Raise --capability-max-tokens."
                )
            if task["unparsed"]:
                limitations.append(
                    f"[{task['name']}] {task['unparsed']} of {task['items']} responses do not "
                    "state a recognizable option. They count as failures, but they may point "
                    "to a formatting problem rather than a knowledge one."
                )

        metrics = {
            "dataset": dataset.provenance(),
            "options": self.opts.as_dict(),
            "answerStyle": self.opts.answer_style,
            "promptTemplateHash": prompt_template_hash(self.opts.answer_style),
            "scoring": "generated text (not log-probabilities)",
            "tasks": tasks,
        }
        return metrics, limitations

    # ------------------------------------------------------------------ internal

    def _run_task(self, backend: InferenceBackend, task: Task, workers: int) -> dict[str, Any]:
        done = count(1)

        def evaluate(item: Item) -> _Outcome:
            outcome = self._evaluate_item(backend, item)
            self.step(detail=f"{task.name} · {next(done)}/{len(task.items)} items")
            return outcome

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # map preserves order: the report does not depend on arrival order.
                outcomes = list(pool.map(evaluate, task.items))
        else:
            outcomes = [evaluate(item) for item in task.items]

        scored = [o for o in outcomes if o.error is None]
        correct = sum(1 for o in scored if o.correct)
        unparsed = sum(1 for o in scored if not o.parsed)

        strategies: dict[str, int] = {}
        for outcome in scored:
            if outcome.strategy:
                strategies[outcome.strategy] = strategies.get(outcome.strategy, 0) + 1

        result: dict[str, Any] = {
            "name": task.name,
            "category": task.category,
            "type": task.type,
            "description": task.description,
            "items": len(scored),
            "correct": correct,
            "accuracy": wilson_interval(correct, len(scored)),
            "unparsed": unparsed,
            "truncated": sum(1 for o in scored if o.truncated),
            "errors": len(outcomes) - len(scored),
            "extractionStrategies": strategies,
            "failures": [
                {
                    "id": o.item_id,
                    "expected": o.expected,
                    "got": o.got,
                    "response": o.excerpt,
                }
                for o in scored
                if not o.correct
            ][:_FAILURE_SAMPLES],
            "errorSamples": [o.error for o in outcomes if o.error][:3],
        }
        if task.type == "instruction_following":
            result["constraintBreakdown"] = _constraint_breakdown(scored)
        return result

    def _evaluate_item(self, backend: InferenceBackend, item: Item) -> _Outcome:
        if item.type == "multiple_choice":
            prompt = render_multiple_choice(item.prompt, item.choices, self.opts.answer_style)
            max_tokens = item.max_tokens or self.opts.choice_max_tokens
        else:
            prompt = render_instruction(item.prompt)
            max_tokens = item.max_tokens or self.opts.instruction_max_tokens

        try:
            result = backend.generate(
                prompt,
                max_tokens=max_tokens,
                temperature=self.opts.temperature,
                seed=self.opts.seed,
                stream=backend.supports_streaming,
            )
        except Exception as exc:  # noqa: BLE001 - an engine failure is data, not an exception
            return _Outcome(
                item_id=item.id,
                correct=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        text = result.text
        truncated = result.finish_reason == "length"
        if item.type == "multiple_choice":
            outcome = _score_choice(item, text)
        else:
            outcome = _score_instruction(item, text)
        outcome.truncated = truncated
        return outcome


def _score_choice(item: Item, text: str) -> _Outcome:
    extracted = extract_choice(text, len(item.choices))
    expected = (item.answer or "").strip().upper()
    return _Outcome(
        item_id=item.id,
        correct=extracted.parsed and extracted.letter == expected,
        parsed=extracted.parsed,
        strategy=extracted.strategy,
        expected=expected,
        got=extracted.letter,
        excerpt=_excerpt(text),
    )


def _score_instruction(item: Item, text: str) -> _Outcome:
    try:
        ok, details = check_all(text, item.constraints)
    except ConstraintError as exc:
        # This is a dataset failure, not a model one: it can count neither as a hit nor as an error.
        return _Outcome(item_id=item.id, correct=False, error=f"Invalid dataset: {exc}")
    failed = [detail["kind"] for detail in details if not detail["ok"]]
    return _Outcome(
        item_id=item.id,
        correct=ok,
        expected="meets all constraints",
        got="fails: " + ", ".join(failed) if failed else "meets",
        excerpt=_excerpt(text),
        constraint_details=details,
    )


def _constraint_breakdown(outcomes: list[_Outcome]) -> list[dict[str, Any]]:
    """Accuracy per constraint kind: it says *which* instructions the model ignores."""
    totals: dict[str, list[int]] = {}
    for outcome in outcomes:
        for detail in outcome.constraint_details:
            kind = str(detail["kind"])
            counters = totals.setdefault(kind, [0, 0])
            counters[0] += int(bool(detail["ok"]))
            counters[1] += 1
    return [
        {"kind": kind, "passed": passed, "total": total, "rate": wilson_interval(passed, total)}
        for kind, (passed, total) in sorted(totals.items())
    ]


def _dataset_limitations(
    dataset: Dataset, tasks: list[Task], opts: CapabilityOptions
) -> list[str]:
    limitations: list[str] = []
    if dataset.synthetic_smoke:
        limitations.append(
            f"Dataset '{dataset.name}' is a SMOKE dataset: it validates the pipeline, it does "
            "not measure capability. The results of this block are NOT a quality evaluation "
            "and should not be certified. See docs/datasets.md to install real subsets."
        )
    if opts.limit:
        limitations.append(
            f"Each task was trimmed to {opts.limit} items: the confidence interval reflects "
            "that sample, not the full dataset."
        )
    if dataset.item_count < 100:
        limitations.append(
            f"Only {dataset.item_count} items in total: with samples this small the confidence "
            "interval is wide and differences between models are rarely significant."
        )
    limitations.extend(exclusion_limitations(tasks))
    limitations.append(
        "Accuracy is scored on the generated text, not on log-probabilities: the figures are "
        "not directly comparable with those published by harnesses that score by likelihood."
    )
    if opts.answer_style == "direct":
        limitations.append(
            "Answer style 'direct': the bare letter is required, with no reasoning. It heavily "
            "penalizes small models on arithmetic tasks and partly measures format compliance. "
            "Not comparable with a 'cot' run."
        )
    return limitations


def _excerpt(text: str) -> str:
    body = " ".join(text.split())
    return body if len(body) <= _RESPONSE_EXCERPT else body[:_RESPONSE_EXCERPT] + "…"
