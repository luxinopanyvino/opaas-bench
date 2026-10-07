# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this project is

**OPAAS** (On-Prem AI Audit Suite, by onpremIALabs) evaluates an LLM **deployment**
that is already running — it never deploys or downloads the model under evaluation — and
turns the result into a signed, third-party-verifiable digital certificate.

One run answers: *this model, with this quantization, on this backend, on this hardware —
how does it perform and how does it behave?*

The pipeline is:

```
run  → report.json (canonical JCS bytes)
sign → credential.json (W3C VC, Ed25519 over JCS) + --log JSONL (hash chain)
verify → offline signature + log inclusion check
publish → static site (browser-side verification, no backend)
```

Python 3.10+, CLI built on Typer + Rich, reports modelled with Pydantic v2, signatures with
`cryptography`. Runtime deps are deliberately few (`httpx`, `typer`, `pydantic`, `rich`,
`cryptography`, `psutil`, `segno` for the certificate's QR code); torch/transformers live behind the optional `classifiers` extra.

## Non-negotiable invariants

These are the reason the project exists. **Do not "improve" code by breaking one of them.**
Most are recorded, with what each one costs, in [`docs/decisions.md`](docs/decisions.md).

1. **No aggregate score, no tiers.** Results are per category only. Never add a
   "total score", a weighted mean across blocks, or a letter grade.
2. **What could not be measured is declared, never reported as zero and never omitted.**
   Missing measurements land in a suite's `limitations` (which bubble up into
   `report.limitations`) and in `metrics.notImplemented`. A zero means "measured, and it
   was zero".
3. **No LLM judge anywhere in scoring.** Block D uses deterministic pattern matching
   (`refusal.py`) whose pattern hash travels in the report. A score that depends on which
   judge model and version was used is not reproducible by a third party (D11).
4. **The certified hash is the hash of the report file's bytes.** `report.json` contains the
   report and nothing wrapping it, written as canonical JCS. Verifying is `sha256sum
   report.json`; `RunReport.content_hash()` and `hashing.file_digest()` match by
   construction and a test pins that (D2). Never add a wrapper key or write the report with
   `json.dump`.
5. **Everything signed or hashed goes through `canonical.py` (RFC 8785 / JCS).** Numbers
   outside the interoperably canonicalizable range raise rather than get signed (D5). Do not
   substitute `json.dumps(sort_keys=True)`.
6. **`verify` never takes the public key from the credential.** Resolution order is
   `--key`/`--did-document` → `~/.opaas/anchors/` (or `$OPAAS_ANCHOR_DIR`) → anchors
   bundled with the tool; if none, it fails saying what is missing (D4).
7. **External artifacts are pinned.** Datasets carry a SHA-256 verified at registration
   (`datasets/sources.py`); default classifiers carry a full commit revision, never a tag
   (`classifiers/registry.py`). A mismatch stops the run. Bumping either is a deliberate,
   reviewed change.
8. **A classifier is calibrated before it is believed.** `classifiers/calibration.py` feeds
   control cases with obvious answers; if the instrument does not separate them, the
   measurement is not published (D18).
9. **Declared and probed are never added together.** Block E keeps operator claims separate
   from what the tool verifies itself; a requirement declared met without evidence is
   reported as `unsupported` (D14).
10. **A cloud baseline cannot become a certificate.** `referenceType: "cloud-baseline"`
    refuses `performance` and `compliance`, is rejected by `build_credential`, and is
    excluded from performance tables in `compare` (D22).
11. **A difference whose confidence intervals overlap is not a difference** (`compare.py`).
12. **The report is a public document.** `runner._publishable_config` strips secrets
    (`api_key` → `***`) and non-serializable objects. Nothing new goes into the report
    without checking it is publishable.
13. **Suites talk only to `InferenceBackend`.** Never import or special-case a specific
    engine inside `suites/`.
14. **Prompts go through the chat template by default** (`prompt_mode="chat"`). Raw mode
    measures something else entirely — 88% vs 52% on IFEval for the same model (D25).

## Layout

