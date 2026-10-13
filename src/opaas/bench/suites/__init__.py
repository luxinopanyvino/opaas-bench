"""Registry of evaluation suites.

Block A (Performance) is implemented today. Phase 3's blocks B–E get
registered here as they are implemented; `--suite all` will pick them up on its
own.

While blocks are missing, a run is not a complete evaluation. That is reflected
in `coverage` inside the report and travels all the way to the credential: a
certificate that said "evaluated" having measured only speed would be exactly
the kind of claim this project exists to prevent.
"""

from __future__ import annotations

from typing import Any

from opaas.bench.suites.base import Suite
from opaas.bench.suites.capability import CapabilitySuite
from opaas.bench.suites.compliance import ComplianceSuite
from opaas.bench.suites.consistency import ConsistencySuite
from opaas.bench.suites.performance import PerformanceSuite
from opaas.bench.suites.safety import SafetySuite

#: Blocks from the design document.
BLOCKS: dict[str, str] = {
    "A": "Performance",
    "B": "Capability / Quality",
    "C": "Consistency / Robustness",
    "D": "Behavior and safety",
    "E": "Compliance readiness",
}

_REGISTRY: dict[str, type[Suite]] = {
    PerformanceSuite.name: PerformanceSuite,
    CapabilitySuite.name: CapabilitySuite,
    ConsistencySuite.name: ConsistencySuite,
    SafetySuite.name: SafetySuite,
    ComplianceSuite.name: ComplianceSuite,
}

#: Suites designed but not implemented yet. All five blocks are covered; what
#: is missing inside each one is declared in its metrics (`notImplemented`) and
#: in its limitations, not here.
PLANNED_SUITES: dict[str, dict[str, str]] = {}


def available_suites() -> list[str]:
    return sorted(_REGISTRY)


def planned_suite_hint(name: str) -> str:
    plan = PLANNED_SUITES[name]
    return f"Block {plan['block']} — {plan['description']} ({plan['phase']})"


def get_suite(name: str, options: dict | None = None) -> Suite:
    try:
        suite_cls = _REGISTRY[name]
    except KeyError:
        if name in PLANNED_SUITES:
            raise KeyError(
                f"Suite '{name}' not implemented yet — {planned_suite_hint(name)}"
            ) from None
        raise KeyError(
            f"Unknown suite: {name!r}. Available: {', '.join(available_suites())}"
        ) from None
    return suite_cls(options or {})


def register_suite(suite_cls: type[Suite]) -> type[Suite]:
    _REGISTRY[suite_cls.name] = suite_cls
    return suite_cls


def coverage(executed: list[str]) -> dict[str, Any]:
    """Which design blocks this run actually covers.

    A block counts as unevaluated whether the suite does not exist yet or it
    exists but was not requested. To whoever reads the certificate the
    difference is irrelevant — that block was not measured — but it is
    distinguished in `status` because to us it does matter.
    """
    evaluated = sorted({_REGISTRY[name].block for name in executed if name in _REGISTRY})
    implemented_by_block = {suite_cls.block: name for name, suite_cls in _REGISTRY.items()}
    planned_by_block = {plan["block"]: name for name, plan in PLANNED_SUITES.items()}

    missing = []
    for block, label in sorted(BLOCKS.items()):
        if block in evaluated:
            continue
        if planned := planned_by_block.get(block):
            missing.append(
                {
                    "block": block,
                    "name": label,
                    "suite": planned,
                    "status": "not implemented",
                    "plannedIn": PLANNED_SUITES[planned]["phase"],
                }
            )
        else:
            missing.append(
                {
                    "block": block,
                    "name": label,
                    "suite": implemented_by_block.get(block),
                    "status": "not run",
                    "plannedIn": None,
                }
            )

    return {
        "complete": not missing,
        "blocksEvaluated": evaluated,
        "blocksMissing": missing,
    }


__all__ = [
    "BLOCKS",
    "PLANNED_SUITES",
    "CapabilitySuite",
    "ComplianceSuite",
    "ConsistencySuite",
    "PerformanceSuite",
    "SafetySuite",
    "Suite",
    "available_suites",
    "coverage",
    "get_suite",
    "planned_suite_hint",
    "register_suite",
]
