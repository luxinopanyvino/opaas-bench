"""Scripted, deterministic fault injection and what the record shows of it (D51).

A fault plan names which calls to disturb and how — one model slow, erroring,
or answering with a malformed tool call — and on which occurrence: every
k-th matching call, at most n times. Counters, not randomness: the same
traffic gets the same faults, so a run can be repeated and compared.

The faults are injected by the sentinel proxy (`--faults`), which records
each one in the event it disturbed (`extra.injectedFault`). That record is
the ground truth for this analysis, as the mock tools are for `smith`.

For each injected fault, what followed in the same trace (or, without a
trace, from the same actor chain):

- `contained`: a later call succeeded within the retry budget, and nothing
  else failed;
- `cascaded`: calls to *other* targets failed after it;
- `unrecovered`: no later success within the budget;
- `no-follow-up`: nothing came after it at all.

Each case is also a lifecycle-coverage test: a recovery the record shows only
as another `attempt` — no `retry` or `fallback` phase — is reported as
recovered but not visible in the lifecycle.
"""

from __future__ import annotations

import fnmatch
import threading
from collections.abc import Sequence
from typing import Any

FAULT_KINDS = ("slow", "error", "malformed")


class FaultPlanError(ValueError):
    """The fault plan is malformed."""


class FaultPlan:
    """A `FaultInjector` for the sentinel proxy."""

    def __init__(self, raw: Any) -> None:
        items = raw.get("faults") if isinstance(raw, dict) else None
        if not isinstance(items, list) or not items:
            raise FaultPlanError("A fault plan is an object with a non-empty 'faults' list.")
        self.faults: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if item.get("kind") not in FAULT_KINDS:
                raise FaultPlanError(f"faults[{index}].kind is one of {', '.join(FAULT_KINDS)}.")
            every = int(item.get("every", 1))
            if every < 1:
                raise FaultPlanError(f"faults[{index}].every must be at least 1.")
            self.faults.append({
                "id": str(item.get("id") or f"f{index}"),
                "kind": item["kind"],
                "action": item.get("action", "*"),
                "target": item.get("target", "*"),
                "every": every,
                "limit": int(item["limit"]) if "limit" in item else None,
                **{k: item[k] for k in ("delayMs", "status") if k in item},
            })
        self._seen = [0] * len(self.faults)
        self._injected = [0] * len(self.faults)
        self._lock = threading.Lock()

    def pick(self, action: str, target: str | None) -> dict[str, Any] | None:
        with self._lock:
            for index, fault in enumerate(self.faults):
                if not (fnmatch.fnmatchcase(action, fault["action"])
                        and fnmatch.fnmatchcase(str(target), fault["target"])):
                    continue
                self._seen[index] += 1
                if self._seen[index] % fault["every"]:
                    continue
                if fault["limit"] is not None and self._injected[index] >= fault["limit"]:
                    continue
                self._injected[index] += 1
                return {k: v for k, v in fault.items() if k not in ("action", "target")} | {
                    "occurrence": self._seen[index]}
        return None


def _scope(record: dict[str, Any]) -> tuple[str, str]:
    trace = (record.get("trace") or {}).get("traceId")
    if trace:
        return "trace", str(trace)
    return "chain", str(record.get("actorChain"))


def analyse_faults(records: Sequence[dict[str, Any]], *, retry_budget: int = 3
                   ) -> dict[str, Any]:
    cases = []
    for position, record in enumerate(records):
        fault = (record.get("extra") or {}).get("injectedFault")
        if not fault:
            continue
        scope = _scope(record)
        after = [r for r in records[position + 1:] if _scope(r) == scope]
        if not after:
            cases.append({"event": record.get("id"), "fault": fault, "outcome": "no-follow-up"})
            continue
        window = after[:retry_budget]
        same = [r for r in window if r.get("target") == record.get("target")
                or r.get("phase") == "fallback"]
        recovered = any(not r.get("error") for r in same)
        others_failed = [r.get("id") for r in after
                         if r.get("error") and r.get("target") != record.get("target")
                         and not (r.get("extra") or {}).get("injectedFault")]
        visible = any(r.get("phase") in ("retry", "fallback") for r in same)
        if others_failed:
            outcome = "cascaded"
        elif recovered:
            outcome = "contained"
        else:
            outcome = "unrecovered"
        cases.append({
            "event": record.get("id"),
            "fault": fault,
            "target": record.get("target"),
            "outcome": outcome,
            "followUps": len(same),
            "recoveryVisibleInLifecycle": visible if same else None,
            "cascadedTo": others_failed[:20],
        })
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["outcome"]] = counts.get(case["outcome"], 0) + 1
    hidden = [c["event"] for c in cases
              if c["outcome"] == "contained" and c.get("recoveryVisibleInLifecycle") is False]
    return {
        "retryBudget": retry_budget,
        "injected": len(cases),
        "outcomes": dict(sorted(counts.items())),
        "recoveredButNotVisible": hidden,
        "cases": cases,
    }


__all__ = ["FAULT_KINDS", "FaultPlan", "FaultPlanError", "analyse_faults"]
