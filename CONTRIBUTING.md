# Contributing to OPAAS

Contributions are welcome. This document is short because most of what matters
is already written down: the [design decisions](docs/decisions.md) explain why
the code is shaped the way it is, and the [methodology](docs/methodology.md)
states what may and may not be claimed from each figure.

## Before opening a pull request

```bash
uv sync --extra dev
```

```bash
pytest
```

```bash
ruff check src tests
```

```bash
uv lock --check
```

CI runs the same four on Python 3.10 through 3.13. The floor is 3.10 because
JetPack 6 ships it and edge devices are a supported target.

## The invariants

Some properties are the reason this project exists. **Do not break one to
improve the code around it.** The full list, with what each one costs, is in
[docs/decisions.md](docs/decisions.md); the ones contributors trip over most:

- **No aggregate score, no tiers.** Results are per category. A weighted mean
  across blocks, a total, or a letter grade would turn an audit into a ranking.
- **What could not be measured is declared**, never omitted and never reported
  as a zero. A zero means "measured, and it was zero".
- **No LLM judge anywhere in scoring.** A figure that depends on which judge
  model and version was used is not reproducible by a third party.
- **Suites talk only to `InferenceBackend`.** A new engine is a new adapter and
  touches nothing under `suites/`.
- **Everything signed or hashed goes through `canonical.py`** (RFC 8785).
- **The report is a public document.** Nothing new goes into it without
  checking it is publishable — secrets are stripped, not trusted to be absent.

If your change departs from one of these, that is a decision, not a detail: add
a `D<n>` entry to [docs/decisions.md](docs/decisions.md) saying what was decided,
why, and **what is lost**.

## Extending

The three seams that are open by design:

| adding | what you touch |
|---|---|
| An inference engine | `adapters/<engine>.py` + registration. Nothing in `suites/` |
| An evaluation block | Subclass `Suite`, register it. `--suite all` and `coverage()` pick it up |
| A classifier | An entry in `DEFAULT_CLASSIFIERS` and a profile in `PROFILES` |

New dataset *item types* are not yet extensible from outside: `SUPPORTED_ITEM_TYPES`
in `datasets/loader.py` is a closed set. Adding one is fine, it just is not a
plug-in point yet.

## Tests

- No test may reach the network, a GPU, or real model weights. `tests/conftest.py`
  provides deterministic backends, and autouse fixtures force the classifiers
  extra off and isolate the global registries.
- Tests are named as sentences describing the guarantee
  (`test_report_file_is_bare_and_self_hashing`), not after the function under test.
- When a guarantee needs a reason, give the test a docstring saying it.

## Commits

Descriptive sentences, imperative-adjacent, no `feat:`/`fix:` prefixes:

> *Stop reporting cached datasets as not downloaded*

Bodies are substantive: what was wrong, why it mattered, what changed, and which
design decision it implements or revises.

## What never goes in a commit

Keys, `runs/`, `site/`, `*.pem`, `*.key`. The Ed25519 root key is the entire
root of trust — see [docs/key-custody.md](docs/key-custody.md).
