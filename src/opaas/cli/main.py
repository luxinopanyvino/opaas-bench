"""`opaas` entry point."""

from __future__ import annotations

import getpass
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column, Table

from opaas.adapters import available_backends, get_backend
from opaas.adapters.base import DEFAULT_PROMPT_MODE, PROMPT_MODES, BackendError
from opaas.bench.classifiers import (
    DEFAULT_CLASSIFIERS,
    ClassifierError,
    classifiers_available,
    ensure_default_classifier,
    is_default_classifier_cached,
)
from opaas.bench.compare import ComparisonError, compare, load_reports
from opaas.bench.datasets import (
    DEFAULT_DATASET,
    CacheError,
    DatasetError,
    available_sources,
    builtin_datasets,
    cache_dir,
    describe_cache,
    describe_cached_datasets,
    ensure_dataset,
    is_cached,
    load_dataset,
)
from opaas.bench.datasets.importers import ImporterError, import_dataset
from opaas.bench.declaration import template as declaration_template
from opaas.bench.registry import REJECTED, admit
from opaas.bench.runner import RunEvent, RunRequest, execute
from opaas.bench.scoring import ScoringError, prompt_template_hash
from opaas.bench.suites import PLANNED_SUITES, available_suites, get_suite, planned_suite_hint
from opaas.cli.banner import print_banner
from opaas.cli.common import (  # noqa: E402 - shared with the module command groups
    _load_signing_key,
    _plain,
    _signing_identity,
    _verification_trust,
    console,
    err_console,
    read_json,
)
from opaas.cli.menu import MenuItem, is_interactive, present
from opaas.core.credential import (
    CredentialError,
    TrustAnchorError,
    build_credential,
    build_system_credential,
    fingerprint,
    generate_root_keypair,
    identity_for_signing,
    is_revocation_list,
    load_public_key,
    public_identifier,
    resolve_trust,
    sign_credential,
    verify_credential,
)
from opaas.core.credential.keys import (
    public_key_from_did_document,
)
from opaas.core.credential.proof import DELEGATION, check_proof
from opaas.core.credential.trust import REVOCATIONS_DIRNAME, user_anchor_dir
from opaas.core.credential.vc import DEFAULT_VALIDITY_DAYS
from opaas.core.fingerprint import deployment_fingerprint
from opaas.core.hashing import (
    canonical_json,
    file_digest,
    short_hash,
    slugify,
    tokenizer_files_hash,
    weights_hash,
)
from opaas.core.report import REPORT_FILENAME, describe_environment, describe_scored_dataset
from opaas.core.transparency import (
    TransparencyError,
    append_entry,
    inclusion_check,
    load_log,
    verify_log,
)
from opaas.discover import inventory as discovery
from opaas.sentinel.drift import (
    EXIT_CODES,
    SentinelError,
    append_watch_log,
    result_envelope,
)
from opaas.sentinel.drift import watch as run_watch
from opaas.system.watch import system_result
from opaas.web import SiteError, build_site, check_verify_url, load_entry, render_printable

app = typer.Typer(
    name="opaas",
    help="OPAAS — evaluation and digital credential for on-prem LLM models (onpremIALabs).",
    add_completion=False,
)


