"""`opaas sentinel`: the observing proxy, its gate, and the inventory watch.

`opaas watch` (one certificate, one deployment) keeps its top-level name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from opaas.cli.common import (
    _load_signing_key,
    _plain,
    console,
    err_console,
    read_json,
    write_json,
)
from opaas.core.events import EventLog
from opaas.core.evidence import EvidenceError, envelope, read_document, write_document
from opaas.core.policy import PolicyError, load_policies
from opaas.discover import discover
from opaas.sentinel.drift import EXIT_CODES
from opaas.sentinel.gate import ApprovalQueue, GateError
from opaas.sentinel.inventory import check_environment
from opaas.sentinel.overhead import benchmark_overhead
from opaas.sentinel.proxy import (
    DEFAULT_KEEP,
    DEFAULT_KEEP_OUTPUT,
    MODES,
    SentinelProxy,
    serve,
)

app = typer.Typer(help="Observe and gate live traffic; watch the environment.",
                  no_args_is_help=True)

Policies = Annotated[Path | None, typer.Option(
    "--policies", "-p", help="Structural policy set (JSON).")]
Queue = Annotated[Path, typer.Option("--queue", help="Approval queue directory.")]


@app.command("observe")
def observe_command(
    upstream: Annotated[str, typer.Option("--upstream", "-u",
                                          help="Engine or tool server to front.")],
    log: Annotated[Path, typer.Option("--log", help="Hash-chained event log to append to.")],
    listen: Annotated[str, typer.Option("--listen", help="host:port to listen on.")]
    = "127.0.0.1:8899",
    policies: Policies = None,
    mode: Annotated[str, typer.Option("--mode", help="observe (default) or gate (opt-in).")]
    = "observe",
    queue: Queue = Path("approvals"),
    approval_timeout: Annotated[float, typer.Option(
        "--approval-timeout", help="Seconds a held action waits before it is refused.")]
    = 300.0,
    key: Annotated[Path | None, typer.Option(
        "--key", "-k", help="Operational Ed25519 key that seals the log. Never the root key.")]
    = None,
    passphrase: Annotated[str | None, typer.Option("--passphrase",
                                                   envvar="OPAAS_KEY_PASSPHRASE")] = None,
    seal_every: Annotated[int, typer.Option("--seal-every", min=1)] = 100,
    keep: Annotated[list[str], typer.Option(
        "--keep", help="Extra request field to keep in clear (dotted, * for lists). "
        "Repeatable. Everything else is a digest.")] = None,
    keep_output: Annotated[list[str], typer.Option(
        "--keep-output", help="Extra response field to keep in clear, e.g. "
        "choices.*.message.content. A stream is assembled first, so the whole answer is "
        "kept. Repeatable.")] = None,
    actor_chain: Annotated[str | None, typer.Option(
        "--actor-chain", help="Chain to record when a caller sends none, e.g. "
        "service:batch>agent:nightly. Recorded as configured, not as received.")] = None,
    faults: Annotated[Path | None, typer.Option(
        "--faults", help="Scripted fault plan (swarm). For test deployments only.")] = None,
    overhead_out: Annotated[Path | None, typer.Option(
        "--overhead-out", help="Where to write the measured overhead on shutdown.")] = None,
) -> None:
    """Runs the proxy. Observe changes nothing in the traffic; gate is opt-in."""
    if mode not in MODES:
        err_console.print(f"[red]--mode is one of {', '.join(MODES)}.[/red]")
        raise typer.Exit(code=2)
    try:
        loaded = load_policies(read_json(policies)) if policies else []
        signing = _load_signing_key(key, passphrase) if key else None
        plan = None
        if faults:
            from opaas.swarm import FaultPlan

            plan = FaultPlan(read_json(faults))
    except (PolicyError, OSError, ValueError, TypeError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    host, _, port = listen.rpartition(":")
    if not port.isdigit():
        err_console.print("[red]--listen is host:port.[/red]")
        raise typer.Exit(code=2)

    def alert(decision: dict) -> None:
        err_console.print(f"[yellow]policy {decision['policy']}: {decision['verdict']}[/yellow] "
                          f"event {decision['event']} target {decision.get('target')}")

    try:
        proxy = SentinelProxy(
            upstream, EventLog(log, signing_key=signing, seal_every=seal_every),
            policies=loaded, mode=mode, keep=(*DEFAULT_KEEP, *(keep or [])),
            keep_output=(*DEFAULT_KEEP_OUTPUT, *(keep_output or [])),
            actor_chain=actor_chain, queue=ApprovalQueue(queue) if mode == "gate" else None,
            approval_timeout_s=approval_timeout, faults=plan, alert=alert,
        )
    except ValueError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    console.print(f"[bold]{mode}[/bold] {upstream} · log {log}"
                  + (" · sealed" if signing else " · [yellow]unsealed[/yellow]")
                  + f" · {len(loaded)} policies")
    if plan:
        console.print("[yellow]Fault injection is on: this proxy disturbs traffic.[/yellow]")
    try:
        serve(proxy, host or "127.0.0.1", int(port),
              ready=lambda url: console.print(f"listening on {url}"))
    except KeyboardInterrupt:
        pass
    finally:
        summary = proxy.overhead_summary()
        if overhead_out is not None:
            write_document(overhead_out, envelope(
                "sentinel-overhead",
                {"method": "measured in service", "overhead": summary, "mode": mode},
                environment=None))
        console.print(f"overhead p50 {summary['p50']} ms · p95 {summary['p95']} ms · "
                      f"p99 {summary['p99']} ms over {summary['count']} request(s)")


@app.command("approvals")
def approvals_command(queue: Queue = Path("approvals")) -> None:
    """Lists the actions held for approval."""
    pending = ApprovalQueue(queue).pending()
    if not pending:
        console.print("Nothing is waiting for approval.")
    for entry in pending:
        console.print(f"  {entry['id']}  {entry.get('target')}  "
                      f"{json.dumps(entry.get('actorChain'))}  "
                      f"{[p['policy'] for p in entry.get('policies', [])]}")


def _decide(queue: Path, pending_id: str, decision: str, by: str, note: str) -> None:
    try:
        made = ApprovalQueue(queue).decide(pending_id, decision, by=by, note=note)
    except GateError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    console.print(f"{made.decision} by {made.by} at {made.at}")


By = Annotated[str, typer.Option("--by", help="Who decides. Recorded in the log.")]
Note = Annotated[str, typer.Option("--note")]


@app.command("approve")
def approve_command(pending_id: str, by: By, queue: Queue = Path("approvals"),
                    note: Note = "") -> None:
    """Approves a held action; the proxy forwards it and records the approval."""
    _decide(queue, pending_id, "approve", by, note)


@app.command("deny")
def deny_command(pending_id: str, by: By, queue: Queue = Path("approvals"),
                 note: Note = "") -> None:
    """Denies a held action; the proxy refuses it."""
    _decide(queue, pending_id, "deny", by, note)


@app.command("inventory")
def inventory_command(
    baseline: Annotated[Path, typer.Option("--baseline", "-b",
                                           help="Signed `discover scan` document.")],
    report: Annotated[list[Path], typer.Option(
        "--report", help="Certified report; its model counts as certified. Repeatable.")]
    = None,
    endpoint: Annotated[list[str], typer.Option("--endpoint")] = None,
    config: Annotated[list[Path], typer.Option("--config")] = None,
    manifest: Annotated[list[Path], typer.Option("--manifest")] = None,
    skills: Annotated[list[Path], typer.Option("--skills")] = None,
    containers: Annotated[bool, typer.Option("--containers/--no-containers")] = True,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("inventory-watch.json"),
) -> None:
    """Re-discovers the environment and compares it with the baseline (exit 0/1/3)."""
    try:
        base = read_document(baseline, kind="inventory")["inventory"]
        certified = {str((read_json(p).get("model") or {}).get("name")) for p in report or []}
    except (EvidenceError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    current = discover(endpoints=endpoint or [], configs=config or [],
                       manifests=manifest or [], skill_dirs=skills or [],
                       containers=containers)
    result = check_environment(base, current, certified_models=certified or None)
    write_json(out, result)
    color = {"current": "green", "drifted": "red", "inconclusive": "yellow"}[result["state"]]
    console.print(f"[bold {color}]{result['state'].upper()}[/bold {color}] {result['detail']}")
    for alert in result["alerts"]:
        console.print(f"  [red]{alert['alert']}[/red] {_plain(alert['detail'])}")
    raise typer.Exit(code=EXIT_CODES[result["state"]])


@app.command("overhead")
def overhead_command(
    requests: Annotated[int, typer.Option("--requests", "-n", min=10)] = 500,
    policies: Policies = None,
    inventory: Annotated[Path | None, typer.Option(
        "--inventory", help="`discover scan` document to bind the result to (N5): the "
        "overhead is this host's.")] = None,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("sentinel-overhead.json"),
) -> None:
    """Benchmarks the proxy's own added latency (p50/p95/p99)."""
    try:
        loaded = load_policies(read_json(policies)) if policies else []
        environment = ({"inventory": read_document(inventory, kind="inventory")["inventory"]}
                       if inventory else None)
    except (PolicyError, EvidenceError, OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    result = benchmark_overhead(requests=requests, policies=loaded)
    digest = write_document(out, envelope("sentinel-overhead", result,
                                          environment=environment))
    o = result["overhead"]
    console.print(f"overhead p50 {o['p50']} ms · p95 {o['p95']} ms · p99 {o['p99']} ms "
                  f"({requests} requests, {len(loaded)} policies)")
    console.print(f"[dim]{result['excludes']} is not included.[/dim]  {out} ({digest})")
