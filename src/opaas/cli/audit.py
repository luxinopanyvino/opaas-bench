"""`opaas audit`: the system audit (D60).

`run` audits a system from its manifest and writes the system report (S1).
`validate` checks a manifest; `extract` reads any OPAAS document into the
common measurement format (S0). None of them writes over a document: each
member keeps its bytes and its hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from opaas.bench.declaration import DECLARED_REQUIREMENTS, DeclarationError, load_declaration
from opaas.bench.suites.compliance import declared_item
from opaas.cli.common import _plain, console, err_console, read_json
from opaas.core.evidence import envelope, write_document
from opaas.core.hashing import file_digest
from opaas.smith.agents import available_agents
from opaas.system import (
    ExtractionError,
    SystemManifestError,
    document_kind,
    extract,
    load_manifest,
)
from opaas.system.orchestrate import STEPS, AuditPlanError, run_audit
from opaas.system.report import SYSTEM_ROW, SystemReportError, build_report, load_thresholds

app = typer.Typer(help="System audit: manifest and measurements (D60).", no_args_is_help=True)

_STATUS_STYLE = {"measured": "green", "not-measured": "yellow", "not-applicable": "dim"}
_STATUS_MARK = {"measured": "[green]●[/green]", "not-measured": "[yellow]○[/yellow]",
                "not-applicable": "[dim]·[/dim]"}


@app.command("run")
def run_command(
    system: Annotated[Path, typer.Argument(help="System manifest (JSON).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Directory for every document.")]
    = Path("system-audit"),
    member: Annotated[list[str], typer.Option(
        "--member", "-m", help="component=path: a document you already have (a certified "
        "bench report, say). That step is not run again for that component. Repeatable.")]
    = None,
    only: Annotated[list[str], typer.Option(
        "--only", help=f"Run only these steps: {', '.join(STEPS)}. Repeatable.")] = None,
    skip: Annotated[list[str], typer.Option(
        "--skip", help="Steps not to run; their cells read not measured. Repeatable.")] = None,
    bench_args: Annotated[str, typer.Option(
        "--bench-args", help="Extra `opaas run` options for every model, quoted.")] = "",
    repeats: Annotated[int, typer.Option("--repeats", "-n", min=1,
                                         help="smith runs per scenario.")] = 3,
) -> None:
    """Audits a system: runs only what its manifest calls for, then writes the system report."""
    base = system.resolve().parent
    try:
        manifest = load_manifest(read_json(system), known_adapters=tuple(available_agents()))
        supplied = [_supplied(entry, manifest) for entry in member or []]
        thresholds = (load_thresholds(read_json(base / manifest.thresholds))
                      if manifest.thresholds else None)
        declared = None
        if manifest.declaration:
            declaration = load_declaration(base / manifest.declaration)
            declared = [declared_item(r, declaration) for r in DECLARED_REQUIREMENTS]
    except (SystemManifestError, SystemReportError, DeclarationError, ExtractionError,
            OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    try:
        run = run_audit(manifest, base=base, out=out, invoke=_invoke, supplied=supplied,
                        only=only or [], skip=skip or [], bench_args=bench_args,
                        repeats=repeats)
        body = build_report(manifest, manifest_hash=file_digest(system), members=run.members,
                            steps=run.steps, thresholds=thresholds, declared_items=declared)
    except (AuditPlanError, SystemReportError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    inventory = read_json(run.inventory)["inventory"] if run.inventory else None
    report = out / "system-report.json"
    digest = write_document(report, envelope(
        "system-report", body, environment={"inventory": inventory} if inventory else None))
    _print_summary(body)
    console.print(f"\n{report} ({digest}). Sign it with `opaas evidence sign`.")
    failed = [s for s in body["steps"] if s["status"] == "failed"]
    if failed:
        raise typer.Exit(code=1)


def _supplied(entry: str, manifest) -> tuple[str, Path, str]:
    component, sep, path = entry.partition("=")
    if not sep or not path:
        raise ValueError(f"--member takes component=path, got {entry!r}.")
    if component != SYSTEM_ROW and manifest.component(component) is None:
        raise ValueError(f"--member: '{component}' is not a component of {manifest.id}.")
    document = Path(path)
    return component, document, document_kind(read_json(document))


def _invoke(args: list[str]) -> int:
    """Runs one OPAAS command in this process, as the module's own CLI would."""
    import click

    from opaas.cli.main import app as main_app

    console.print(f"[dim]$ opaas {' '.join(args)}[/dim]")
    try:
        main_app(args, standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        err_console.print(f"[red]{_plain(exc.format_message())}[/red]")
        return exc.exit_code
    return 0


def _print_summary(body: dict) -> None:
    for step in body["steps"]:
        if step["status"] in ("skipped", "failed", "pending"):
            colour = "red" if step["status"] == "failed" else "yellow"
            console.print(f"  [{colour}]{step['status']}[/{colour}] {step['step']} · "
                          f"{_plain(step['component'])}: {_plain(step.get('reason', ''))}")
    dimensions = sorted({d for row in body["coverage"].values() for d in row},
                        key=lambda d: list(_ORDER).index(d) if d in _ORDER else 99)
    table = Table(title=f"{_plain(body['system']['id'])} · coverage "
                        "(● measured  ○ not measured  · not applicable)")
    table.add_column("component")
    for dimension in dimensions:
        table.add_column(_SHORT.get(dimension, dimension), justify="center")
    for row, cells in body["coverage"].items():
        table.add_row(_plain(row), *[_STATUS_MARK.get(cells.get(d, ""), "") for d in dimensions])
    console.print(table)
    for finding in body["composition"]["findings"]:
        console.print(f"  [yellow]composition:[/yellow] {_plain(finding)}")
    environments = body["environments"]
    if not environments["consistent"]:
        console.print("  [yellow]The members were not all measured in one environment "
                      "(N5).[/yellow]")
    verdicts = [r for r in body["measurements"] if "verdict" in r]
    for record in verdicts:
        word = record["verdict"]["verdict"]
        colour = {"comply": "green", "violate": "red"}.get(word, "yellow")
        console.print(f"  threshold · {_plain(record['component'])} · {_plain(record['aspect'])}: "
                      f"[{colour}]{word}[/{colour}] ({record['threshold']['scope']})")


_ORDER = ("performance", "capability", "consistency", "safety", "compliance",
          "actionRecoverability", "lifecycleCoverage", "policyCheckability",
          "responsibilityAttribution", "evidenceIntegrity", "degradation")
_SHORT = {"performance": "perf", "capability": "capab", "consistency": "consist",
          "safety": "safety", "compliance": "compl", "actionRecoverability": "Q1",
          "lifecycleCoverage": "Q2", "policyCheckability": "Q3",
          "responsibilityAttribution": "Q4", "evidenceIntegrity": "Q5", "degradation": "Q6"}


@app.command("draft")
def draft_command(
    out: Annotated[Path, typer.Option("--out", "-o", help="Manifest to write.")]
    = Path("system.json"),
    inventory: Annotated[Path | None, typer.Option(
        "--inventory", "-i", help="A `discover scan` document; without it, the host is scanned "
        "now.")] = None,
    endpoint: Annotated[list[str], typer.Option(
        "--endpoint", help="A remote engine to include, by URL. Repeatable.")] = None,
    config: Annotated[list[Path], typer.Option(
        "--config", help="MCP configuration file the agents use. Repeatable.")] = None,
    manifest: Annotated[list[Path], typer.Option(
        "--manifest", help="Tool manifest (MCP tools/list or OpenAI functions). Repeatable.")]
    = None,
    system_id: Annotated[str, typer.Option("--id", help="The system's id.")] = "system",
    purpose: Annotated[str | None, typer.Option(
        "--purpose", help="The intended use. Left as a marker when omitted: the system is not "
        "certifiable until a person writes it.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Drafts a system manifest from what the host serves: every model, MCP server and tool."""
    from opaas.discover import discover
    from opaas.system.draft import draft_manifest

    if out.exists() and not force:
        err_console.print(f"[red]{out} exists: a draft never overwrites a manifest someone "
                          "completed. Pass --force, or choose another --out.[/red]")
        raise typer.Exit(code=2)
    try:
        observed = (read_json(inventory)["inventory"] if inventory
                    else discover(endpoints=endpoint or [], configs=config or [],
                                  manifests=manifest or [], containers=False))
    except (OSError, ValueError, KeyError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    document, notes = draft_manifest(observed, system_id=system_id, purpose=purpose)
    for note in notes:
        console.print(f"  [yellow]note:[/yellow] {_plain(note)}")
    try:
        load_manifest(document, known_adapters=tuple(available_agents()))
    except SystemManifestError as exc:
        err_console.print(f"[red]The draft is not a valid manifest: {_plain(exc)}[/red]")
        raise typer.Exit(code=1) from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for component in document["components"]:
        console.print(f"  {component['kind']:<11} {_plain(component['id'])}")
    console.print(f"\n{out}: complete the purpose, then add agents, flows and records before "
                  "`opaas audit run`.")


@app.command("template")
def template_command(
    system: Annotated[Path, typer.Argument(help="System manifest (JSON).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Directory for the templates.")]
    = Path("scenarios"),
    manifest: Annotated[list[Path], typer.Option(
        "--manifest", help="Tool manifest (MCP tools/list or OpenAI functions) to take each "
        "tool's description and parameters from. Repeatable.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    """Writes scenario templates for the manifest's tools: the operator writes the answers."""
    from opaas.system.template import scenario_templates

    try:
        loaded = load_manifest(read_json(system), known_adapters=tuple(available_agents()))
        schemas = _tool_schemas(manifest or [])
    except (SystemManifestError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    templates, notes = scenario_templates(loaded, tool_schemas=schemas)
    existing = [name for name, _ in templates if (out / name).exists()]
    if existing and not force:
        err_console.print(f"[red]{len(existing)} template(s) already exist in {out}, perhaps "
                          f"completed: {', '.join(existing)}. Pass --force to overwrite.[/red]")
        raise typer.Exit(code=2)
    out.mkdir(parents=True, exist_ok=True)
    for name, scenario in templates:
        (out / name).write_text(json.dumps(scenario, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
        console.print(f"  {scenario['category']:<12} {out / name}")
    for note in notes:
        console.print(f"  [yellow]note:[/yellow] {_plain(note)}")
    console.print("\nComplete them, then name the directory in the manifest's \"scenarios\".")


def _tool_schemas(paths: list[Path]) -> dict[str, dict]:
    """Each tool's description and parameters, from MCP tools/list or OpenAI function lists."""
    schemas: dict[str, dict] = {}
    for path in paths:
        raw = read_json(path)
        items = raw.get("tools") if isinstance(raw, dict) else raw
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            definition = item.get("function") if isinstance(item.get("function"), dict) else item
            if definition.get("name"):
                schema = definition.get("inputSchema") or definition.get("parameters")
                schemas[str(definition["name"])] = {
                    "description": definition.get("description"),
                    **({"parameters": schema} if isinstance(schema, dict) else {}),
                }
    return schemas


@app.command("validate")
def validate_command(
    system: Annotated[Path, typer.Argument(help="System manifest (JSON).")],
) -> None:
    """Checks a system manifest and lists its components."""
    try:
        manifest = load_manifest(read_json(system), known_adapters=tuple(available_agents()))
    except (SystemManifestError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    console.print(f"[bold]{_plain(manifest.id)}[/bold] — {_plain(manifest.purpose)}")
    for component in manifest.components:
        console.print(f"  {component.kind:<15} {_plain(component.id)}")
    for flow in manifest.flows:
        via = f" via {flow['via']}" if "via" in flow else ""
        console.print(f"  [dim]flow[/dim] {_plain(flow['from'])} → {_plain(flow['to'])}{via}")
    for entry in manifest.pending():
        console.print(
            f"  [yellow]{_plain(entry['component'])}: the '{entry['adapter']}' adapter is not "
            "implemented yet; its cells will read not measured until it is.[/yellow]"
        )


@app.command("extract")
def extract_command(
    document: Annotated[Path, typer.Argument(help="An OPAAS report or evidence document.")],
    component: Annotated[str, typer.Option(
        "--component", "-c", help="The component of the system the document describes.")],
    out: Annotated[Path | None, typer.Option(
        "--out", "-o", help="Write the measurements as canonical JSON.")] = None,
) -> None:
    """Reads one document into measurements: measured, not measured, not applicable."""
    try:
        extraction = extract(read_json(document), component=component, file=document.name,
                             file_hash=file_digest(document))
    except (ExtractionError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    table = Table(title=f"{_plain(component)} · {extraction.kind} · {_plain(document.name)}")
    for column in ("axis", "dimension", "aspect", "status", "value or reason"):
        table.add_column(column)
    for m in extraction.measurements:
        style = _STATUS_STYLE[m.status]
        table.add_row(m.axis, m.dimension, _plain(m.aspect),
                      f"[{style}]{m.status}[/{style}]",
                      _plain(_show(m.value) if m.value else m.reason or ""))
    console.print(table)
    for note in extraction.notes:
        console.print(f"  [dim]{_plain(note)}[/dim]")
    if extraction.declarations:
        console.print(f"  {len(extraction.declarations)} operator declaration(s) kept apart "
                      "from the measurements (invariant 9).")
    if out is not None:
        digest = write_document(out, extraction.as_dict())
        console.print(f"\n{out} ({digest})")


def _show(value: dict) -> str:
    kind = value["type"]
    if kind == "rate":
        return f"{value['point']:.1%} [{value['low']:.1%}, {value['high']:.1%}] n={value['n']}"
    if kind == "number":
        return f"{value['value']} {value['unit']}"
    if kind == "distribution":
        centre = value.get("p50", value.get("point"))
        return f"p50 {centre} {value['unit']}" + (
            f" · p95 {value['p95']}" if "p95" in value else "")
    return str(value["verdict"])


__all__ = ["app"]