```
src/opaas/
├── __init__.py         __version__ + OPAAS_SPEC_VERSION (methodology version)
├── canonical.py        RFC 8785 JCS serialization — everything signed passes through here
├── hashing.py          config/weights/file hashes, run ids (YYYYMMDD-NNN), slugify
├── fingerprint.py      Deployment fingerprint: what was serving, not how OPAAS ran (D34)
├── report.py           Pydantic report models, canonical write, content_hash
├── runner.py           RunRequest → execute() → RunOutcome; progress events
├── scoring.py          Block B prompt templates (direct/cot) + answer extraction
├── constraints.py      Instruction-following checkers (IFEval-style)
├── refusal.py          Pattern-based refusal/abstention detection + patterns_hash
├── stats.py            percentile/mean/summarize/wilson_interval
├── compare.py          Cross-run comparison with CI-overlap rule
├── declaration.py      Block E requirement catalogue + template
├── transparency.py     Hash-chain issuance log (explicitly NOT Certificate Transparency)
├── adapters/           InferenceBackend ABC + vllm, ollama, openai_compatible
├── suites/             Blocks A–E: performance, capability, consistency, safety, compliance
├── datasets/           loader (Dataset/Task/Item), cache, sources, importers/
│   └── builtin/opaas-smoke/   40 trivial items, flagged synthetic — never certifiable
├── classifiers/        Optional block D classifiers: base, registry, cache, calibration,
│                       transformers_backend
├── credential/         keys (Ed25519 + DID), vc (build/sign/verify), trust (anchors),
│                       anchors/ (deliberately empty until the production key exists)
├── telemetry/          gpu (nvidia-smi), system (psutil), network, sampler
├── web/                Static site + badge generation, printable certificate (certificate.py),
│                       assets/verify.js (browser verify)
└── cli/                main.py (all commands), menu.py, banner.py, sprite.py, pixelfont.py

docs/     design.md, methodology.md, decisions.md, datasets.md, key-custody.md,
          self-evaluation.md, implementation-plan.md, mitigation-plan.md
tests/    659 tests, one file per module, plus conftest.py with the fake backends
scripts/  sprite_from_png.py — regenerates cli/_sprite_data.py from docs/img/mascot.png
web/      README describing the published site layout (the generator lives in src/)
```

## How a run flows

1. `cli/main.py::run` parses flags, validates them, and **resolves the dataset before the
   model is loaded** — finding out the reference is wrong must not happen after 40 GB of
   VRAM has been taken.
2. It builds a `RunRequest` with `backend_config` and a `suite_options` dict keyed by suite
   name.
3. `runner.execute()` validates, allocates a run id, loads the backend, and runs each
   requested suite in order, wiring `Suite.set_progress_callback` into `RunEvent`s.
4. `Suite.run()` wraps `evaluate()` with timing and error capture: **a failing suite does
   not abort the run**, it is recorded with `status: "failed"` and the report status becomes
   `partial`.
5. Limitations bubble up, `coverage()` records which of blocks A–E were actually measured,
   and a run that does not cover all five is `evaluated-partial`.
6. The report is written as canonical bytes; `sign`/`verify`/`publish` operate on that file.

## Development workflow

```bash
uv sync --extra dev                       # exactly what uv.lock pins
uv sync --extra dev --extra classifiers   # add torch/transformers (large)
uv run pytest                             # test suite; must not touch the network
uv run ruff check src tests               # lint (E, F, I, UP, B, SIM; line-length 100)
uv lock --check                           # fails if uv.lock drifted from pyproject.toml
```

**Dependencies are locked on purpose.** `pyproject.toml` declares `>=` bounds; `uv.lock`
pins the exact resolution of all 72 packages including extras. Run `uv lock` after changing
a dependency. Plain `pip install .` ignores the lockfile — fine for trying the tool, not for
reproducing a run.

**CI runs on every push and pull request** (`.github/workflows/ci.yml`): the suite on
Python 3.10 through 3.13, `ruff check`, `uv lock --check`, and a package build checked
with `twine`. Run them yourself before committing anyway — the feedback is faster than
a round trip.

### Known test state

`uv run pytest` reports **659 passed** in ~4s. The suite is green: treat any failure as
yours.

### Test conventions

- `tests/conftest.py` provides `FakeBackend` (deterministic, no network, no GPU),
  `ScriptedBackend` (responses as a function of the prompt, can simulate prompt trimming),
  and `NoConcurrencyBackend` (in-process engine).
- Two **autouse** fixtures matter:
  - `_no_classifier_extra` forces `classifiers_available()` to False so no test ever loads
    real weights or downloads ~800 MB mid-`pytest`. Tests that exercise the default opt back
    in by patching it.
  - `_isolate_registries` snapshots and restores the global backend and suite registries.
- Tests are named as sentences describing the guarantee
  (`test_report_file_is_bare_and_self_hashing`), not after the function under test. Keep
  that style, and give a docstring when the guarantee needs a reason.
- No test may reach the network, a GPU, or real model weights. Use `tmp_path` and
  `monkeypatch` (`OPAAS_CACHE_DIR` redirects the dataset/classifier cache).

## Code conventions

- `from __future__ import annotations` at the top of every module; modern typing
  (`str | None`, `list[str]`, `dict[str, Any]`).
- `@dataclass(slots=True)` for internal value objects; `frozen=True` for registry entries.
  Pydantic only for the report, where camelCase aliases and JSON schema matter.
- Report field names are snake_case in Python and **camelCase in JSON**, via
  `alias_generator=to_camel` on `report._Base`. Always dump with `by_alias=True`.
- Registries are module-level dicts with `available_*()`, `get_*()` and `register_*()`
  helpers (`adapters/__init__.py`, `suites/__init__.py`). `__all__` is maintained and sorted.
