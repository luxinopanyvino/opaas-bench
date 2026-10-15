"""`opaas audit run`: one command, only what the manifest calls for (D60, phase S1).

The plan follows the manifest, in the order every later result depends on:

1. `discover` — the inventory and the gap scan. Everything else binds to it (N5).
2. `bench` — blocks A–E for each `model` component.
3. `smith` — the reference scenarios, and the operator's own, for each `agent`.
4. `sentinel` — the overhead of each `proxy`.
5. `inquest` — each record source, and the same records degraded (Q6).
6. `swarm` — the declared flows against the analysed records.

Each step runs the module's own command, with the module's own output file:
the member document is exactly what `opaas run` or `opaas smith run` would
have written, and keeps its hash. A document the operator already has is
supplied instead (`--member component=path`) and that step is not run again:
a bench run takes hours, and the one that was certified is the one to cite.

A step skipped by the operator, one that failed, and an agent whose adapter
does not exist yet (MCP) are all recorded with their reason, and their cells
read *not measured* in the report. Nothing is left out silently.
"""

from __future__ import annotations

import glob
import shlex
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opaas.system.manifest import PENDING_ADAPTERS, SystemManifest
from opaas.system.report import SYSTEM_ROW, Member, StepOutcome, flow_edge

STEPS = ("discover", "bench", "smith", "sentinel", "inquest", "swarm")

#: Member document kind → the step it stands in for.
STEP_OF_KIND = {
    "inventory": "discover",
    "gap-scan": "discover",
    "model-report": "bench",
    "smith-report": "smith",
    "sentinel-overhead": "sentinel",
    "inquest-audit": "inquest",
    "swarm-analysis": "swarm",
    "auditability-card": "card",
}

#: Degradation used for Q6: every tenth record lost. Mild on purpose: the
#: question is whether the audit's verdicts survive a realistic loss.
DEGRADE = "drop-every:10"

#: Principals start chains; they are not components. An edge from one to an
#: agent or a model is the system being used, not an undeclared flow.
_PRINCIPALS = ("user:*", "service:*", "schedule:*")

Invoke = Callable[[list[str]], int]


class AuditPlanError(ValueError):
    """The audit cannot be planned: a member or an option does not fit the manifest."""


@dataclass(slots=True)
class AuditRun:
    out: Path
    members: list[Member] = field(default_factory=list)
    steps: list[StepOutcome] = field(default_factory=list)
    inventory: Path | None = None


