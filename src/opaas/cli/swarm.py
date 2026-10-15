"""`opaas swarm`: multi-model and multi-agent flows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from opaas.cli.common import _plain, console, err_console, read_json
from opaas.core.credential import load_public_key
from opaas.core.evidence import EvidenceError, envelope, read_document, write_document
from opaas.inquest import FORMATS, import_records
from opaas.inquest.importers import ImportError_
from opaas.swarm import TopologyError, analyse, attribute, load_topology

app = typer.Typer(help="Topology declared against observed, attribution, fault outcomes.",
                  no_args_is_help=True)

Source = Annotated[Path, typer.Argument(help="Records: a sentinel log, OTel spans or JSONL.")]
Fmt = Annotated[str, typer.Option("--format", "-f", help=f"One of {', '.join(FORMATS)}.")]


@app.command("analyze")
def analyze_command(
    source: Source,
    topology: Annotated[Path, typer.Option("--topology", "-t",
                                           help="Declared flow: {edges: [{from, to}]}.")],
    fmt: Fmt = "sentinel",
    key: Annotated[Path | None, typer.Option("--key", "-k")] = None,
    inventory: Annotated[Path | None, typer.Option(
        "--inventory", help="`discover scan` document to bind the result to (N5).")] = None,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("swarm.json"),
) -> None:
    """Edges declared vs observed, per-edge figures, delegation attribution, faults."""
    try:
        declared = load_topology(read_json(topology))
        imported = import_records(source, fmt, public_key=load_public_key(key) if key else None)
        environment = ({"inventory": read_document(inventory, kind="inventory")["inventory"]}
                       if inventory else None)
    except (TopologyError, ImportError_, EvidenceError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    result = {**analyse(imported.records, declared), "integrity": imported.integrity}
    digest = write_document(out, envelope("swarm-analysis", result, environment=environment))
    topo = result["topology"]
    for edge in topo["undeclared"]:
        console.print(f"  [red]undeclared[/red] {edge['from']} → {edge['to']} "
                      f"({edge['calls']} calls)")
    for edge in topo["declaredButUnused"]:
        console.print(f"  [yellow]unused[/yellow]     {edge['from']} → {edge['to']}")
    for key_name, count in topo["unattributedCalls"].items():
        console.print(f"  [yellow]unattributed[/yellow] {key_name}: {count}")
    for edge in result["edges"]:
        latency = edge["latencyMs"].get("point")
        console.print(f"  {edge['from']} → {edge['to']}: {edge['calls']} calls, "
                      f"p50 {latency} ms, errors {edge['errorRate']['point']}")
    faults = result["faults"]
    if faults["injected"]:
        console.print(f"  faults: {faults['outcomes']}; recovered but not visible: "
                      f"{len(faults['recoveredButNotVisible'])}")
    console.print(f"\n{out} ({digest})")


@app.command("attribute")
def attribute_command(
    source: Source,
    event: Annotated[str, typer.Option("--event", "-e", help="Id of the outcome record.")],
    fmt: Fmt = "sentinel",
) -> None:
    """The subgraph of actors and edges behind one outcome."""
    try:
        imported = import_records(source, fmt)
        result = attribute(imported.records, event)
    except (TopologyError, ImportError_, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    console.print(json.dumps(result, indent=2, ensure_ascii=False))