- Errors are narrow, module-local subclasses of `ValueError`/`RuntimeError`
  (`BackendError`, `DatasetError`, `CacheError`, `CredentialError`, `ClassifierError`,
  `ScoringError`, `ComparisonError`, `TransparencyError`, `SiteError`,
  `CanonicalizationError`). Error messages tell the user what to do next, in full sentences.
- **Comments explain why, not what.** Module docstrings carry the reasoning behind the
  design; `#:` comments document fields inline. This density is the house style — match it.
  Prose (docstrings, comments, user-facing strings, docs) is in English.
- Optional dependencies are imported **inside the function that needs them**
  (`classifiers.load_classifier` imports transformers lazily), never at module scope.
- CLI options are declared as module-level `Annotated[...]` type aliases in
  `cli/main.py`, grouped just before the command that uses them and reused across
  commands; each command validates its inputs itself and exits with
  `typer.Exit(code=2)` for user error, `1` for a failed run or backend error.

## Extension recipes

**A new inference engine** — add `adapters/<engine>.py` with a subclass of
`InferenceBackend` (set the `name`, `supports_concurrency`, `supports_streaming` ClassVars;
implement `load`, `generate`, `get_resource_usage`, `get_backend_metadata`; override
`context_window()`, `server_pid()`, `model_digest()` and `chat_template_hash()` when the
engine can answer them). Register it in
`adapters/__init__.py`. Touch nothing in `suites/`. `context_window()` returning `None` means
*unknown*, never *unlimited*.

**A new evaluation block or sub-measurement** — subclass `Suite`, set `name`/`block`/
`description`, implement `evaluate(backend) -> (metrics, limitations)`, call `plan_steps()`
and `step()` so progress reports work, and register it in `suites/_REGISTRY`. `--suite all`
picks it up automatically, as does `coverage()`. Anything the block does not measure
goes into its limitations and, following `suites/safety.py`, into a
`metrics.notImplemented` list.

**A new dataset source** — add a `Source` to `KNOWN_SOURCES` with its real SHA-256, license
and block, and an importer under `datasets/importers/` that converts to the local format.
Importers download nothing and discard whole items rather than half-converting them (D15);
what was dropped, and why, is recorded in `Task.excluded`.

**A new default classifier** — add a `ClassifierSource` to `DEFAULT_CLASSIFIERS` with a full
commit SHA resolved by hand, the license, and minimal `allow_patterns`. Add the matching
profile in `classifiers/base.py::PROFILES` and calibration cases if the task is new.

**Cache layout** (rooted at `$OPAAS_CACHE_DIR` or `~/.opaas/cache`):
`sources/<source>/<file>` raw verified download, `datasets/<source>/` converted,
`classifiers/<task>/` pinned weights. `--offline` forbids every network fetch and fails
naming what is missing, rather than breaking mid-evaluation.

## Environment variables

| Variable | Effect |
|---|---|
| `OPAAS_CACHE_DIR` | Overrides `~/.opaas/cache` (datasets, sources, classifiers) |
| `OPAAS_ANCHOR_DIR` | Overrides `~/.opaas/anchors` for `verify` trust anchors |
| `OPAAS_KEY_PASSPHRASE` | Non-interactive passphrase for `keygen`/`sign` |

## Documentation duties

- **`docs/decisions.md` is a living log.** Any change that departs from `docs/design.md`, or
  settles a point it left open, gets a `D<n>` entry saying what was decided, why, and
  **what is lost**. Revising an earlier decision updates that entry's status rather than
  quietly contradicting it (see how D26 revises D17, and D20 revises D9/D15).
- `docs/methodology.md` states what may **not** be claimed from each figure; keep it in step
  with what the suites actually compute.
- `README.md` is organized around what a user actually runs. Update the command reference,
  the `run` options table, and the "What is not measured" section when behaviour changes.
- `src/opaas/cli/_sprite_data.py` is generated — regenerate with
  `python scripts/sprite_from_png.py docs/img/mascot.png` (needs pillow + numpy, dev extra)
  and never hand-edit it; `tests/test_sprite_matches_png.py` re-reads the PNG and will catch
  a manual touch-up.
- `.gitattributes` forces LF on `*.jsonl`, `*.json`, `*.py`, `*.md`, `*.toml`, `*.yml`
  because dataset hashes must be identical on every platform. Do not relax it.

## Git workflow

- The default branch is **`develop`**; work happens on short feature branches
  (`fix/…`, `feat/…`, `docs/…`) merged via pull request.
- Commit subjects are descriptive sentences in the imperative-adjacent style used here —
  *"Stop reporting cached datasets as not downloaded"*, *"Fix the report contradicting its
  own data"*. No `feat:`/`fix:` prefixes.
- Commit bodies are substantive: what was wrong, why it mattered, what changed, and which
  design decision (`D<n>`) it implements or revises. Bold lead-ins for each thread of a
  multi-part change are the established pattern.
- Never commit keys, `runs/`, `site/`, or anything under the ignored paths; `keys/`, `*.pem`
  and `*.key` are gitignored for a reason — the Ed25519 root key is the whole root of trust
  (see `docs/key-custody.md`).
