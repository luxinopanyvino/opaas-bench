"""`opaas audit`: the system audit (D60). Phase S0: the manifest and the measurements.

`validate` checks a system manifest; `extract` reads any OPAAS document into
the common measurement format. Neither writes over a document: `extract`
writes its own file, and the document it read keeps its bytes and its hash.
The orchestrated `opaas audit --system` arrives with phase S1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from opaas.cli.common import _plain, console, err_console, read_json
from opaas.core.evidence import write_document
from opaas.core.hashing import file_digest
from opaas.smith.agents import available_agents
from opaas.system import (
    ExtractionError,
    SystemManifestError,
    extract,
    load_manifest,
)

app = typer.Typer(help="System audit: manifest and measurements (D60).", no_args_is_help=True)

_STATUS_STYLE = {"measured": "green", "not-measured": "yellow", "not-applicable": "dim"}


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
