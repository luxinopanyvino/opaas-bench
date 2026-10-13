"""`opaas discover`: inventory and auditability gap scan."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opaas.cli.common import _plain, console, err_console, read_json
from opaas.core.credential import load_public_key
from opaas.core.evidence import EvidenceError, envelope, read_document, write_document
from opaas.discover import discover
from opaas.discover.gaps import scan_gaps

app = typer.Typer(help="What runs here, and what can and cannot be audited about it.",
                  no_args_is_help=True)

Endpoint = Annotated[list[str], typer.Option(
    "--endpoint", help="A remote engine to include, by URL. Repeatable.")]
Config = Annotated[list[Path], typer.Option(
    "--config", help="MCP configuration file the agent uses (mcpServers). Repeatable.")]
Manifest = Annotated[list[Path], typer.Option(
    "--manifest", help="Tool manifest: an MCP tools/list result or OpenAI functions. "
    "Repeatable.")]
Skills = Annotated[list[Path], typer.Option(
    "--skills", help="Directory of skills (SKILL.md or skill.json). Repeatable.")]
Containers = Annotated[bool, typer.Option(
    "--containers/--no-containers", help="List running containers via docker or podman.")]
Out = Annotated[Path, typer.Option("--out", "-o", help="Evidence document to write.")]


@app.command("scan")
def scan_command(
    endpoint: Endpoint = None,
    config: Config = None,
    manifest: Manifest = None,
    skills: Skills = None,
    containers: Containers = True,
    out: Out = Path("inventory.json"),
) -> None:
    """Inventory of engines, models, MCP servers, tools, skills and containers.

    Read-only: GETs to well-known engine endpoints, process names (never command
    lines) and the files named here. Sign the result with `opaas evidence sign`.
    """
    for path in [*(config or []), *(manifest or []), *(skills or [])]:
        if not path.exists():
            err_console.print(f"[red]Not found:[/red] {path}")
            raise typer.Exit(code=2)
    document = discover(endpoints=endpoint or [], configs=config or [],
                        manifests=manifest or [], skill_dirs=skills or [],
                        containers=containers)
    digest = write_document(out, envelope("inventory", {"inventory": document},
                                          environment={"inventory": document}))
    console.print(f"[bold]Environment[/bold] {document['hash']}")
    for engine in document.get("engines", []):
        console.print(
            f"  {engine['kind']:<18} :{engine['port']:<6} {engine.get('version') or '?':<10} "
            f"{engine['bind']:<15} {len(engine.get('models', []))} model(s)"
        )
    stack = document["agentStack"]
    servers = sum(len(c.get("servers", [])) for c in stack["mcpConfigs"])
    tools = sum(len(m.get("tools", [])) for m in stack["toolManifests"])
    skills_found = sum(len(r.get("skills", [])) for r in stack["skills"])
    console.print(f"  MCP servers {servers} · tools {tools} · skills {skills_found} · "
                  f"agent runtimes {len(stack['agentRuntimes'])}")
    for note in stack["notObserved"]:
        console.print(f"  [dim]not observed: {_plain(note)}[/dim]")
    console.print(f"\n{out} ({digest})")


@app.command("gaps")
def gaps_command(
    inventory: Annotated[Path, typer.Option("--inventory", "-i",
                                            help="Document from `discover scan`.")],
    log: Annotated[list[Path], typer.Option("--log", help="Agent log to assess. Repeatable.")]
    = None,
    key: Annotated[Path | None, typer.Option(
        "--key", "-k", help="Public key that sealed the logs, to verify their seals.")] = None,
    declaration: Annotated[Path | None, typer.Option(
        "--declaration", help="Operator declaration (JSON): toolLogging, approvals, "
        "appendOnlyLogs. Reported as declared, never as observed.")] = None,
    logging_endpoint: Annotated[list[str], typer.Option(
        "--logging-endpoint", help="Scheme://host:port of a logging layer, such as the "
        "sentinel proxy. Repeatable.")] = None,
    out: Out = Path("gaps.json"),
) -> None:
    """Deterministic auditability gap scan (OP1). A gap is never a predicted violation."""
    try:
        document = read_document(inventory, kind="inventory")
        declared = read_json(declaration) if declaration else None
        public_key = load_public_key(key) if key else None
    except (EvidenceError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    for path in log or []:
        if not path.exists():
            err_console.print(f"[red]Log not found:[/red] {path}")
            raise typer.Exit(code=2)
    result = scan_gaps(document["inventory"], logs=log or [], public_key=public_key,
                       declaration=declared, logging_endpoints=logging_endpoint or [])
    digest = write_document(out, envelope("gap-scan", result,
                                          environment={"inventory": document["inventory"]}))
    style = {"gap": "red", "ok": "green", "unsupported": "yellow", "not-observed": "dim"}
    for finding in result["findings"]:
        color = style[finding["status"]]
        console.print(
            f"  [{color}]{finding['status']:<13}[/{color}] {finding['rule']} "
            f"{finding['question']} {_plain(finding['subject'])}: {_plain(finding['detail'])}"
        )
    console.print(f"\n{result['counts']}  ·  {out} ({digest})")
    console.print(f"[dim]{result['note']}[/dim]")
