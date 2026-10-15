"""`opaas smith`: scenarios with deterministic oracles."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from opaas.adapters import get_backend
from opaas.adapters.base import BackendError
from opaas.cli.common import _plain, console, err_console
from opaas.core.evidence import EvidenceError, envelope, read_document, write_document
from opaas.core.fingerprint import deployment_fingerprint
from opaas.core.report import describe_environment
from opaas.smith import BareModelAgent, HttpAgent, McpAgent, ScenarioError, available_agents
from opaas.smith.runner import build_report, run_scenarios
from opaas.smith.scenario import load_scenarios
from opaas.smith.toxicity import score_agent_toxicity

app = typer.Typer(help="Fixed scenarios, mock tools, deterministic oracles.",
                  no_args_is_help=True)

Scenarios = Annotated[str, typer.Option(
    "--scenarios", "-s", help="'reference', a directory of scenario JSON files, or one file.")]


@app.command("scenarios")
def scenarios_command(scenarios: Scenarios = "reference") -> None:
    """Lists and validates scenarios, with their hashes."""
    try:
        loaded = load_scenarios(scenarios)
    except ScenarioError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    table = Table("id", "category", "tools", "policies", "steps", "hash")
    for s in loaded:
        table.add_row(s.id, s.category, str(len(s.tools)), str(len(s.policies)),
                      str(s.max_steps), s.hash[:23] + "…")
    console.print(table)
    console.print(f"{len(loaded)} scenario(s) valid.")


@app.command("run")
def run_command(
    agent: Annotated[str, typer.Option("--agent", "-a",
                                       help=f"Adapter: {', '.join(available_agents())}.")],
    scenarios: Scenarios = "reference",
    repeats: Annotated[int, typer.Option("--repeats", "-n", min=1,
                                         help="Runs per scenario; 2+ measures instability.")]
    = 3,
    endpoint: Annotated[str | None, typer.Option(
        "--endpoint", help="Agent URL (http adapter).")] = None,
    backend: Annotated[str | None, typer.Option(
        "--backend", "-b", help="Engine adapter (bare-model).")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model (bare-model).")]
    = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Engine URL.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", envvar="OPAAS_API_KEY",
                                                help="Engine key.")] = None,
    inventory: Annotated[Path | None, typer.Option(
        "--inventory", help="`discover scan` document to bind the result to (N5).")] = None,
    toxicity_model: Annotated[Path | None, typer.Option(
        "--toxicity-model", help="Toxicity classifier weights for the agent's answers; "
        "without it the pinned default is used if the classifiers extra is installed.")]
    = None,
    toxicity: Annotated[bool, typer.Option(
        "--toxicity/--no-toxicity", help="Score the agent's final answers for toxicity.")]
    = True,
    keep_finals: Annotated[bool, typer.Option(
        "--keep-finals", help="Write the agent's final answers into the report. By default "
        "only their digest and length are written (N4): the report is a public document.")]
    = False,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("smith.json"),
) -> None:
    """Runs scenarios against an agent: behaviour and auditability, per category, no score."""
    try:
        loaded = load_scenarios(scenarios)
        inventory_doc = read_document(inventory, kind="inventory")["inventory"] \
            if inventory else None
    except (ScenarioError, EvidenceError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    environment: dict[str, Any] = {}
    if inventory_doc:
        environment["inventory"] = inventory_doc
    engine = None
    if agent in ("http", "mcp"):
        if not endpoint:
            err_console.print(f"[red]--agent {agent} needs --endpoint.[/red]")
            raise typer.Exit(code=2)
        adapter: Any = HttpAgent(endpoint) if agent == "http" else McpAgent(endpoint)
    elif agent == "bare-model":
        if not backend or not model:
            err_console.print("[red]--agent bare-model needs --backend and --model.[/red]")
            raise typer.Exit(code=2)
        try:
            engine = get_backend(backend)
            engine.load(model, {"base_url": base_url, "api_key": api_key})
        except BackendError as exc:
            err_console.print(f"[red]{_plain(exc)}[/red]")
            raise typer.Exit(code=1) from None
        environment["deployment"] = deployment_fingerprint(
            engine, engine.get_backend_metadata(), describe_environment())
        adapter = BareModelAgent(engine)
    else:
        err_console.print(f"[red]Unknown agent adapter '{agent}'. "
                          f"Known: {', '.join(available_agents())}.[/red]")
        raise typer.Exit(code=2)

    try:
        outcomes = run_scenarios(
            loaded, adapter, repeats=repeats, keep_finals=keep_finals,
            progress=lambda sid, i, n: console.print(f"[dim]{i}/{n} {sid}[/dim]"),
        )
    finally:
        if engine is not None:
            engine.close()
    answers = [o.final_text for o in outcomes if o.final_text]
    scored = (score_agent_toxicity(answers, model=toxicity_model) if toxicity
              else {"measured": False, "reason": "Skipped with --no-toxicity."})
    report = build_report(loaded, adapter, outcomes, repeats=repeats, toxicity=scored,
                          keep_finals=keep_finals)
    digest = write_document(out, envelope("smith-report", report, environment=environment))
    _print(report)
    console.print(f"\n{out} ({digest})")


def _fmt(metric: dict[str, Any]) -> str:
    if not metric.get("measured", True):
        return f"[dim]not measured: {_plain(metric.get('reason'))}[/dim]"
    if metric.get("point") is None:
        return "—"
    low, high = metric.get("low"), metric.get("high")
    interval = f" [{low:.2f}, {high:.2f}]" if low is not None and high is not None else ""
    return f"{metric['point']:.2f}{interval} (n={metric.get('n')})"


def _print(report: dict[str, Any]) -> None:
    console.print("\n[bold]Behaviour[/bold] (against the mock-tool ground truth)")
    for name, metric in report["behaviour"].items():
        if isinstance(metric, dict):
            console.print(f"  {name:<32} {_fmt(metric)}")
    console.print("\n[bold]Auditability[/bold] (the agent's own record)")
    audit = report["auditability"]
    for name in ("ACR", "RF", "LPC", "GB", "SPDR"):
        if name in audit:
            console.print(f"  {name:<32} {_fmt(audit[name])}")
    if "AC" in audit and audit["AC"].get("measured", True) and "AC" in audit["AC"]:
        console.print(f"  {'AC':<32} {_fmt(audit['AC']['AC'])}")
    if audit.get("misleadingVerdicts"):
        console.print(f"  [red]misleading record[/red] for {audit['misleadingVerdicts']}")
    console.print(f"\n  instability {_fmt(report['instability'])}")
    console.print("\n[bold]Agent-level bias and toxicity[/bold]")
    bias = report.get("agentBias") or {}
    if bias.get("measured"):
        console.print(f"  {'counterfactual divergence':<32} {_fmt(bias['divergentRate'])}")
        for row in bias["scenarios"]:
            reason = f" — {_plain(row['reason'])}" if row.get("reason") else ""
            console.print(f"    {_plain(row['scenario'])} ({row['attribute']}): "
                          f"{row['verdict']}{reason}")
    else:
        console.print(f"  {'counterfactual divergence':<32} {_fmt(bias)}")
    toxicity = report.get("agentToxicity") or {}
    shown = toxicity["aboveThresholdRate"] if toxicity.get("measured") else toxicity
    console.print(f"  {'toxic answers':<32} {_fmt(shown)}")
    for line in report["limitations"]:
        console.print(f"[yellow]limitation:[/yellow] {_plain(line)}")
