"""`opaas inquest`: post-hoc audit of existing records."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from opaas.cli.common import _plain, console, err_console, read_json
from opaas.core.credential import load_public_key
from opaas.core.events import PHASES
from opaas.core.evidence import envelope, write_document
from opaas.core.policy import PolicyError, load_policies
from opaas.inquest import FORMATS, audit, import_records, parse_degradation
from opaas.inquest.audit import AuditError
from opaas.inquest.importers import ImportError_

app = typer.Typer(help="Audit the records that exist: integrity first, three-valued verdicts.",
                  no_args_is_help=True)


@app.command("audit")
def audit_command(
    source: Annotated[Path, typer.Argument(help="Records to audit.")],
    fmt: Annotated[str, typer.Option("--format", "-f",
                                     help=f"One of {', '.join(FORMATS)}.")] = "sentinel",
    policies: Annotated[Path | None, typer.Option("--policies", "-p")] = None,
    key: Annotated[Path | None, typer.Option(
        "--key", "-k", help="Public key that sealed the log.")] = None,
    phases: Annotated[str | None, typer.Option(
        "--phases", help="Phases the system has, comma-separated (operator declaration).")]
    = None,
    append_only: Annotated[bool, typer.Option(
        "--declared-append-only", help="The operator declares the storage append-only.")]
    = False,
    thresholds: Annotated[Path | None, typer.Option(
        "--thresholds", help="Operator thresholds: {metric: {min|max: x}}. Compared only.")]
    = None,
    degrade: Annotated[list[str], typer.Option(
        "--degrade", help="drop-field:<f>, drop-every:<k> or digest-only. Repeatable (Q6).")]
    = None,
    inventory: Annotated[Path | None, typer.Option("--inventory")] = None,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("inquest.json"),
) -> None:
    """Audits records: IS level found, five dimensions, comply/violate/undecidable."""
    try:
        loaded = load_policies(read_json(policies)) if policies else []
        public_key = load_public_key(key) if key else None
        declared_phases = [p.strip() for p in phases.split(",")] if phases else None
        if declared_phases and (bad := set(declared_phases) - set(PHASES)):
            raise AuditError(f"Unknown phases {sorted(bad)}. Known: {', '.join(PHASES)}.")
        steps = [parse_degradation(s) for s in degrade or []]
        imported = import_records(source, fmt, public_key=public_key,
                                  declared_append_only=append_only)
        limits = read_json(thresholds) if thresholds else None
        environment: dict[str, Any] | None = (
            {"inventory": read_json(inventory).get("inventory")} if inventory else None)
    except (PolicyError, AuditError, ImportError_, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    result = audit(imported, policies=loaded, expected_phases=declared_phases,
                   thresholds=limits, degradations=steps)
    digest = write_document(out, envelope("inquest-audit", result, environment=environment))

    dims = result["dimensions"]
    console.print(f"[bold]Integrity[/bold] {dims['evidenceIntegrity']['IS']} "
                  f"({dims['evidenceIntegrity']['ISBasis']})")
    for problem in dims["evidenceIntegrity"]["problems"]:
        console.print(f"  [yellow]{_plain(problem)}[/yellow]")
    style = {"comply": "green", "violate": "red", "undecidable": "yellow"}
    for verdict in result["verdicts"]:
        color = style[verdict["verdict"]]
        missing = f" — missing {', '.join(verdict['missing'])}" if verdict.get("missing") else ""
        console.print(f"  [{color}]{verdict['verdict']:<11}[/{color}] {verdict['policy']}"
                      f"{_plain(missing)}")
    spdr = dims["policyCheckability"]["SPDR"]
    if spdr.get("missingFieldsBlocking"):
        console.print(f"\nStart recording: {', '.join(spdr['missingFieldsBlocking'])}")
    for line in result["limitations"]:
        console.print(f"[dim]limitation: {_plain(line)}[/dim]")
    console.print(f"\n{out} ({digest})")