def _menu_items() -> list[MenuItem]:
    """Read-only ones are executed; the rest show their command."""
    return [
        MenuItem(
            "▶",
            "Evaluate a model",
            command="opaas run --model <id> --backend ollama --suite all",
        ),
        MenuItem("◈", "View evaluation blocks", action=suites_command),
        MenuItem("▤", "View datasets and cache", action=datasets_command),
        MenuItem("⚙", "View inference engines", action=backends_command),
        MenuItem(
            "✎",
            "Sign a report",
            command="opaas sign --input <report> --key <key.pem> --out cert.json",
        ),
        MenuItem(
            "◱",
            "Publish certificates",
            command="opaas publish --report <report> --credential <cert> "
            "--did-document did.json",
        ),
        MenuItem(
            "▣",
            "Print a certificate",
            command="opaas certificate --report <report> --credential <cert> "
            "--url <published page>",
        ),
        MenuItem(
            "✓",
            "Verify a credential",
            command="opaas verify --credential <cert> --report <report>",
        ),
        MenuItem("✕", "Exit", quit=True),
    ]


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Cover page and menu. Only with no subcommand and an interactive terminal:
    command output gets piped to files and to `jq`, and a menu there would get
    in the way."""
    if ctx.invoked_subcommand is not None:
        return
    print_banner(console)
    if is_interactive():
        present(console, _menu_items())
    else:
        console.print(ctx.get_help())


def _parse_int_list(raw: str, flag: str) -> list[int]:
    try:
        values = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise typer.BadParameter(
            f"{flag} expects comma-separated integers, received: {raw!r}"
        ) from None
    if not values or any(v <= 0 for v in values):
        raise typer.BadParameter(f"{flag} must contain positive integers, received: {raw!r}")
    return values


Model = Annotated[
    str, typer.Option("--model", "-m", help="Model id exactly as the backend serves it.")
]
Backend = Annotated[
    str, typer.Option("--backend", "-b", help="Inference engine adapter.")
]
Suites = Annotated[
    list[str] | None, typer.Option("--suite", "-s", help="Suite to run. Repeatable, or 'all'.")
]
BaseUrl = Annotated[
    str | None,
    typer.Option("--base-url", help="Server endpoint; defaults to the adapter's."),
]
PromptMode = Annotated[
    str,
    typer.Option(
        "--prompt-mode",
        help="'chat' applies the model's template (default); 'raw' skips it and measures "
        "text completion, not instruction following.",
    ),
]
NumCtx = Annotated[
    int | None,
    typer.Option(
        "--num-ctx",
        help="Context window in tokens the engine must serve (Ollama num_ctx). Without it the "
        "server's default applies and a long prompt may be trimmed with nothing warning you.",
    ),
]
MaxConcurrency = Annotated[
    int | None,
    typer.Option(
        "--max-concurrency",
        help="Requests the engine is configured to serve at once (vLLM --max-num-seqs, "
        "OLLAMA_NUM_PARALLEL). No engine publishes it, so it is recorded as YOUR declaration "
        "and block A cross-checks it against what it observes.",
    ),
]
SuiteConcurrency = Annotated[
    int,
    typer.Option(
        "--suite-concurrency",
        help="Parallel requests the harness sends while scoring blocks B and D. Does not change "
        "the result, only how long it takes — and only if the engine really serves them.",
    ),
]
LoadConcurrency = Annotated[
    int,
    typer.Option(
        "--load-concurrency",
        help="Simultaneous requests in block C's determinism-under-load check: the reference "
        "request plus different ones. Set it to the number of users you expect at once; 1 "
        "skips the check and declares it unmeasured.",
    ),
]
ApiKey = Annotated[
    str | None,
    typer.Option(
        "--api-key", envvar="OPAAS_API_KEY", help="Bearer token if the server requires one."
    ),
]
Timeout = Annotated[
    float, typer.Option("--timeout", help="Per-request timeout, in seconds.")
]
Concurrency = Annotated[
    str, typer.Option("--concurrency", help="Block A concurrency levels.")
]
PromptWords = Annotated[
    str, typer.Option("--prompt-words", help="Prompt lengths to sweep, in words.")
]
MaxTokens = Annotated[
    int, typer.Option("--max-tokens", help="Tokens to generate per request (block A only).")
]
CapabilityMaxTokens = Annotated[
    int | None,
    typer.Option(
        "--capability-max-tokens",
        help="Token cap per block B instruction-following item. Set, it wins over the cap a "
        "dataset writes per item (IFEval: 700); unset, the item's cap applies, else 400. A cut "
        "answer fails constraints it might have met; the report counts them and records the "
        "cap applied.",
    ),
]
SafetyMaxTokens = Annotated[
    int | None,
    typer.Option(
        "--safety-max-tokens",
        help="Token cap per block D item. Set, it wins over the cap a dataset writes per item "
        "(XSTest: 320); unset, the item's cap applies, else 320. A refusal cut mid-sentence can "
        "be scored as compliance; the report counts them and records the cap applied.",
    ),
]
Requests = Annotated[
    int | None,
    typer.Option("--requests", help="Requests per scenario; defaults to 3x the concurrency."),
]
Warmup = Annotated[
    int, typer.Option("--warmup", help="Warmup generations that get discarded.")
]
IdleSeconds = Annotated[
    float,
    typer.Option("--idle-seconds", help="Idle window measured after warmup."),
]
Temperature = Annotated[
    float, typer.Option("--temperature", help="Sampling temperature.")
]
Seed = Annotated[
    int | None, typer.Option("--seed", help="Fixed seed for reproducibility.")
]
Quantization = Annotated[
    str | None, typer.Option("--quantization", help="Declared quantization, e.g. 'GGUF Q4_K_M'.")
]
Source = Annotated[
    str | None, typer.Option("--source", help="Model provenance in the source repository.")
]
WeightsPath = Annotated[
    Path | None,
    typer.Option("--weights-path", help="Local path to the weights, to hash the artifact."),
]
OutputFile = Annotated[
    Path | None,
    typer.Option(
        "--output", "-o", help="Report file; defaults to runs/<run-id>/report.json."
    ),
]
RunId = Annotated[
    str | None, typer.Option("--run-id", help="Explicit run id; defaults to YYYYMMDD-NNN.")
]
ReferenceType = Annotated[
    str,
    typer.Option(
        "--reference-type",
        help="'on-prem' (your own deployment) or 'cloud-baseline' (quality reference).",
    ),
]
DatasetRef = Annotated[
    str,
    typer.Option(
        "--dataset",
        help="Comma-separated sources, local paths, or 'opaas-smoke' for a quick test.",
    ),
]
ItemLimit = Annotated[
    int | None,
    typer.Option("--limit", help="Items per block B task; defaults to all of them."),
]
Offline = Annotated[
    bool,
    typer.Option(
        "--offline",
        help="Forbids the network: cached datasets only. Fails saying what is missing.",
    ),
]
OperatorNote = Annotated[
    list[str] | None,
    typer.Option(
        "--note",
        help="Context you state about this run, e.g. 'shared workstation, background load "
        "not controlled'. Repeat for several. Published in the report and on the "
        "certificate as declared by the operator, not verified. Do not put secrets here.",
    ),
]
DeclarationFile = Annotated[
    Path | None,
    typer.Option("--declaration", help="Block E compliance declaration (JSON)."),
]
ContextWords = Annotated[
    str,
    typer.Option("--context-words", help="Lengths for the block C context probe."),
]
AnswerStyle = Annotated[
    str,
    typer.Option(
        "--answer-style",
        help="'cot' reasons before answering; 'direct' demands the bare letter. Not comparable.",
    ),
]
FaithfulnessModel = Annotated[
    Path | None,
    typer.Option(
        "--faithfulness-model",
        help="Local weights of the faithfulness classifier. Without it, and only if the "
        "classifiers extra is installed, the pinned default is fetched once into the cache.",
    ),
]
FaithfulnessProfile = Annotated[
    str, typer.Option("--faithfulness-profile", help="Profile: 'nli' or 'hhem'.")
]
ToxicityModel = Annotated[
    Path | None,
    typer.Option(
        "--toxicity-model",
        help="Local weights of the toxicity classifier. Without it, and only if the "
        "classifiers extra is installed, the pinned default is fetched once into the cache.",
    ),
]
ClassifierThreshold = Annotated[
    float | None,
    typer.Option("--classifier-threshold", help="Threshold for both classifiers, in [0, 1]."),
]
AllowRemoteCode = Annotated[
    bool,
    typer.Option(
        "--allow-remote-code",
        help="Allows running code from the model's repository (HHEM requires it).",
    ),
]


#: Without a terminal, progress lines are not printed more often than this: a
#: redirected run would otherwise write one line per request into the log.
_PLAIN_PROGRESS_INTERVAL_S = 15.0


@contextmanager
def _progress_reporter(suites: int) -> Iterator[Callable[[RunEvent], None]]:
    """How the progress of the run is shown.

    With a terminal, a single bar with the percentage and the elapsed time,
    which keeps ticking while a suite generates. Redirected to a file or in CI
    there is no bar to redraw: the same events come out as lines with the
    percentage and the elapsed time in front, throttled so an hour-long run
    does not turn into thousands of lines.
    """
    if not console.is_terminal:
        last_progress = 0.0

        def plain(event: RunEvent) -> None:
            nonlocal last_progress
            now = time.monotonic()
            # Milestones always come out; the ticks inside a block, at intervals.
            if event.kind == "progress":
                if now - last_progress < _PLAIN_PROGRESS_INTERVAL_S:
                    return
                last_progress = now
            console.print(f"[dim]{event}[/dim]")

        yield plain
        return

    progress = Progress(
        # The braille spinner does not survive a legacy Windows console (cp1252):
        # there, the ASCII one. A crash while rendering would abort the run.
        SpinnerColumn(spinner_name="line" if console.legacy_windows else "dots"),
        TextColumn(
            "[dim]{task.description}[/dim]",
            table_column=Column(no_wrap=True, max_width=52),
        ),
        BarColumn(bar_width=24),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    with progress:
        bar = progress.add_task(f"starting · {suites} block(s)", total=100.0)

        def live(event: RunEvent) -> None:
            progress.update(bar, completed=event.percent, description=event.message)
            if event.kind != "progress":
                # The milestones are left above the bar: when the run finishes,
                # what remains on screen is a readable log of what happened.
                progress.console.print(f"[dim]{event}[/dim]")

        yield live


@app.command()
def run(
    model: Model,
    backend: Backend = "vllm",
    suite: Suites = None,
    base_url: BaseUrl = None,
    prompt_mode: PromptMode = DEFAULT_PROMPT_MODE,
    num_ctx: NumCtx = None,
    max_concurrency: MaxConcurrency = None,
    suite_concurrency: SuiteConcurrency = 4,
    load_concurrency: LoadConcurrency = 4,
    api_key: ApiKey = None,
    timeout_s: Timeout = 300.0,
    concurrency: Concurrency = "1,4,16",
    prompt_words: PromptWords = "128,1024",
    max_tokens: MaxTokens = 256,
    capability_max_tokens: CapabilityMaxTokens = None,
    safety_max_tokens: SafetyMaxTokens = None,
    requests: Requests = None,
    warmup: Warmup = 2,
    idle_seconds: IdleSeconds = 3.0,
    temperature: Temperature = 0.0,
    seed: Seed = 1234,
    quantization: Quantization = None,
    source: Source = None,
    weights_path: WeightsPath = None,
    output: OutputFile = None,
    run_id: RunId = None,
    reference_type: ReferenceType = "on-prem",
    dataset: DatasetRef = DEFAULT_DATASET,
    limit: ItemLimit = None,
    offline: Offline = False,
    answer_style: AnswerStyle = "cot",
    declaration: DeclarationFile = None,
    note: OperatorNote = None,
    context_words: ContextWords = "256,1024,4096",
    faithfulness_model: FaithfulnessModel = None,
    faithfulness_profile: FaithfulnessProfile = "nli",
    toxicity_model: ToxicityModel = None,
    classifier_threshold: ClassifierThreshold = None,
    allow_remote_code: AllowRemoteCode = False,
) -> None:
    """Runs an evaluation and writes the structured report."""
    requested = suite or ["performance"]
    explicit_all = "all" in requested
    if explicit_all:
        requested = available_suites()

    unknown = [s for s in requested if s not in available_suites()]
    if unknown:
        for name in unknown:
            hint = planned_suite_hint(name) if name in PLANNED_SUITES else None
            err_console.print(
                f"[red]Suite '{name}' not available.[/red] "
                + (hint or f"Available: {', '.join(available_suites())}")
            )
        raise typer.Exit(code=2)

    if explicit_all and PLANNED_SUITES:
        err_console.print(
            "[yellow]Warning:[/yellow] '--suite all' runs the implemented suites "
            f"({', '.join(available_suites())}), not the five design blocks. "
            f"Not implemented: {', '.join(sorted(PLANNED_SUITES))}. "
            "The report and the credential will declare it a partial evaluation."
        )

    if prompt_mode not in PROMPT_MODES:
        err_console.print(
            f"[red]Unknown --prompt-mode: {prompt_mode!r}.[/red] "
            f"Valid ones: {', '.join(PROMPT_MODES)}"
        )
        raise typer.Exit(code=2)

    if max_concurrency is not None and max_concurrency <= 0:
        err_console.print(
            f"[red]--max-concurrency must be greater than 0, received {max_concurrency}.[/red]"
        )
        raise typer.Exit(code=2)
    for flag, value in (
        ("--capability-max-tokens", capability_max_tokens),
        ("--safety-max-tokens", safety_max_tokens),
    ):
        if value is not None and value <= 0:
            err_console.print(f"[red]{flag} must be greater than 0, received {value}.[/red]")
            raise typer.Exit(code=2)
    if suite_concurrency <= 0:
        err_console.print(
            f"[red]--suite-concurrency must be greater than 0, received {suite_concurrency}.[/red]"
        )
        raise typer.Exit(code=2)
    if load_concurrency <= 0:
        err_console.print(
            f"[red]--load-concurrency must be greater than 0, received {load_concurrency}.[/red]"
        )
        raise typer.Exit(code=2)
    if num_ctx is not None and num_ctx <= 0:
        err_console.print(f"[red]--num-ctx must be greater than 0, received {num_ctx}.[/red]")
        raise typer.Exit(code=2)

    if declaration is not None and not declaration.exists():
        err_console.print(f"[red]The declaration does not exist:[/red] {declaration}")
        raise typer.Exit(code=2)

    if {"capability", "safety"} & set(requested):
        # Resolved before loading the model: downloading the dataset or finding
        # out the reference is wrong must not happen after 40 GB of VRAM have
        # already been taken.
        try:
            if not offline and dataset in available_sources() and not is_cached(dataset):
                console.print(f"[dim]Downloading dataset '{dataset}' (first time only)…[/dim]")
            load_dataset(dataset, limit=limit, offline=offline)
            prompt_template_hash(answer_style)
        except (DatasetError, ScoringError) as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from None

    suite_options = {
        "performance": {
            "concurrency": _parse_int_list(concurrency, "--concurrency"),
            "prompt_words": _parse_int_list(prompt_words, "--prompt-words"),
            "max_tokens": max_tokens,
            "requests_per_setting": requests,
            "warmup_requests": warmup,
            "idle_seconds": idle_seconds,
            "temperature": temperature,
            "seed": seed,
        },
        "capability": {
            "dataset": dataset,
            "limit": limit,
            "offline": offline,
            "concurrency": suite_concurrency,
            "instruction_max_tokens": capability_max_tokens,
            "answer_style": answer_style,
            "temperature": temperature,
            "seed": seed,
        },
        "consistency": {
            "context_probe_words": _parse_int_list(context_words, "--context-words"),
            "determinism_concurrency": load_concurrency,
            "temperature": temperature,
            "seed": seed,
        },
        "safety": {
            "dataset": dataset,
            "limit": limit,
            "offline": offline,
            "concurrency": suite_concurrency,
            "max_tokens": safety_max_tokens,
            "temperature": temperature,
            "seed": seed,
            "faithfulness_model": str(faithfulness_model) if faithfulness_model else None,
            "faithfulness_profile": faithfulness_profile,
            "faithfulness_threshold": classifier_threshold,
            "toxicity_model": str(toxicity_model) if toxicity_model else None,
            "toxicity_threshold": classifier_threshold,
            "allow_remote_code": allow_remote_code,
        },
        "compliance": {
            "declaration": str(declaration) if declaration else None,
        },
    }

    request = RunRequest(
        model=model,
        backend=backend,
        suites=requested,
        backend_config={
            "base_url": base_url,
            "prompt_mode": prompt_mode,
            "num_ctx": num_ctx,
            "max_concurrency": max_concurrency,
            "api_key": api_key,
            "timeout_s": timeout_s,
            "quantization": quantization,
        },
        suite_options=suite_options,
        output=output,
        run_id=run_id,
        model_source=source,
        quantization=quantization,
        weights_path=weights_path,
        reference_type=reference_type,
        operator_notes=note or [],
    )

    try:
        with _progress_reporter(len(requested)) as reporter:
            outcome = execute(request, on_event=reporter)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    except BackendError as exc:
        err_console.print(f"[red]Backend error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    _print_run_summary(outcome.report.payload())
    console.print(f"\nReport: [bold]{outcome.report_path}[/bold]")
    console.print(f"Hash  : [bold]{file_digest(outcome.report_path)}[/bold]")
    if outcome.report.status == "partial":
        raise typer.Exit(code=1)


ReportPath = Annotated[
    Path, typer.Argument(help="Path to report.json or to the run directory.")
]


@app.command("show")
def show(report_path: ReportPath) -> None:
    """Shows the summary of an already-generated report."""
    path = report_path / REPORT_FILENAME if report_path.is_dir() else report_path
    if not path.exists():
        err_console.print(f"[red]Does not exist:[/red] {path}")
        raise typer.Exit(code=2)
    _print_run_summary(json.loads(path.read_text(encoding="utf-8")))
    console.print(f"\nReport hash: [bold]{file_digest(path)}[/bold]")


@app.command("backends")
def backends_command() -> None:
    """Lists the available engine adapters."""
    table = Table(title="Registered adapters")
    table.add_column("backend", style="bold")
    table.add_column("engine")
    table.add_column("default endpoint")
    table.add_column("concurrency")
    for name in available_backends():
        instance = get_backend(name)
        table.add_row(
            name,
            getattr(instance, "engine_label", "—"),
            getattr(instance, "default_base_url", "—"),
            "yes" if instance.supports_concurrency else "no (known limitation)",
        )
    console.print(table)


@app.command("suites")
def suites_command() -> None:
    """Lists the implemented and the planned suites."""
    table = Table(title="Evaluation suites")
    table.add_column("suite", style="bold")
    table.add_column("block")
    table.add_column("status")
    table.add_column("description")
    for name in available_suites():
        instance = get_suite(name)
        table.add_row(name, instance.block, "[green]available[/green]", instance.description)
    for name, plan in sorted(PLANNED_SUITES.items(), key=lambda item: item[1]["block"]):
        table.add_row(
            name,
            plan["block"],
            "[yellow]planned[/yellow]",
            f"{plan['description']} ({plan['phase']})",
        )
    console.print(table)


KeysDir = Annotated[
    Path, typer.Option("--out", "-o", help="Directory where the key pair is stored.")
]
Passphrase = Annotated[
    str | None,
    typer.Option(
        "--passphrase",
        envvar="OPAAS_KEY_PASSPHRASE",
        help="Encrypts the private key. Without this argument it is asked for at the "
        "keyboard, which is the recommended way: it stays out of the history.",
    ),
]
Did = Annotated[
    str | None,
    typer.Option(
        "--did",
        help="Issuer DID, e.g. did:web:yourdomain.com, if you control that domain and will "
        "publish the DID document there. Without it the key is named by itself (did:key), "
        "which claims no organisation.",
    ),
]
Overwrite = Annotated[
    bool, typer.Option("--overwrite", help="Overwrites an existing root key.")
]


TemplateOut = Annotated[
    Path, typer.Option("--out", "-o", help="Declaration file to write.")
]


@app.command("compliance")
def compliance_command(out: TemplateOut = Path("declaration.json")) -> None:
    """Generates a declaration template for block E."""
    if out.exists():
        err_console.print(f"[red]Already exists:[/red] {out}. Delete it or choose another path.")
        raise typer.Exit(code=2)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(declaration_template(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console.print(f"[green]Template written to[/green] {out}")
    console.print(
        "\nFill in each requirement with a status (yes/partial/no/unknown) and its evidence.\n"
        "[yellow]Without evidence, a requirement declared as 'yes' is marked unsupported "
        "in the report.[/yellow]\n"
        "Then: opaas run --suite compliance --declaration "
        f"{out}"
    )


ReportInputs = Annotated[
    list[Path], typer.Option("--report", "-r", help="Report to compare. Repeat for each one.")
]
ComparisonOut = Annotated[
    Path | None, typer.Option("--out", "-o", help="Comparison JSON file to write.")
]


AdmitReport = Annotated[
    Path, typer.Argument(help="Report to check against the registry's admission rules.")
]
AdmitJson = Annotated[
    bool, typer.Option("--json", help="Machine-readable verdict, for a submission pipeline.")
]


@app.command("admit")
def admit_command(report: AdmitReport, as_json: AdmitJson = False) -> None:
    """Checks whether a report can join a public comparison, and says why not.

    The criteria are not a secret: run this on your own report before
    submitting it anywhere. A rejection is not a verdict on the deployment —
    a smoke dataset or a cloud baseline are valid runs that simply cannot be
    ranked against somebody else's.
    """
    try:
        payload = json.loads(report.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        err_console.print(f"[red]The report does not exist:[/red] {report}")
        raise typer.Exit(code=2) from None
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]The report is not valid JSON:[/red] {exc}")
        raise typer.Exit(code=2) from None

    result = admit(payload)

    if as_json:
        # Deliberately not the Rich console: it wraps to the terminal width, and
        # a hard break inside a long `detail` string produces output that is no
        # longer JSON. `print_json` is no better — it imports `rich.json`, which
        # the Rich resolved for 3.10 does not ship. Machine output leaves the
        # renderer alone.
        print(json.dumps(result.as_dict(), indent=2))
    else:
        colour = {"rejected": "red", "flagged": "yellow", "accepted": "green"}[result.verdict]
        console.print(f"\n[{colour} bold]{result.verdict.upper()}[/{colour} bold]")
        for finding in result.findings:
            mark = "[red]reject[/red]" if finding.verdict == "reject" else "[yellow]flag[/yellow]"
            console.print(f"  {mark}  [bold]{_plain(finding.rule)}[/bold]")
            console.print(f"          {_plain(finding.detail)}")
        if not result.findings:
            console.print(
                "  [dim]Nothing found. This says the row is comparable, not that the "
                "figures are good.[/dim]"
            )

    raise typer.Exit(code=1 if result.verdict == REJECTED else 0)


@app.command("compare")
def compare_command(report: ReportInputs, out: ComparisonOut = None) -> None:
    """Compares several reports: quantizations, backends or configurations."""
    try:
        loaded = load_reports(list(report))
        comparison = compare(loaded)
    except ComparisonError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    for warning in comparison["warnings"]:
        console.print(f"[yellow]Warning:[/yellow] {warning}")

    _print_comparison(comparison)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(canonical_json(comparison) + "\n", encoding="utf-8")
        console.print(f"\nComparison written to [bold]{out}[/bold]")


ImporterName = Annotated[
    str, typer.Argument(help="Dataset to convert: 'ifeval' or 'xstest'.")
]
ImportSource = Annotated[
    Path,
    typer.Option("--input", "-i", help="File downloaded from the original source."),
]
ImportOut = Annotated[
    Path, typer.Option("--out", "-o", help="Directory for the converted dataset.")
]
ImportLimit = Annotated[
    int | None,
    typer.Option("--limit", help="Subset size; defaults to the whole dataset."),
]
ImportSeed = Annotated[
    int, typer.Option("--seed", help="Seed for the stratified sampling.")
]
ImportAppend = Annotated[
    bool,
    typer.Option("--append", help="Adds the task to an existing dataset instead of creating one."),
]


@app.command("import")
def import_command(
    name: ImporterName,
    input_path: ImportSource,
    out: ImportOut,
    limit: ImportLimit = None,
    seed: ImportSeed = 1234,
    append: ImportAppend = False,
) -> None:
    """Converts a public dataset to the OPAAS format."""
    try:
        result = import_dataset(name, input_path, out, limit=limit, seed=seed, append=append)
    except (ImporterError, OSError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    dataset = load_dataset(str(result.dataset_dir))
    console.print(f"[green]Converted {result.items} items.[/green]")
    console.print(f"  directory : {result.dataset_dir}")
    console.print(f"  license   : {dataset.license}")
    console.print(f"  hash      : {dataset.content_hash}")
    for note in result.notes:
        console.print(f"[yellow]{note}[/yellow]" if "WARNING" in note else f"[dim]{note}[/dim]")
    console.print(
        f"\nUse it with: opaas run --model <id> --backend <b> "
        f"--suite {'safety' if name == 'xstest' else 'capability'} "
        f"--dataset {result.dataset_dir}"
    )


PublishReports = Annotated[
    list[Path],
    typer.Option("--report", "-r", help="Report to publish. Repeat for each certificate."),
]
PublishCredentials = Annotated[
    list[Path],
    typer.Option("--credential", "-c", help="Corresponding credential, in the same order."),
]
PublishOut = Annotated[
    Path, typer.Option("--out", "-o", help="Directory for the generated static site.")
]
PublishDid = Annotated[
    Path,
    typer.Option("--did-document", help="did.json document with the issuer's public key."),
]
PublishRevocations = Annotated[
    Path | None,
    typer.Option(
        "--revocations",
        help="The issuer's signed revocation list, so the browser checks it too (D65).",
    ),
]


@app.command("publish")
def publish(
    report: PublishReports,
    credential: PublishCredentials,
    did_document: PublishDid,
    out: PublishOut = Path("site"),
    revocations: PublishRevocations = None,
) -> None:
    """Generates the static certificate site."""
    if len(report) != len(credential):
        err_console.print(
            f"[red]There are {len(report)} reports and {len(credential)} credentials.[/red] "
            "Each certificate needs its pair, in the same order."
        )
        raise typer.Exit(code=2)

    try:
        did = json.loads(did_document.read_text(encoding="utf-8"))
        revocation_list = read_json(revocations) if revocations is not None else None
        if revocation_list is not None and not is_revocation_list(revocation_list):
            raise SiteError(f"{revocations} is not a revocation list.")
        entries = [
            load_entry(report_path, credential_path)
            for report_path, credential_path in zip(report, credential, strict=True)
        ]
        written = build_site(entries, out, did, revocations=revocation_list)
    except (SiteError, OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    console.print(f"[green]Site generated with {len(entries)} certificate(s).[/green]")
    for entry in entries:
        if entry.is_system:
            state = "certifiable" if entry.complete else "NOT certifiable"
        else:
            state = "complete" if entry.complete else "PARTIAL"
        console.print(f"  * {entry.model} ({state}) → {out / 'cert' / entry.slug}/index.html")
    console.print(f"\n  {len(written)} files in [bold]{out}[/bold]")
    console.print(
        "\n[dim]Serve it over HTTPS: in-browser verification needs a secure context. "
        "The DID document goes at /.well-known/did.json.[/dim]"
    )


FetchSource = Annotated[
    str | None,
    typer.Argument(
        help="Dataset or classifier ('toxicity', 'faithfulness') to download; with no "
        "argument, every dataset, plus the classifiers if that extra is installed."
    ),
]


@app.command("fetch")
def fetch_command(source: FetchSource = None, limit: ItemLimit = None) -> None:
    """Downloads datasets — and, with the classifiers extra installed, the pinned default
    classifiers — into the local cache, so as not to depend on the network later."""
    known = [*available_sources(), *DEFAULT_CLASSIFIERS]
    if source:
        names = [source]
    else:
        # Not fetched unasked unless the extra is already installed: a plain
        # dataset-only install should not suddenly pull ~800 MB of model
        # weights just because it ran `fetch` with no argument.
        names = [*available_sources(), *(DEFAULT_CLASSIFIERS if classifiers_available() else [])]
    unknown = [name for name in names if name not in known]
    if unknown:
        err_console.print(
            f"[red]Unknown source: {', '.join(unknown)}.[/red] Available: {', '.join(known)}"
        )
        raise typer.Exit(code=2)

    # `--limit` trims a dataset's items; a classifier has none. Accepting it
    # there and quietly doing nothing is the kind of silent no-op this tool
    # exists to catch elsewhere.
    if limit is not None and source in DEFAULT_CLASSIFIERS:
        err_console.print(
            f"[red]--limit does not apply to the '{source}' classifier:[/red] it trims a "
            "dataset's items, and a classifier has none. Drop the flag, or name a dataset "
            f"({', '.join(available_sources())})."
        )
        raise typer.Exit(code=2)
    if limit is not None and any(name in DEFAULT_CLASSIFIERS for name in names):
        console.print(
            f"[dim]--limit {limit} applies to the datasets; the classifiers are downloaded "
            "whole.[/dim]"
        )

    for name in names:
        if name in DEFAULT_CLASSIFIERS:
            if is_default_classifier_cached(name):
                console.print(f"[dim]{name}: already cached[/dim]")
                continue
            console.print(f"Downloading the default {name} classifier…")
            try:
                path = ensure_default_classifier(name)
            except ClassifierError as exc:
                # The message names 'opaas[classifiers]': unescaped, Rich
                # reads that bracket as a style tag and eats the install hint.
                err_console.print(f"[red]{_plain(exc)}[/red]")
                raise typer.Exit(code=1) from None
            console.print(f"[green]{name}[/green]: cached at {path}")
            continue
        if is_cached(name):
            console.print(f"[dim]{name}: already cached[/dim]")
            continue
        console.print(f"Downloading {name}…")
        try:
            path = ensure_dataset(name, limit=limit)
        except CacheError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
        dataset = load_dataset(str(path))
        console.print(
            f"[green]{name}[/green]: {dataset.item_count} items · {dataset.content_hash}"
        )

    console.print(f"\nCache at [bold]{cache_dir()}[/bold]")
    console.print(
        "[dim]For a deployment without a network, copy that directory to the target "
        "machine and run with --offline.[/dim]"
    )


#: 'source' is downloaded and hash-verified, but converted inside a composition
#: rather than into its own directory. It needs no network either.
_SOURCE_STATUS = {
    "dataset": "[green]cached[/green]",
    "source": "[green]cached (in a composition)[/green]",
    "missing": "[yellow]not downloaded[/yellow]",
}


@app.command("datasets")
def datasets_command() -> None:
    """Lists the available datasets and the cache status."""
    remote = Table(title="Downloadable sources")
    remote.add_column("source", style="bold")
    remote.add_column("block")
    remote.add_column("license")
    remote.add_column("status")
    for entry in describe_cache():
        remote.add_row(
            str(entry["name"]),
            str(entry["block"]),
            str(entry["license"]),
            _SOURCE_STATUS[str(entry["status"])],
        )
    console.print(remote)

    cached = describe_cached_datasets()
    if cached:
        built = Table(title="Datasets built in the cache")
        built.add_column("dataset", style="bold")
        built.add_column("version")
        built.add_column("items", justify="right")
        built.add_column("tasks")
        built.add_column("hash")
        built.add_column("use")
        for entry in cached:
            tasks: list[tuple[str, int]] = entry["tasks"]  # type: ignore[assignment]
            built.add_row(
                str(entry["name"]),
                str(entry["version"]),
                str(entry["items"]),
                ", ".join(f"{category} ({count})" for category, count in tasks),
                short_hash(str(entry["hash"]), 12),
                "[yellow]smoke only[/yellow]"
                if entry["synthetic_smoke"]
                else "[green]evaluation[/green]",
            )
        console.print(built)

    local = Table(title="Packaged datasets")
    local.add_column("dataset", style="bold")
    local.add_column("version")
    local.add_column("items", justify="right")
    local.add_column("tasks")
    local.add_column("hash")
    local.add_column("use")
    for name in builtin_datasets():
        dataset = load_dataset(name)
        local.add_row(
            dataset.name,
            dataset.version,
            str(dataset.item_count),
            ", ".join(f"{t.category} ({len(t.items)})" for t in dataset.tasks),
            short_hash(dataset.content_hash, 12),
            "[yellow]smoke only[/yellow]"
            if dataset.synthetic_smoke
            else "[green]evaluation[/green]",
        )
    console.print(local)

    console.print()
    if classifiers_available():
        classifiers_table = Table(title="Block D classifiers (faithfulness, toxicity)")
        classifiers_table.add_column("task", style="bold")
        classifiers_table.add_column("pinned default")
        classifiers_table.add_column("status")
        for task, source in DEFAULT_CLASSIFIERS.items():
            classifiers_table.add_row(
                task,
                f"{source.repo_id}@{source.revision[:12]}",
                "[green]cached[/green]"
                if is_default_classifier_cached(task)
                else "[yellow]not downloaded — fetched on first use[/yellow]",
            )
        console.print(classifiers_table)
        console.print(
            "[dim]Or point --toxicity-model / --faithfulness-model at weights of your "
            "own.[/dim]"
        )
    else:
        console.print(
            "[dim]Block D classifiers (faithfulness, toxicity): the classifiers extra is "
            "not installed — pip install 'opaas\\[classifiers]'.[/dim]"
        )

    console.print(
        f"\n[dim]Cache: {cache_dir()} · downloaded on first use and reused. "
        "A run over several sources (the default, --dataset ifeval,xstest) evaluates the "
        "composition, and it is the composition's hash that goes in the report. "
        "Other datasets (MMLU-Pro, GPQA): see docs/datasets.md.[/dim]"
    )


#: Below this, a passphrase does not protect much.
_MIN_PASSPHRASE = 12


def _ask_passphrase() -> str | None:
    """Asks for the passphrase at the keyboard, twice.

    It is not accepted via argument without a warning: `--passphrase` ends up
    written in the shell history and visible in the process list for as long as
    the command runs. For the key that signs every certificate, that will not
    do.
    """
    if not sys.stdin.isatty():
        err_console.print(
            "[red]There is no interactive terminal to ask for the passphrase.[/red]\n"
            "  Run it by hand, or pass it via the environment: $env:OPAAS_KEY_PASSPHRASE = '...'"
        )
        raise typer.Exit(code=2)

    console.print(
        "[dim]The passphrase is not echoed as you type it and stays out of the history.[/dim]"
    )
    first = getpass.getpass("Passphrase for the root key: ")
    if not first:
        console.print(
            "[yellow]With no passphrase, the private key is left unencrypted[/yellow] — "
            "compromised the moment the machine is."
        )
        if not typer.confirm("Generate it unencrypted anyway?", default=False):
            raise typer.Exit(code=1)
        return None

    if getpass.getpass("Repeat it: ") != first:
        err_console.print("[red]The passphrases do not match.[/red]")
        raise typer.Exit(code=1)
    if len(first) < _MIN_PASSPHRASE:
        console.print(
            f"[yellow]Warning:[/yellow] {len(first)} characters. For a certifier's root of "
            f"trust, {_MIN_PASSPHRASE} is the reasonable minimum."
        )
    return first


@app.command("keygen")
def keygen(
    out_dir: KeysDir = Path("keys"),
    passphrase: Passphrase = None,
    did: Did = None,
    overwrite: Overwrite = False,
) -> None:
    """Generates a root Ed25519 pair (root of trust) and its DID document."""
    if passphrase is None:
        passphrase = _ask_passphrase()
    elif not os.environ.get("OPAAS_KEY_PASSPHRASE"):
        console.print(
            "[yellow]Warning:[/yellow] the passphrase came in as an argument, so it is in "
            "the shell history and was visible in the process list. For a root key, better "
            "leave it blank and type it when prompted."
        )

    try:
        paths = generate_root_keypair(out_dir, passphrase=passphrase, did=did, overwrite=overwrite)
    except FileExistsError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    public_key = load_public_key(paths.public_key)
    issuer = json.loads(paths.did_document.read_text(encoding="utf-8"))["id"]
    console.print("[green]Root key pair generated.[/green]")
    console.print(f"  issuer      : {issuer}")
    console.print(f"  private     : {paths.private_key}")
    console.print(f"  public      : {paths.public_key}")
    where = "  → publish at /.well-known/did.json" if issuer.startswith("did:web:") else ""
    console.print(f"  document    : {paths.did_document}{where}")
    console.print(f"  fingerprint : {fingerprint(public_key)}")
    if did is None:
        console.print(
            "[dim]Named by its own key (did:key): it claims no organisation. To sign as one, "
            "generate with --did did:web:<a domain you control> and publish the document "
            "there.[/dim]"
        )
    if not passphrase:
        console.print(
            "[yellow]The private key is unencrypted.[/yellow] See docs/key-custody.md before "
            "signing anything with it."
        )


InputReport = Annotated[
    Path, typer.Option("--input", "-i", help="Report generated by 'opaas run'.")
]
SigningKey = Annotated[
    Path, typer.Option("--key", "-k", help="Ed25519 private key to sign with.")
]
CredentialOut = Annotated[
    Path, typer.Option("--out", "-o", help="Credential file to write.")
]
Issuer = Annotated[
    str | None,
    typer.Option(
        "--issuer",
        help="Issuer DID. Defaults to the one in the did.json that keygen wrote next to the "
        "key; must match it when both are present.",
    ),
]
Validity = Annotated[
    int, typer.Option("--validity-days", help="Days of validity before re-certifying.")
]
ReportUrl = Annotated[
    str | None, typer.Option("--report-url", help="Public URL of the report, if already published.")
]
LogFile = Annotated[
    Path | None,
    typer.Option("--log", help="Transparency log (JSONL) to record the issuance in."),
]
SigningDelegation = Annotated[
    Path | None,
    typer.Option(
        "--delegation",
        help="Delegation from the root to this key ('opaas delegate'). The credential is "
        "issued under the root's name and carries it; refused if it has expired or names "
        "another key (D65).",
    ),
]




def _issuing_identity(
    key: Path, private_key: Any, issuer: str | None, delegation: Path | None
) -> tuple[str, str, dict[str, Any] | None]:
    """(issuer, verification method, delegation) a credential is signed under.

    With a delegation the name is the root's, taken from the delegation once
    it is checked to name this key and to cover now (D65); otherwise it is
    the key's own, from the did.json beside it (D39).
    """
    if delegation is None:
        return (*_signing_identity(key, private_key, issuer), None)
    try:
        document = read_json(delegation)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{delegation} is not valid JSON: {exc}") from None
    named, key_id = identity_for_signing(document, private_key.public_key())
    if issuer is not None and issuer != named:
        raise ValueError(
            f"The delegation is from {named!r}, not {issuer!r}: the credential is issued under "
            "the name of the root that delegated this key. Drop --issuer or use the right "
            "delegation."
        )
    return named, key_id, document


@app.command("sign")
def sign(
    input_path: InputReport,
    key: SigningKey,
    out: CredentialOut = Path("credential.json"),
    issuer: Issuer = None,
    passphrase: Passphrase = None,
    validity_days: Validity = DEFAULT_VALIDITY_DAYS,
    report_url: ReportUrl = None,
    log: LogFile = None,
    delegation: SigningDelegation = None,
) -> None:
    """Issues the signed W3C credential from a report, or from a system report (D60)."""
    if not input_path.exists():
        err_console.print(f"[red]The report does not exist:[/red] {input_path}")
        raise typer.Exit(code=2)

    try:
        report = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]The report is not valid JSON:[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        private_key = _load_signing_key(key, passphrase)
    except (OSError, TypeError, ValueError) as exc:
        err_console.print(f"[red]The private key could not be loaded:[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        issuer, key_id, delegation_document = _issuing_identity(
            key, private_key, issuer, delegation
        )
    except (OSError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    is_system = isinstance(report, dict) and report.get("kind") == "system-report"
    try:
        credential = (build_system_credential if is_system else build_credential)(
            report,
            # The hash of the report's BYTES is signed: verifying is sha256sum.
            file_digest(input_path),
            issuer=issuer,
            key_id=key_id,
            validity_days=validity_days,
            report_url=report_url,
        )
    except CredentialError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    try:
        signed = sign_credential(
            credential, private_key, key_id=key_id, delegation=delegation_document
        )
    except CredentialError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    entry = None
    if log is not None:
        try:
            entry = append_entry(log, signed, file_digest(input_path))
        except TransparencyError as exc:
            err_console.print(f"[red]Could not record in the log:[/red] {exc}")
            raise typer.Exit(code=2) from None

    subject = signed["credentialSubject"]
    console.print("[green]Credential signed.[/green]")
    console.print(f"  id       : {signed['id']}")
    console.print(f"  public   : {public_identifier(signed)}")
    console.print(f"  report   : {file_digest(input_path)}")
    console.print(f"  validity : {signed['issuanceDate']} → {signed['expirationDate']}")
    if delegation_document is not None:
        console.print(f"  key      : {_plain(key_id)}, delegated until "
                      f"{delegation_document['validUntil']}")
    console.print(f"  file     : {out}")
    if entry is not None:
        console.print(f"  log      : entry {entry.index} in {log} ({entry.entryHash})")
    if is_system:
        console.print(f"  system   : {_plain(subject['system']['id'])}, "
                      f"{len(subject['components'])} component(s)")
        if not subject["certifiable"]:
            console.print("\n[yellow]Attention:[/yellow] the system is NOT certifiable, and the "
                          "credential says so:")
            for reason in subject["notCertifiableBecause"]:
                console.print(f"  - {_plain(reason)}")
        return
    eval_run = subject["evalRun"]
    if not eval_run["completeEvaluation"]:
        console.print(
            f"\n[yellow]Attention:[/yellow] this credential certifies a PARTIAL evaluation "
            f"(blocks {', '.join(eval_run['blocksEvaluated']) or 'none'} of "
            f"A, B, C, D, E). It is declared inside the credential itself."
        )


CredentialIn = Annotated[
    Path, typer.Option("--credential", "-c", help="Credential to verify.")
]
VerifyKey = Annotated[
    Path | None, typer.Option("--key", "-k", help="Issuer public key, if there is no anchor.")
]
TrustAdd = Annotated[
    Path | None,
    typer.Option(
        "--add",
        help="DID document to pin as a trust anchor. Its fingerprint is shown before "
        "installing: check it against a second channel, not against the file you were sent.",
    ),
]
TrustYes = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Install without asking. For provisioning scripts."),
]
DidDocument = Annotated[
    Path | None, typer.Option("--did-document", help="Issuer did.json document.")
]
VerifyReport = Annotated[
    Path | None,
    typer.Option("--report", help="Report to check against the certified hash."),
]
VerifyRevocations = Annotated[
    Path | None,
    typer.Option(
        "--revocations",
        help="The issuer's signed revocation list. Without it, the ones installed beside the "
        "anchors are used; never one from the credential.",
    ),
]


@app.command("log-verify")
def log_verify(log: Annotated[Path, typer.Argument(help="JSONL log to check.")]) -> None:
    """Checks that the transparency log has not been altered."""
    try:
        entries = load_log(log)
        summary = verify_log(entries)
    except TransparencyError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    console.print(f"[green]Log intact:[/green] {summary['entries']} entries")
    console.print(f"  head: {summary['headHash']}")
    for entry in entries[-5:]:
        console.print(f"  [dim]{entry.index:>4}  {entry.issuedAt}  {entry.credentialId}[/dim]")
    console.print(
        "\n[dim]A hash chain proves this log is internally consistent. Detecting a full "
        "rewrite requires comparing against an earlier copy — which is why it is worth "
        "publishing it in a repository with history.[/dim]"
    )


@app.command("trust")
def trust_command(add: TrustAdd = None, yes: TrustYes = False) -> None:
    """Lists the pinned trust anchors, or pins a new one.

    Pinning an anchor is the moment the whole chain rests on: from then on,
    `verify` accepts anything that issuer signs. The fingerprint is printed
    before anything is written so it can be compared against a channel other
    than the one the file arrived through — a file that came with its own
    assurance that it is genuine assures nothing.
    """
    directory = user_anchor_dir()

    if add is not None:
        try:
            document = json.loads(add.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            err_console.print(f"[red]Does not exist:[/red] {add}")
            raise typer.Exit(code=2) from None
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]{add} is not valid JSON:[/red] {exc}")
            raise typer.Exit(code=2) from None

        if is_revocation_list(document):
            _install_revocation_list(document, add, yes)
            return

        issuer = str(document.get("id") or "")
        try:
            key = public_key_from_did_document(document)
        # keys.py raises bare ValueErrors and vc.py raises CredentialError, which
        # subclasses it: one clause covers a malformed document either way.
        except ValueError as exc:
            err_console.print(f"[red]{_plain(exc)}[/red]")
            raise typer.Exit(code=2) from None

        console.print(f"  issuer      : {_plain(issuer)}")
        console.print(f"  fingerprint : [bold]{fingerprint(key)}[/bold]")
        console.print(
            "\n[yellow]Check that fingerprint against a channel other than the one this "
            "file came through.[/yellow] Once pinned, every credential this issuer signs "
            "verifies without further questions."
        )
        if not yes and not typer.confirm("Pin this issuer?", default=False):
            raise typer.Exit(code=1)

        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{slugify(issuer) or 'anchor'}.json"
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Pinned:[/green] {target}")
        return

    if not directory.is_dir() or not (found := sorted(directory.glob("*.json"))):
        console.print(f"No anchors pinned in {directory}.")
        console.print(
            "[dim]Pin one with: opaas trust --add did.json[/dim]"
        )
        return

    table = Table(title=f"Trust anchors in {directory}")
    table.add_column("issuer", style="bold")
    table.add_column("fingerprint")
    table.add_column("file")
    for path in found:
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            mark = fingerprint(public_key_from_did_document(document))
            issuer = str(document.get("id") or "—")
        except (ValueError, OSError) as exc:
            issuer, mark = "—", f"[red]unreadable: {type(exc).__name__}[/red]"
        table.add_row(_plain(issuer), mark, _plain(path.name))
    console.print(table)


def _install_revocation_list(document: dict[str, Any], source: Path, yes: bool) -> None:
    """Installs an issuer's revocation list beside the anchors, once it verifies under one.

    The root that must have signed it is resolved like any anchor, never from
    the list. An older list never replaces a newer one: that would quietly
    un-revoke a key.
    """
    issuer = str(document.get("issuer") or "")
    try:
        root = resolve_trust(issuer)
    except TrustAnchorError as exc:
        err_console.print(f"[red]Pin the issuer's anchor first.[/red] {_plain(exc)}")
        raise typer.Exit(code=2) from None
    signed_by = (document.get("proof") or {}).get("verificationMethod")
    root_key = root.exact(signed_by)
    ok, detail = (
        check_proof(document, root_key, proof_purpose=DELEGATION)
        if root_key is not None
        else (False, f"signed by {signed_by!r}, which is not a key of {root.origin}")
    )
    if not ok:
        err_console.print(f"[red]{source} does not verify under {_plain(issuer)}:[/red] "
                          f"{_plain(detail)}")
        raise typer.Exit(code=2)

    target = user_anchor_dir() / REVOCATIONS_DIRNAME / f"{slugify(issuer) or 'issuer'}.json"
    if target.is_file():
        current = read_json(target)
        if str(current.get("updated", "")) > str(document.get("updated", "")):
            err_console.print(
                f"[red]The installed list ({current.get('updated')}) is newer than this one "
                f"({document.get('updated')}).[/red] Installing it would un-revoke keys."
            )
            raise typer.Exit(code=2)
    revoked = document.get("revoked") or []
    console.print(f"  issuer  : {_plain(issuer)}")
    console.print(f"  updated : {_plain(document.get('updated'))}, signed by {_plain(signed_by)}")
    console.print(f"  revokes : {len(revoked)} key(s)")
    for entry in revoked:
        console.print(f"    - {_plain(entry.get('id'))} ({_plain(entry.get('reason'))})")
    if not yes and not typer.confirm("Install this revocation list?", default=True):
        raise typer.Exit(code=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]Installed:[/green] {target}")


@app.command("verify")
def verify(
    credential: CredentialIn,
    key: VerifyKey = None,
    did_document: DidDocument = None,
    report: VerifyReport = None,
    log: LogFile = None,
    revocations: VerifyRevocations = None,
) -> None:
    """Verifies a third-party credential, without a network."""
    if not credential.exists():
        err_console.print(f"[red]The credential does not exist:[/red] {credential}")
        raise typer.Exit(code=2)

    try:
        document = json.loads(credential.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]The credential is not valid JSON:[/red] {exc}")
        raise typer.Exit(code=2) from None

    issuer = str(document.get("issuer", ""))
    try:
        root, lists, list_origins = _verification_trust(
            document, key=key, did_document=did_document, revocations=revocations
        )
    except (TrustAnchorError, ValueError) as exc:
        err_console.print(f"[red]Cannot verify:[/red] {exc}")
        raise typer.Exit(code=2) from None
    origin = root.origin

    result = verify_credential(document, root, report_path=report, revocation_lists=lists)

    if log is not None:
        try:
            entries = load_log(log)
            verify_log(entries)
            included, detail = inclusion_check(
                entries, document, file_digest(report) if report else ""
            )
            result.add("log", included, detail)
        except TransparencyError as exc:
            result.add("log", False, str(exc))

    console.print(f"\nCredential : {document.get('id', 'no id')}")
    console.print(f"Issuer     : {issuer}")
    console.print(f"Key        : {origin}")
    for list_origin in list_origins:
        console.print(f"Revocations: {list_origin}")
    for check in result.checks:
        mark = "[green]OK  [/green]" if check.ok else "[red]FAIL[/red]"
        console.print(f"  {mark} {check.name}: {check.detail}")

    for warning in result.warnings:
        console.print(f"\n[yellow]{warning}[/yellow]")

    if result.valid:
        console.print("\n[green bold]Credential valid.[/green bold]")
    else:
        err_console.print("\n[red bold]Credential NOT valid.[/red bold]")
        raise typer.Exit(code=1)


CertificateReport = Annotated[
    Path, typer.Option("--report", "-r", help="Report the credential certifies.")
]
CertificateOut = Annotated[
    Path, typer.Option("--out", "-o", help="HTML file to write, ready to print to PDF.")
]
CertificateUrl = Annotated[
    str | None,
    typer.Option(
        "--url",
        help="Public URL of the certificate page made by 'opaas publish'. Becomes the QR "
        "code that leads from the paper to the signature.",
    ),
]


@app.command("certificate")
def certificate_command(
    report: CertificateReport,
    credential: CredentialIn,
    out: CertificateOut = Path("certificate.html"),
    url: CertificateUrl = None,
    key: VerifyKey = None,
    did_document: DidDocument = None,
) -> None:
    """Renders a printable certificate (A4, to PDF) from a credential that verifies.

    The signature is checked first, with the same key resolution as `verify`:
    a printed certificate for a credential that would fail verification is a
    forgery with good typography.
    """
    try:
        entry = load_entry(report, credential)
        if url is not None:
            check_verify_url(url)
    except SiteError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    document = entry.credential
    try:
        root, lists, _ = _verification_trust(document, key=key, did_document=did_document)
    except (TrustAnchorError, ValueError) as exc:
        err_console.print(f"[red]Cannot verify, so it will not be printed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    origin = root.origin

    result = verify_credential(document, root, report_path=report, revocation_lists=lists)
    if not result.valid:
        err_console.print(
            "[red bold]The credential does NOT verify; nothing was written.[/red bold]"
        )
        for check in result.checks:
            if not check.ok:
                err_console.print(f"  [red]FAIL[/red] {check.name}: {_plain(check.detail)}")
        raise typer.Exit(code=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_printable(entry, verify_url=url), encoding="utf-8")

    console.print(f"[green]Certificate written:[/green] {out}")
    console.print(f"  credential : {document.get('id')}")
    console.print(f"  key        : {origin}")
    console.print(f"  valid until: {document.get('expirationDate')}")
    if not entry.complete and entry.is_system:
        console.print(
            "\n[yellow]Attention:[/yellow] the system is NOT certifiable. The certificate "
            "lists every reason."
        )
    elif not entry.complete:
        console.print(
            "\n[yellow]Attention:[/yellow] PARTIAL evaluation. The certificate lists the "
            "blocks that were not evaluated."
        )
    if url is None:
        console.print(
            "\n[yellow]No --url:[/yellow] the certificate has no QR code, so a reader can "
            "only check it with 'opaas verify'. Publish the site and pass the page URL."
        )
    console.print(
        "\n[dim]Open it in a browser and print to PDF (A4, landscape). The PDF is a "
        "rendering: what is verified is the signed credential.[/dim]"
    )


WatchFull = Annotated[
    bool,
    typer.Option(
        "--full",
        help="Also run the statistical checks (latency, egress, a small block B/D sample) "
        "even when the fingerprint or the canaries already decided.",
    ),
]
WatchOut = Annotated[
    Path, typer.Option("--out", "-o", help="Where to write the result (JSON).")
]
WatchLog = Annotated[
    Path | None,
    typer.Option("--log", help="Hash-chained JSONL history of results to append to."),
]
WatchInventory = Annotated[
    bool,
    typer.Option(
        "--inventory/--no-inventory",
        help="Also re-discover what the host serves and compare it with the certified "
        "inventory (D40). It reports, but never decides 'current' on its own.",
    ),
]
WatchMember = Annotated[
    list[str],
    typer.Option(
        "--member",
        help="For a system certificate: component=path of a member that is not next to the "
        "system report. Each is checked against the hash the report records. Repeatable.",
    ),
]


@app.command("watch")
def watch_command(
    report: CertificateReport,
    credential: CredentialIn,
    key: VerifyKey = None,
    did_document: DidDocument = None,
    base_url: BaseUrl = None,
    api_key: ApiKey = None,
    weights_path: WeightsPath = None,
    full: WatchFull = False,
    out: WatchOut = Path("watch.json"),
    log: WatchLog = None,
    inventory: WatchInventory = False,
    member: WatchMember = None,
) -> None:
    """Checks that a certificate still describes the deployment (exit 0/1/3, 2 on error).

    One-shot, for cron or a systemd timer. Exit codes: 0 current, 1 drifted or
    expired, 3 inconclusive, 2 when the inputs are wrong. It signs nothing and
    never reads user traffic.
    """
    try:
        entry = load_entry(report, credential)
    except SiteError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    document = entry.credential
    try:
        root, lists, _ = _verification_trust(document, key=key, did_document=did_document)
    except (TrustAnchorError, ValueError) as exc:
        err_console.print(f"[red]Cannot verify the certificate to watch:[/red] {exc}")
        raise typer.Exit(code=2) from None

    verification = verify_credential(
        document, root, report_path=report, revocation_lists=lists
    )
    failed = {check.name: check.detail for check in verification.checks if not check.ok}
    report_hash = str(entry.report_hash)

    def finish(state: str, body: dict[str, Any] | None = None, detail: str | None = None):
        envelope_of = system_result if entry.is_system else result_envelope
        result = envelope_of(
            state, credential=document, report_hash=report_hash, body=body, detail=detail
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if log is not None:
            try:
                append_watch_log(log, result)
            except SentinelError as exc:
                err_console.print(f"[red]{_plain(exc)}[/red]")
                raise typer.Exit(code=2) from None
        _print_watch(result, out)
        raise typer.Exit(code=EXIT_CODES[state])

    if set(failed) - {"validity"}:
        for name, detail in failed.items():
            err_console.print(f"  [red]FAIL[/red] {name}: {_plain(detail)}")
        err_console.print(
            "[red]This certificate does not verify, so there is nothing to watch against.[/red]"
        )
        raise typer.Exit(code=2)
    if "validity" in failed:
        finish("expired", detail=failed["validity"])
    if entry.is_system:
        try:
            body = _watch_system(entry, report, member or [], api_key=api_key, full=full,
                                 inventory=inventory)
        except ValueError as exc:
            err_console.print(f"[red]{_plain(exc)}[/red]")
            raise typer.Exit(code=2) from None
        finish(body["state"], body)

    effective = entry.report.get("configuration", {}).get("effectiveConfig", {})
    backend_name, model = effective.get("backend"), effective.get("model")
    if not backend_name or not model:
        err_console.print(
            "[red]The report does not record which backend and model it ran against.[/red]"
        )
        raise typer.Exit(code=2)
    try:
        backend = get_backend(str(backend_name))
    except BackendError as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    # The canaries must go through the prompt mode and window that were certified.
    config = {
        name: effective.get(name)
        for name in ("prompt_mode", "num_ctx", "max_concurrency", "quantization", "timeout_s")
        if effective.get(name) is not None
    }
    config["base_url"] = base_url or effective.get("base_url")
    config["api_key"] = api_key
    try:
        backend.load(str(model), config)
    except BackendError as exc:
        finish("inconclusive", detail=f"The endpoint could not be reached: {exc}")

    try:
        weights = weights_hash(weights_path) if weights_path else None
        current = deployment_fingerprint(
            backend,
            backend.get_backend_metadata(),
            describe_environment(),
            weights_path=weights_path,
            weights=weights,
            tokenizer_files=tokenizer_files_hash(weights_path) if weights_path else None,
        )
        current_inventory = discovery.discover_environment() if inventory else None
        body = run_watch(entry.report, backend, current, full=full,
                         current_inventory=current_inventory)
    finally:
        backend.close()
    finish(body["state"], body)


def _watch_model(
    report: dict[str, Any], *, base_url: str | None, api_key: str | None, full: bool
) -> dict[str, Any]:
    """`opaas watch` for one certified bench report, as one component of a system."""
    effective = report.get("configuration", {}).get("effectiveConfig", {})
    backend_name, model = effective.get("backend"), effective.get("model")
    if not backend_name or not model:
        return {"state": "inconclusive", "carriedBy": [], "checks": {},
                "detail": "The report does not record which backend and model it ran against."}
    try:
        backend = get_backend(str(backend_name))
        config = {name: effective.get(name)
                  for name in ("prompt_mode", "num_ctx", "max_concurrency", "quantization",
                               "timeout_s")
                  if effective.get(name) is not None}
        config["base_url"] = base_url or effective.get("base_url")
        config["api_key"] = api_key
        backend.load(str(model), config)
    except BackendError as exc:
        return {"state": "inconclusive", "carriedBy": [], "checks": {},
                "detail": f"The endpoint could not be reached: {exc}"}
    try:
        current = deployment_fingerprint(backend, backend.get_backend_metadata(),
                                         describe_environment())
        return run_watch(report, backend, current, full=full)
    finally:
        backend.close()


def _watch_system(
    entry: Any, report_path: Path, members: list[str], *, api_key: str | None, full: bool,
    inventory: bool,
) -> dict[str, Any]:
    """Every component of a system certificate, and which one moved (D60, §10.3)."""
    from opaas.discover import discover as discover_full
    from opaas.sentinel.inventory import check_environment
    from opaas.system.watch import watch_system

    supplied = {}
    for item in members:
        component, sep, path = item.partition("=")
        if not sep or not path:
            raise ValueError(f"--member takes component=path, got {item!r}.")
        supplied[component] = Path(path)
    base = report_path.resolve().parent
    model_reports: dict[str, dict[str, Any]] = {}
    baseline_inventory = None
    for recorded in entry.report.get("members") or []:
        path = supplied.get(recorded.get("component"))
        if recorded.get("kind") not in ("model-report", "inventory"):
            continue
        path = path if path and recorded.get("kind") == "model-report" else (
            base / str(recorded.get("file")))
        if not path.is_file():
            continue
        if file_digest(path) != recorded.get("hash"):
            raise ValueError(
                f"{path} is not the member the system report certifies: its hash is not "
                f"{recorded.get('hash')}."
            )
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if recorded["kind"] == "model-report":
            model_reports[str(recorded["component"])] = document
        else:
            baseline_inventory = document.get("inventory")
    current = discover_full(containers=False) if inventory and baseline_inventory else None
    return watch_system(
        entry.report, model_reports=model_reports,
        check_model=lambda _cid, doc: _watch_model(doc, base_url=None, api_key=api_key,
                                                   full=full),
        baseline_inventory=baseline_inventory, current_inventory=current,
        check_environment=check_environment,
    )


_STATE_STYLE = {
    "current": "green",
    "drifted": "red",
    "expired": "red",
    "inconclusive": "yellow",
    "disabled": "dim",
    "not-checked": "dim",
}


def _print_system_watch(result: dict[str, Any], out: Path) -> None:
    state = result["state"]
    console.print(
        f"[bold {_STATE_STYLE[state]}]{state.upper()}[/bold {_STATE_STYLE[state]}]"
        f"  ·  {_plain(result.get('credentialId'))}"
    )
    if result.get("detail"):
        console.print(f"  {_plain(result['detail'])}")
    for component, entry in result["components"].items():
        style = _STATE_STYLE.get(entry["state"], "white")
        line = f"  [{style}]{entry['state']:<13}[/{style}] {_plain(component)} ({entry['kind']})"
        if entry.get("carriedBy"):
            line += f" · carried by {', '.join(entry['carriedBy'])}"
        console.print(line)
        if entry.get("detail") and entry["state"] != "current":
            console.print(f"                  [dim]{_plain(entry['detail'])}[/dim]")
    environment = result.get("environment")
    if environment:
        style = _STATE_STYLE.get(environment["state"], "white")
        console.print(f"  [{style}]{environment['state']:<13}[/{style}] environment · "
                      f"{_plain(environment.get('detail'))}")
        for alert in environment.get("alerts") or []:
            console.print(f"                  [dim]{_plain(alert.get('detail'))}[/dim]")
    if result.get("changed"):
        console.print(f"\n  Moved: {', '.join(result['changed'])}")
    console.print(f"\n  {out}")


def _print_watch(result: dict[str, Any], out: Path) -> None:
    if "components" in result:
        _print_system_watch(result, out)
        return
    state = result["state"]
    console.print(
        f"[bold {_STATE_STYLE[state]}]{state.upper()}[/bold {_STATE_STYLE[state]}]"
        f"  ·  {_plain(result.get('credentialId'))}"
    )
    if result.get("detail"):
        console.print(f"  {_plain(result['detail'])}")
    for name, check in result.get("checks", {}).items():
        style = _STATE_STYLE.get(check["status"], "white")
        console.print(
            f"  [{style}]{check['status']:<12}[/{style}] {name}: {_plain(check['detail'])}"
        )
    carried = result.get("carriedBy") or []
    if state == "current" and set(carried) <= {"latency", "egress", "behaviour"}:
        console.print(
            "\n[yellow]Carried by coarse statistics only:[/yellow] nothing identified the model "
            "build and the canaries could not run. A subtle change would pass unnoticed."
        )
    console.print(f"\n[dim]Result: {out}. Not a certificate: the sentinel signs nothing.[/dim]")


@app.command("version")
def version_command() -> None:
    """Tool and methodology version."""
    print_banner(console)



def _print_run_summary(report: dict[str, Any]) -> None:
    console.print()
    console.print(
        f"[bold]Run[/bold] {_plain(report.get('runId'))}  ·  "
        f"status: {_plain(report.get('status'))}"
    )
    model = report.get("model", {})
    configuration = report.get("configuration", {})
    console.print(
        f"  model    : {_plain(model.get('name'))}"
        + (f"  ({_plain(model.get('quantization'))})" if model.get("quantization") else "")
    )
    console.print(f"  backend  : {_plain(configuration.get('backend'))}")
    console.print(f"  hardware : {_plain(configuration.get('hardware'))}")
    console.print(f"  configHash: {configuration.get('configHash')}")
    if fingerprint := report.get("deploymentFingerprint"):
        console.print(f"  deployment: {fingerprint.get('hash')}")
        if unknown := fingerprint.get("unknown"):
            console.print(
                f"           : [yellow]not exposed by the deployment: "
                f"{_plain(', '.join(sorted(unknown)))}[/yellow]"
            )

    if cov := report.get("coverage"):
        evaluated = ", ".join(cov.get("blocksEvaluated") or []) or "none"
        if cov.get("complete"):
            # Every block ran, which is not the same as every dimension being
            # measured: saying only "complete" above a list of "not measured"
            # invites reading the report as more thorough than it is.
            console.print(f"  coverage : blocks {evaluated} (all five blocks ran)")
        else:
            missing = ", ".join(b["block"] for b in cov.get("blocksMissing", []))
            console.print(
                f"  coverage : blocks {evaluated} — [yellow]not evaluated: {missing}[/yellow]"
            )
        if unmeasured := _unmeasured_dimensions(report):
            console.print(
                f"           : [yellow]{len(unmeasured)} dimensions within those blocks were "
                f"not measured: {_plain(', '.join(unmeasured))}[/yellow]"
            )

    for result in report.get("suites", []):
        printer = _SUITE_PRINTERS.get(str(result.get("suite")))
        if result.get("status") == "completed" and printer:
            printer(result.get("metrics", {}))
        elif result.get("status") != "completed":
            err_console.print(
                f"[red]Suite '{_plain(result.get('suite'))}' {_plain(result.get('status'))}:"
                f"[/red] {_plain(result.get('error'))}"
            )

    if limitations := report.get("limitations"):
        console.print("\n[yellow]Known limitations of this run:[/yellow]")
        for limitation in limitations:
            console.print(f"  * {_plain(limitation)}")

    if notes := report.get("operatorNotes"):
        console.print(
            "\n[bold]Declared by the operator[/bold] [dim](not verified by OPAAS):[/dim]"
        )
        for entry in notes:
            console.print(f"  * {_plain(entry)}")


def _unmeasured_dimensions(report: dict[str, Any]) -> list[str]:
    """Dimensions a block declares it did not measure, gathered for the header."""
    names: list[str] = []
    for result in report.get("suites", []):
        metrics = result.get("metrics") or {}
        for entry in metrics.get("notImplemented", []):
            names.append(str(entry.get("dimension")))
        for key in ("faithfulness", "toxicity"):
            measure = metrics.get(key) or {}
            if measure and not measure.get("available"):
                names.append(str(measure.get("task", key)))
    return names


def _print_performance_table(metrics: dict[str, Any]) -> None:
    idle = metrics.get("idle", {})
    if idle:
        rss = idle.get("processRssPeakMb")
        ram = f"{_fmt(rss, 0)} MB (process {_plain(idle.get('processName'))})" if rss else (
            f"{_fmt(idle.get('ramMeanMb'), 0)} MB (system)"
        )
        console.print(
            f"\n  [bold]Idle[/bold] (model loaded, not generating): "
            f"VRAM {_fmt(idle.get('vramMeanMb'), 0)} MB · RAM {ram} · "
            f"{_fmt(idle.get('powerMeanW'))} W"
        )

    table = Table(title="Block A — Performance")
    table.add_column("prompt (words)", justify="right")
    table.add_column("conc.", justify="right")
    table.add_column("out tok/s", justify="right")
    table.add_column("prefill tok/s p50", justify="right")
    table.add_column("TTFT p50 / p95 / p99 (s)", justify="right")
    table.add_column("VRAM peak (MB)", justify="right")
    table.add_column("Δ VRAM (MB)", justify="right")
    table.add_column("Wh / 1M tok", justify="right")

    for scenario in metrics.get("scenarios", []):
        ttft = scenario.get("ttftS", {})
        table.add_row(
            str(scenario.get("promptWords")),
            str(scenario.get("concurrency")),
            _fmt(scenario.get("throughput", {}).get("outputTokensPerS")),
            _fmt(scenario.get("throughput", {}).get("prefillTokensPerS", {}).get("p50")),
            f"{_fmt(ttft.get('p50'), 3)} / {_fmt(ttft.get('p95'), 3)} / {_fmt(ttft.get('p99'), 3)}",
            _fmt(scenario.get("resources", {}).get("vramPeakMb"), 0),
            _fmt(scenario.get("overIdle", {}).get("vramMb"), 0),
            _fmt(scenario.get("energy", {}).get("whPerMillionTokens"), 1),
        )
    console.print(table)

    for observation in metrics.get("observations", []):
        console.print(
            f"\n  [red]▲ {_plain(observation['kind'])}[/red] "
            f"(prompt {observation['promptWords']}, concurrency {observation['concurrency']})\n"
            f"    {_plain(observation['detail'])}"
        )


def _print_capability_table(metrics: dict[str, Any]) -> None:
    dataset = metrics.get("dataset", {})
    table = Table(
        title=f"Block B — Capability  ·  dataset {_plain(describe_scored_dataset(metrics))} "
        f"{_plain(dataset.get('version'))} "
        f"({short_hash(str(dataset.get('datasetHash', '')), 12)})"
        f"  ·  style {_plain(metrics.get('answerStyle', '?'))}"
    )
    table.add_column("category", style="bold")
    table.add_column("accuracy", justify="right")
    table.add_column("95% CI", justify="right")
    table.add_column("n", justify="right")
    table.add_column("unparsed", justify="right")
    table.add_column("errors", justify="right")

    for task in metrics.get("tasks", []):
        accuracy = task.get("accuracy", {})
        point = accuracy.get("point")
        table.add_row(
            _plain(task.get("category")),
            "—" if point is None else f"{point:.1%}",
            "—"
            if accuracy.get("low") is None
            else f"{accuracy['low']:.1%} – {accuracy['high']:.1%}",
            str(accuracy.get("n", 0)),
            str(task.get("unparsed", 0)),
            str(task.get("errors", 0)),
        )
    console.print(table)

    for task in metrics.get("tasks", []):
        breakdown = task.get("constraintBreakdown") or []
        if not breakdown:
            continue
        failing = [c for c in breakdown if c["passed"] < c["total"]]
        if failing:
            # Failures over total, not passes: printing the passes under a
            # "failed" heading read as the exact opposite of the data.
            detail = ", ".join(
                f"{_plain(c['kind'])} {c['total'] - c['passed']}/{c['total']}" for c in failing
            )
            console.print(f"  [dim]constraints failed (failed/checked): {detail}[/dim]")

    _print_exclusions(dataset, _CAPABILITY_TYPES)


def _print_consistency_table(metrics: dict[str, Any]) -> None:
    determinism = metrics.get("determinism", {})
    robustness = metrics.get("robustness", {})
    context = metrics.get("context", {})
    rate = determinism.get("identicalRate", {}).get("point")

    table = Table(title="Block C — Consistency / Robustness")
    table.add_column("dimension", style="bold")
    table.add_column("result", justify="right")
    table.add_column("detail")

    divergence = determinism.get("firstDivergenceChar")
    distinct = determinism.get("distinctOutputs") or 0
    table.add_row(
        "determinism (serial)",
        "—" if rate is None else f"{rate:.0%}",
        f"{distinct} distinct output{'' if distinct == 1 else 's'} across "
        f"{determinism.get('repeats')} repetitions"
        + (f", diverging at character {divergence}" if divergence is not None else "")
        + (
            f" (share of the most repeated output; {determinism['warmupDiscarded']} warmup "
            "discarded)"
            if determinism.get("warmupDiscarded")
            else ""
        ),
    )
    under_load = determinism.get("underLoad") or {}
    if under_load.get("measured"):
        load_rate = under_load.get("identicalToSerialRate", {}).get("point")
        table.add_row(
            "determinism (under load)",
            "—" if load_rate is None else f"{load_rate:.0%}",
            f"{under_load.get('matches')}/{under_load.get('outputs')} rounds matched the serial "
            f"output, {under_load.get('concurrency')} simultaneous requests per round",
        )
    elif under_load:
        table.add_row(
            "determinism (under load)", "not measured", _plain(under_load.get("reason"))
        )
    handled = robustness.get("handledRate", {}).get("point")
    truncations = robustness.get("silentTruncations") or 0
    table.add_row(
        "robustness",
        "—" if handled is None else f"{handled:.0%}",
        f"{robustness.get('handled')}/{robustness.get('total')} adverse cases handled; "
        f"{truncations} silent truncation{'' if truncations == 1 else 's'}",
    )
    window = context.get("servedWindowTokens")
    table.add_row(
        "verified window",
        f"{context.get('largestVerifiedWords') or '—'} words",
        _context_detail(context),
    )
    table.add_row(
        "served window",
        f"{window} tokens" if window else "unknown",
        "pinned for this run, so the probe is reproducible"
        if window
        else "the engine does not expose it and it was not pinned: use --num-ctx "
        "(Ollama) or --max-model-len (vLLM)",
    )
    console.print(table)

    if broken := robustness.get("enginesBrokenAfterCase"):
        console.print(
            f"  [red]▲ the engine stopped responding after: {_plain(', '.join(broken))}[/red]"
        )

    evidence = context.get("truncationEvidence") or {}
    if context.get("contextLossDetected") and evidence.get("reason"):
        colour = "red" if evidence.get("verdict") == "confirmed" else "yellow"
        console.print(f"  [{colour}]▲ {_plain(evidence['reason'])}[/{colour}]")


def _context_detail(context: dict[str, Any]) -> str:
    """What the row says, matching the verdict instead of prejudging it."""
    if not context.get("contextLossDetected"):
        return "the whole prompt arrives"
    return {
        "confirmed": "the engine trimmed the prompt (silent truncation)",
        "ruled-out": "the prompt arrives whole; the model loses its beginning",
    }.get(str(context.get("enginePromptTruncation")), "beginning of the prompt lost, cause unknown")


#: Which item types each block scores. A composed dataset carries every task, and
#: block B used to list BBQ's exclusions as if they were its own.
_CAPABILITY_TYPES = ("multiple_choice", "instruction_following")
_SAFETY_TYPES = ("refusal", "grounded_qa", "bias_mc")


def _print_exclusions(dataset: dict[str, Any], types: tuple[str, ...]) -> None:
    for task in dataset.get("tasks", []):
        excluded = task.get("excluded") or {}
        if task.get("type") not in types or not excluded.get("count"):
            continue
        reasons = ", ".join(
            f"{_plain(reason)} {count}" for reason, count in sorted(excluded["byReason"].items())
        )
        # Three different numbers: what the source has, what could be
        # checked, and what this run evaluated after --limit. Collapsing
        # them into one ratio is how the exclusions got lost in the first
        # place.
        console.print(
            f"  [yellow]{_plain(task['name'])}: {task['items']} items evaluated of "
            f"{task['itemsImported']} usable; {excluded['count']} of "
            f"{task['itemsAvailable']} source items excluded ({reasons})[/yellow]"
        )


def _print_bias_table(metrics: dict[str, Any]) -> None:
    """BBQ per category, never pooled (D55)."""
    for task in metrics.get("tasks", []):
        categories = (task.get("subsets") or {}).get("categories")
        if task.get("type") != "bias_mc" or not categories:
            continue
        table = Table(title="Block D — Bias (BBQ)")
        table.add_column("category", style="bold")
        table.add_column("unknown when ambiguous", justify="right")
        table.add_column("accuracy when disambiguated", justify="right")
        table.add_column("stereotype-aligned (95% CI)", justify="right")
        table.add_column("verdict")

        def pct(entry: dict[str, Any]) -> str:
            return "—" if entry.get("point") is None else f"{entry['point']:.0%}"

        for name, figures in categories.items():
            aligned = figures["stereotypeAlignedRate"]
            interval = (
                f" ({aligned['low']:.0%}–{aligned['high']:.0%})"
                if aligned.get("low") is not None
                else ""
            )
            verdict = figures["verdict"]
            colour = {"stereotype-aligned": "red", "counter-stereotypical": "yellow"}.get(
                verdict, "white"
            )
            table.add_row(
                _plain(name.replace("_", " ")),
                pct(figures["ambiguousUnknownRate"]),
                pct(figures["disambiguatedAccuracy"]),
                pct(aligned) + interval,
                f"[{colour}]{verdict}[/{colour}]",
            )
        console.print(table)


def _print_safety_table(metrics: dict[str, Any]) -> None:
    table = Table(title="Block D — Behavior and safety")
    table.add_column("dimension", style="bold")
    table.add_column("rate", justify="right")
    table.add_column("95% CI", justify="right")
    table.add_column("n", justify="right")

    balance = metrics.get("safetyBalance", {})
    for label, key in (
        ("explicit refusal (harmful)", "correctRefusalRate"),
        ("  + dissuades without refusing", "nonComplianceRate"),
        ("over-refusal (benign)", "overRefusalRate"),
    ):
        entry = balance.get(key) or {}
        point = entry.get("point")
        table.add_row(
            label,
            "—" if point is None else f"{point:.1%}",
            "—"
            if entry.get("low") is None
            else f"{entry['low']:.1%} – {entry['high']:.1%}",
            str(entry.get("n", 0)),
        )

    for task in metrics.get("tasks", []):
        if task.get("type") != "grounded_qa":
            continue
        subsets = task.get("subsets") or {}
        # Reports written before D53 carry only the combined rate.
        rows = (
            [
                ("answers from the passage (answerable)",
                 subsets.get("answerable", {}).get("extractionRate", {})),
                ("hallucinates (unanswerable)",
                 subsets.get("unanswerable", {}).get("hallucinationRate", {})),
            ]
            if subsets
            else [("extracts from the passage", task.get("rate", {}))]
        )
        for label, rate in rows:
            point = rate.get("point")
            table.add_row(
                label,
                "—" if point is None else f"{point:.1%}",
                "—" if rate.get("low") is None else f"{rate['low']:.1%} – {rate['high']:.1%}",
                str(rate.get("n", 0)),
            )

    for measure, label, key in (
        (metrics.get("faithfulness", {}), "faithfulness (classifier)", "faithfulRate"),
        (metrics.get("toxicity", {}), "toxicity (classifier)", "aboveThresholdRate"),
        (
            metrics.get("adversarialToxicity", {}),
            "toxicity, provoked (RealToxicityPrompts)",
            "aboveThresholdRate",
        ),
    ):
        if not measure.get("available"):
            continue
        rate = measure.get(key, {})
        point = rate.get("point")
        table.add_row(
            f"{label} · threshold {measure.get('threshold')}",
            "—" if point is None else f"{point:.1%}",
            "—" if rate.get("low") is None else f"{rate['low']:.1%} – {rate['high']:.1%}",
            str(rate.get("n", 0)),
        )
    console.print(table)
    _print_bias_table(metrics)
    _print_exclusions(metrics.get("dataset") or {}, _SAFETY_TYPES)

    for name, entry in (metrics.get("classifiers") or {}).items():
        console.print(
            f"  [dim]classifier {_plain(name)}: {_plain(entry.get('name'))} "
            f"({entry.get('weightsHash')}) from {_plain(entry.get('path'))}[/dim]"
        )
    for measure in (metrics.get("faithfulness", {}), metrics.get("toxicity", {})):
        if measure and not measure.get("available"):
            console.print(
                f"  [yellow]not measured — {_plain(measure['task'])}:[/yellow] "
                f"{_plain(measure['reason'])}"
            )
    for entry in metrics.get("notImplemented", []):
        console.print(
            f"  [yellow]not measured — {_plain(entry['dimension'])}:[/yellow] "
            f"{_plain(entry['reason'])}"
        )


def _print_compliance_table(metrics: dict[str, Any]) -> None:
    summary = metrics.get("summary", {})
    declared, probed = summary.get("declared", {}), summary.get("probed", {})
    # "0/7 met" with all seven undeclared read as seven breaches. Unknown is not
    # failure: what is missing is the declaration, and that is what gets said.
    declared_total = declared.get("total", 0)
    unknown = declared.get("unknown", 0)
    declared_text = f"{declared.get('yes', 0)}/{declared_total} met"
    if unknown:
        declared_text += (
            f", {unknown} not declared"
            if unknown < declared_total
            else " — no declaration supplied"
        )
    probed_total = probed.get("total", 0)
    probed_text = f"{probed.get('pass', 0)}/{probed_total} pass"
    if probed.get("fail"):
        probed_text += f", {probed['fail']} fail"
    if probed.get("unknown"):
        probed_text += f", {probed['unknown']} inconclusive"
    console.print(
        f"\n  [bold]Block E — Compliance[/bold]  ·  declared by the operator: {declared_text}"
        f"  ·  verified by OPAAS: {probed_text}"
    )

    table = Table(title="Requirements")
    table.add_column("requirement", style="bold")
    table.add_column("origin")
    table.add_column("status")
    table.add_column("framework")
    for item in metrics.get("items", []):
        status = str(item.get("status"))
        colour = {
            "yes": "green", "pass": "green",
            "no": "red", "fail": "red", "unsupported": "red",
            "partial": "yellow", "unknown": "yellow",
        }.get(status, "white")
        table.add_row(
            _plain(item.get("title")),
            "verified" if item.get("verifiedByOpaasBench") else "declared",
            f"[{colour}]{status}[/{colour}]",
            _plain(", ".join(item.get("frameworks", []))),
        )
    console.print(table)


def _column_titles(runs: list[dict[str, Any]]) -> list[str]:
    """Short headers, but only if they distinguish. Two identical columns say nothing."""
    quantizations = [str(run.get("quantization") or "") for run in runs]
    if all(quantizations) and len(set(quantizations)) == len(runs):
        return quantizations
    return [str(run["label"]) for run in runs]


def _print_comparison(comparison: dict[str, Any]) -> None:
    console.print("\n[bold]Runs compared[/bold]")
    for run in comparison["runs"]:
        console.print(
            f"  * {_plain(run['label'])}  "
            f"({_plain(run['backend'])}, {_plain(run['hardware'])})"
        )
    titles = [_plain(title) for title in _column_titles(comparison["runs"])]

    performance = comparison.get("performance") or []
    if performance:
        table = Table(title="Block A — comparison")
        table.add_column("prompt", justify="right")
        table.add_column("conc.", justify="right")
        for title in titles:
            table.add_column(f"{title}\nout tok/s · VRAM", justify="right")
        for row in performance:
            cells = []
            for entry in row["runs"]:
                if not entry["measured"]:
                    cells.append("—")
                else:
                    cells.append(
                        f"{_fmt(entry['outputTokensPerS'])} · {_fmt(entry['vramPeakMb'], 0)}"
                    )
            table.add_row(str(row["promptWords"]), str(row["concurrency"]), *cells)
        console.print(table)

    capability = comparison.get("capability") or []
    if capability:
        table = Table(title="Block B — comparison")
        table.add_column("category", style="bold")
        for title in titles:
            table.add_column(title, justify="right")
        table.add_column("significant difference?")
        for row in capability:
            cells = []
            for entry in row["runs"]:
                accuracy = entry.get("accuracy")
                cells.append("—" if not accuracy else f"{accuracy['point']:.1%}")
            verdict = row["verdict"]
            mark = (
                "[green]yes[/green]"
                if verdict.get("significant")
                else "[yellow]no[/yellow]" if verdict.get("comparable") else "—"
            )
            table.add_row(_plain(row["category"]), *cells, mark)
        console.print(table)
        for row in capability:
            if row["verdict"].get("comparable"):
                console.print(
                    f"  [dim]{_plain(row['category'])}: "
                    f"{_plain(row['verdict']['detail'])}[/dim]"
                )


_SUITE_PRINTERS = {
    "performance": _print_performance_table,
    "capability": _print_capability_table,
    "consistency": _print_consistency_table,
    "safety": _print_safety_table,
    "compliance": _print_compliance_table,
}


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


# The modules beyond model certification, one command group each (D41):
# `opaas <module> <command>`. Imported here, at the end, because they register
# on `app` and share nothing else with the bench commands above.
from opaas.cli import audit as _audit_cli  # noqa: E402
from opaas.cli import card as _card_cli  # noqa: E402
from opaas.cli import discover as _discover_cli  # noqa: E402
from opaas.cli import evidence as _evidence_cli  # noqa: E402
from opaas.cli import inquest as _inquest_cli  # noqa: E402
from opaas.cli import root as _root_cli  # noqa: E402
from opaas.cli import sentinel as _sentinel_cli  # noqa: E402
from opaas.cli import smith as _smith_cli  # noqa: E402
from opaas.cli import swarm as _swarm_cli  # noqa: E402

app.add_typer(_discover_cli.app, name="discover")
app.add_typer(_smith_cli.app, name="smith")
app.add_typer(_swarm_cli.app, name="swarm")
app.add_typer(_sentinel_cli.app, name="sentinel")
app.add_typer(_inquest_cli.app, name="inquest")
app.add_typer(_card_cli.app, name="card")
app.add_typer(_evidence_cli.app, name="evidence")
app.add_typer(_audit_cli.app, name="audit")
# What the offline root signs (D65): top-level, beside keygen, sign and trust.
app.command("root-document")(_root_cli.root_document)
app.command("delegate")(_root_cli.delegate)
app.command("revoke")(_root_cli.revoke)


if __name__ == "__main__":
    app()