def run_audit(
    manifest: SystemManifest,
    *,
    base: Path,
    out: Path,
    invoke: Invoke,
    supplied: Sequence[tuple[str, Path, str]] = (),
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    bench_args: str = "",
    repeats: int = 3,
) -> AuditRun:
    """Runs the plan. `supplied`: (component, path, kind) the operator already has.

    `invoke` runs one OPAAS command line and returns its exit code; the CLI
    passes the app itself, tests pass the same app with fake backends.
    """
    for name in (*only, *skip):
        if name not in STEPS:
            raise AuditPlanError(f"Unknown step '{name}'. Steps: {', '.join(STEPS)}.")
    out.mkdir(parents=True, exist_ok=True)
    run = AuditRun(out=out)
    #: (component, document kind) the operator supplied: that document is not produced again.
    have: set[tuple[str, str]] = set()
    for component, path, kind in supplied:
        step = STEP_OF_KIND.get(kind, kind)
        have.add((component, kind))
        run.members.append(Member(component=component, step=step, path=path))
        run.steps.append(StepOutcome(step, component, "supplied", path.name))
        if kind == "inventory":
            run.inventory = path

    def wanted(step: str) -> str | None:
        """None if the step runs; otherwise why it does not."""
        if only and step not in only:
            return "not in --only"
        if step in skip:
            return "skipped by the operator"
        return None

    def attempt(step: str, component: str, kind: str, args: list[str], output: Path) -> bool:
        if (component, kind) in have:
            return False
        if (reason := wanted(step)) is not None:
            run.steps.append(StepOutcome(step, component, "skipped", reason))
            return False
        code = invoke(args)
        if output.is_file():
            run.members.append(Member(component=component, step=step, path=output))
            run.steps.append(StepOutcome(step, component, "ran",
                                         None if code == 0 else f"exit code {code}"))
            return True
        run.steps.append(StepOutcome(step, component, "failed",
                                     f"`opaas {' '.join(args[:2])}` exited {code} and wrote "
                                     "no document"))
        return False

    records = [_resolve_record(r, base) for r in manifest.records]

    # 1. discover
    inventory = out / "inventory.json"
    if attempt("discover", SYSTEM_ROW, "inventory",
               ["discover", "scan", "--out", str(inventory)], inventory):
        run.inventory = inventory
    if run.inventory is not None:
        gaps = out / "gaps.json"
        logs = [arg for r in records for arg in ("--log", str(r["path"]))]
        attempt("discover", SYSTEM_ROW, "gap-scan",
                ["discover", "gaps", "--inventory", str(run.inventory), *logs, "--out", str(gaps)],
                gaps)
    bind = ["--inventory", str(run.inventory)] if run.inventory else []

    # 2. bench
    for component in manifest.components:
        if component.kind != "model":
            continue
        report = out / f"bench-{component.id}.json"
        extra = shlex.split(bench_args)
        args = ["run", "--model", str(component.fields["model"]),
                "--backend", str(component.fields["backend"]),
                # Every block, unless the operator chose which in --bench-args.
                *([] if "--suite" in extra else ["--suite", "all"]),
                "--output", str(report)]
        for name, flag in (("base_url", "--base-url"), ("num_ctx", "--num-ctx")):
            if component.fields.get(name) is not None:
                args += [flag, str(component.fields[name])]
        attempt("bench", component.id, "model-report", [*args, *extra], report)

    # 3. smith
    scenarios = _collect_scenarios(manifest.scenarios, base, out)
    for component in manifest.components:
        if component.kind != "agent":
            continue
        adapter = component.fields["adapter"]
        if adapter in PENDING_ADAPTERS:
            run.steps.append(StepOutcome(
                "smith", component.id, "pending",
                f"the {adapter} agent adapter is not implemented yet; this agent will be "
                "measured once it is, with this same manifest"))
            continue
        target = _agent_args(manifest, component)
        if target is None:
            run.steps.append(StepOutcome(
                "smith", component.id, "failed",
                "a bare-model agent needs backend and model, its own or those of the model "
                "it flows to"))
            continue
        sets = [("reference", "reference")] + ([("operator", str(scenarios))] if scenarios
                                                else [])
        for label, source in sets:
            report = out / f"smith-{component.id}-{label}.json"
            attempt("smith", component.id, "smith-report",
                    ["smith", "run", *target, "--scenarios", source, "--repeats", str(repeats),
                     *bind, "--out", str(report)], report)
        if not scenarios and (component.id, "smith-report") not in have:
            run.steps.append(StepOutcome(
                "smith", component.id, "skipped",
                "no operator scenarios in the manifest: only the reference set ran, which is "
                "not certifiable for this deployment (D47). Scenario templates generated from "
                "the manifest arrive in phase S4"))

    # 4. sentinel
    for component in manifest.components:
        if component.kind == "proxy":
            overhead = out / f"overhead-{component.id}.json"
            attempt("sentinel", component.id, "sentinel-overhead",
                    ["sentinel", "overhead", *bind, "--out", str(overhead)], overhead)

    # 5. inquest, and 6. swarm
    policies = _resolve(manifest.policies, base)
    for index, record in enumerate(records, start=1):
        owner = record.get("component") or SYSTEM_ROW
        common = [str(record["path"]), "--format", str(record["source"])]
        if record.get("key"):
            common += ["--key", str(record["key"])]
        audit = out / f"inquest-{index}.json"
        extra = ["--policies", str(policies)] if policies else []
        attempt("inquest", owner, "inquest-audit", ["inquest", "audit", *common, *extra, *bind,
                                   "--out", str(audit)], audit)
        degraded = out / f"inquest-{index}-degraded.json"
        if (owner, "inquest-audit") not in have:
            attempt("inquest", owner, "inquest-audit",
                    ["inquest", "audit", *common, *extra, "--degrade", DEGRADE, *bind,
                     "--out", str(degraded)], degraded)
        if manifest.flows:
            topology = out / "topology.json"
            topology.write_text(_topology(manifest), encoding="utf-8")
            analysis = out / f"swarm-{index}.json"
            swarm_args = [str(record["path"]), "--format", str(record["source"]),
                          *(["--key", str(record["key"])] if record.get("key") else [])]
            attempt("swarm", SYSTEM_ROW, "swarm-analysis",
                    ["swarm", "analyze", *swarm_args, "--topology", str(topology), *bind,
                     "--out", str(analysis)], analysis)
    if not records:
        for step in ("inquest", "swarm"):
            run.steps.append(StepOutcome(step, SYSTEM_ROW, "skipped",
                                         "the manifest names no records to audit"))
    return run


def _agent_args(manifest: SystemManifest, component: Any) -> list[str] | None:
    adapter = component.fields["adapter"]
    if adapter == "http":
        return ["--agent", "http", "--endpoint", str(component.fields["endpoint"])]
    backend, model = component.fields.get("backend"), component.fields.get("model")
    base_url = component.fields.get("base_url")
    if not (backend and model):
        for flow in manifest.flows:
            target = manifest.component(flow["to"]) if flow["from"] == component.id else None
            if target is not None and target.kind == "model":
                backend, model = target.fields["backend"], target.fields["model"]
                base_url = target.fields.get("base_url")
                break
    if not (backend and model):
        return None
    args = ["--agent", adapter, "--backend", str(backend), "--model", str(model)]
    return [*args, "--base-url", str(base_url)] if base_url else args


def _collect_scenarios(patterns: Sequence[str], base: Path, out: Path) -> Path | None:
    """The operator's scenarios, gathered into one directory smith can load."""
    files = sorted({Path(match) for pattern in patterns
                    for match in glob.glob(str(base / pattern), recursive=True)
                    if Path(match).is_file()})
    if not files:
        return None
    directory = out / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    for file in files:
        shutil.copyfile(file, directory / file.name)
    return directory


def _topology(manifest: SystemManifest) -> str:
    import json

    edges = [flow_edge(flow, manifest) for flow in manifest.flows]
    entries = sorted({e["from"] for e in edges if e["from"].startswith(("agent:", "model:"))}
                     | {e["to"] for e in edges if e["to"].startswith(("agent:", "model:"))})
    edges += [{"from": principal, "to": entry} for principal in _PRINCIPALS for entry in entries]
    return json.dumps({"edges": edges}, indent=2)


def _resolve(path: str | None, base: Path) -> Path | None:
    if path is None:
        return None
    resolved = (base / path) if not Path(path).is_absolute() else Path(path)
    if not resolved.is_file():
        raise AuditPlanError(f"The manifest names {path}, which does not exist.")
    return resolved


def _resolve_record(record: dict[str, Any], base: Path) -> dict[str, Any]:
    resolved = dict(record)
    resolved["path"] = _resolve(record["path"], base)
    if record.get("key"):
        resolved["key"] = _resolve(record["key"], base)
    return resolved


__all__ = ["DEGRADE", "STEPS", "STEP_OF_KIND", "AuditPlanError", "AuditRun", "run_audit"]
