<img src="docs/img/suricata.svg" width="150" alt="Meerkat in pixels">

# opaas-bench

**On-Prem AI Audit Suite** — reproducible evaluation and a verifiable digital credential for LLM models deployed **on-prem**, by [onpremIALabs](https://onpremialabs.com).

A run answers one concrete question: *this model, with this quantization, on this backend and on this hardware — how does it perform and how does it behave?*

The result is a report structured by category — **with no aggregate score and no tiers**, by design — which can then be signed as a W3C Verifiable Credential and published together with its hash. What could not be measured is declared in the report, never omitted and never reported as a zero.

The meerkat is the group's sentinel: it stands on its hind legs and keeps watch while the others eat. That is what this tool does with a deployment.

---

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Your first run](#your-first-run)
- [Execution flows](#execution-flows) — evaluate, certify, publish, compare, air-gapped, cloud baseline, classifiers
- [What is measured](#what-is-measured) — the five blocks
- [Datasets](#datasets)
- [`run` options](#run-options)
- [Command reference](#command-reference)
- [How verification works](#how-verification-works)
- [What is not measured](#what-is-not-measured)

---

## Requirements

- **Python 3.11 or later**
- **An inference server already running** with the model loaded. OPAAS-Bench evaluates a deployment; it does not deploy anything and never downloads weights.
- Supported engines: **vLLM**, **Ollama**, and **`openai`** — a generic OpenAI-compatible adapter that covers local NVIDIA NIM, `llama-server`, TGI and LM Studio with no new code.
- An NVIDIA GPU is not required, but without `nvidia-smi` there are no VRAM or power figures, and the report says so.

## Installation

Not published on PyPI yet, so it installs from the repository.

**With [uv](https://docs.astral.sh/uv/)** — leaves the `opaas-bench` command on your PATH, with no environment to activate:

```bash
uv tool install git+https://github.com/luxinopanyvino/opas-bench.git
```

**With pip**, in a virtual environment of your own:

```bash
git clone https://github.com/luxinopanyvino/opas-bench.git
```

```bash
cd opas-bench && python -m venv .venv
```

Activate it — on Windows (PowerShell):

```bash
.venv\Scripts\Activate.ps1
```

On Linux or macOS:

```bash
source .venv/bin/activate
```

And install:

```bash
pip install .
```

**Check that it worked.** With no arguments you get the cover page and a navigable menu, which reports what you have and what you are missing:

```bash
opaas-bench
```

**Optional — block D classifiers** (faithfulness and toxicity). They are a separate extra because torch weighs more than everything else combined. Without them the tool works the same and declares those dimensions unmeasured. Installing and pointing at the weights is a flow of its own: [Measure faithfulness and toxicity](#7-measure-faithfulness-and-toxicity).

## Your first run

Five minutes, against a model you already serve. This example uses Ollama; for vLLM, swap the backend and point `--base-url` at your server.

**1. Make sure the engine is serving the model.**

```bash
ollama run llama3.2:1b "hello"
```

**2. Run the five blocks.** The first execution downloads the evaluation datasets into `~/.opaas-bench/cache` and reuses them from then on:

```bash
opaas-bench run --model llama3.2:1b --backend ollama --suite all --output result.json
```

A full evaluation takes a while, so `run` prints the percentage and elapsed time as it goes, naming the block and what it is doing inside it (`[2/5] capability · reasoning · 34/60 items`). Redirected to a file or in CI there is no bar to redraw: the same information comes out as lines, every 15 seconds.

**3. Read the result.**

```bash
opaas-bench show result.json
```

You now have `result.json`: the full report, per category, with its confidence intervals, its findings about the deployment and its declared limitations. That file is the artifact everything else is built on — its SHA-256 is what a certificate certifies.

> **Pin the context window.** Without `--num-ctx`, Ollama serves whatever its own default is — usually far below what the model supports — and a long prompt gets trimmed with nothing warning you. The run stays valid, but it measures the deployment as configured, and says so. To measure the model's window instead, pass `--num-ctx 32768`. On vLLM the window is fixed at server start (`--max-model-len`) and is read from the server. Either way it lands in the report as `servedWindowTokens`.

## Execution flows

### 1. Evaluate a deployment

The common case: measure what you have and read the report.

```bash
opaas-bench run --model my-model --backend vllm --base-url http://gpu-box.lan:8000/v1 --suite all --output result.json
```

```bash
opaas-bench show result.json
```

Without `--suite`, only block A (performance) runs. Without `--output`, the report goes to `runs/<run-id>/report.json`.

### 2. Issue a certificate

Turns a report into a signed credential. The key pair is generated **once** and is your root of trust — see [key custody](docs/key-custody.md).

```bash
opaas-bench keygen --out keys
```

It asks for a passphrase at the keyboard and writes `keys/opaas-root-ed25519.pem` (private), `keys/opaas-root-ed25519.pub.pem` and `keys/did.json`.

```bash
opaas-bench sign --input result.json --key keys/opaas-root-ed25519.pem --out credential.json --log ledger.jsonl
```

```bash
opaas-bench verify --credential credential.json --report result.json --log ledger.jsonl
```

`verify` is the step your customer or auditor runs when they receive the certificate, and it works **without a network**. `--log` records the issuance in a hash chain and checks, on verification, that the credential is logged and has not changed since.

A run that does not cover all five blocks is certified as `evaluated-partial`, and `verify` warns about it even when the signature is valid.

### 3. Publish the certificate

`publish` generates a **static site** — public registry, one page per certificate, embeddable badge. No server, no database, no backend that could lie.

```bash
opaas-bench publish --report result.json --credential credential.json --did-document keys/did.json --out site
```

To review it before deploying it anywhere:

```bash
cd site && python -m http.server 8000
```

The certificate page verifies the signature **in the browser**, with the credential and the DID document embedded in the HTML: it does not ask onpremIALabs whether the certificate is good, because then verifying would mean trusting the issuer. It needs a secure context, which `localhost` and HTTPS both provide.

Deploying is your own step — copy `site/` to any hosting. Details in [web/README.md](web/README.md).

### 4. Compare configurations

FP16 and Q4 of the same model do not fit on one GPU at the same time, so each configuration is evaluated separately and the reports are cross-referenced:

```bash
opaas-bench compare --report fp16.json --report q8.json --report q4.json --out comparison.json
```

It warns when what you are comparing is not comparable (different hardware, another engine, another dataset, another answer style) and applies one rule above all: **a difference whose confidence intervals overlap is not a difference**.

### 5. Run without internet access

Nothing is downloaded during a run, so an air-gapped deployment is never broken mid-evaluation. Populate the cache on a connected machine:

```bash
opaas-bench fetch
```

Copy `~/.opaas-bench/cache` to the isolated machine and forbid the network explicitly — it then fails saying what is missing instead of trying:

```bash
opaas-bench run --model my-model --backend vllm --suite all --offline
```

### 6. Cloud baseline

A reference against a public endpoint measures the same model under optimal conditions, to know how much you lose by deploying locally:

```bash
opaas-bench run --model meta/llama-3.3-70b-instruct --backend openai --base-url https://integrate.api.nvidia.com/v1 --api-key $NVIDIA_API_KEY --suite capability --reference-type cloud-baseline --output baseline.json
```

A baseline **refuses** to run performance or compliance, **cannot be signed** as a certificate, and the comparator labels it and excludes it from the performance tables. Somebody else's shared endpoint describes no deployment of yours.

### 7. Measure faithfulness and toxicity

Two dimensions of block D that need a classifier. They are not equally easy to add: **toxicity is one flag away, faithfulness needs a dataset you probably do not have yet** — worth knowing before installing torch.

**Install the extra.** Roughly 2.5 GB with CUDA on Windows:

```bash
pip install "opaas-bench[classifiers] @ git+https://github.com/luxinopanyvino/opas-bench.git"
```

**Download the weights yourself.** Nothing is downloaded during a run, on purpose: an air-gapped deployment must not break mid-evaluation.

```bash
huggingface-cli download unitary/toxic-bert --local-dir /models/toxic-bert
```

```bash
huggingface-cli download MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli --local-dir /models/nli
```

No specific repository is pinned — the profiles describe families, and any model compatible with `AutoModelForSequenceClassification` works as long as the directory has a `config.json`:

| `--faithfulness-profile` / model | Expects |
|---|---|
| `nli` (default) | A DeBERTa-MNLI or similar. The passage is the premise, the response the hypothesis |
| `hhem` | Vectara's HHEM-2.1-Open. Needs `--allow-remote-code`: third-party code, run to measure safety |
| `--toxicity-model` | Detoxify / toxic-bert and compatible |

**Run it:**

```bash
opaas-bench run --model my-model --backend vllm --suite all --toxicity-model /models/toxic-bert --faithfulness-model /models/nli
```

**Faithfulness also needs a passage to score against.** It only reaches items of type `grounded_qa`, and `ifeval,xstest` provides none — with the classifier installed and no such items, the report still says *"There are no items with a passage and a response to score"*. Compose in a dataset of your own documents, which is the on-prem case that matters:

```bash
opaas-bench run --model my-model --backend vllm --suite all --dataset ifeval,xstest,/data/internal-documentation --faithfulness-model /models/nli
```

Toxicity has no such requirement: it scores every response block D generates.

**The calibration gate.** Before either classifier is used it is fed control cases whose answer is obvious. If it does not separate them the measurement is **not published** — a figure from an instrument that does not discriminate is worse than no figure. If it separates them but the threshold is off, the report says so and suggests the right one:

```
The classifier separates the classes (margin 0.014) but the threshold is set
wrong: it fails 1 of 8 cases in en. Try --classifier-threshold 0.9429.
```

That is also what catches the most common mistake: an NLI model whose label order does not match what the profile expects. It is the mapping that is wrong, not the model.

## What is measured

| Block | `--suite` | What it answers |
|---|---|---|
| A | `performance` | Throughput, latency, memory, concurrency and energy cost |
| B | `capability` | Per-category accuracy on a hashed local dataset, with 95% CI |
| C | `consistency` | Determinism, robustness to adverse prompts, real context window |
| D | `safety` | Correct refusal vs over-refusal, and hallucination with a passage |
| E | `compliance` | Audited checklist plus explainability and air-gapped probes |

Full detail, and what may **not** be claimed from each figure, in the [methodology](docs/methodology.md).

### A — Performance

A `prompt length × concurrency` sweep measuring throughput (aggregate and per request), TTFT and latency at p50/p95/p99, VRAM and RAM peak and at idle, degradation under concurrency, and estimated Wh per million tokens.

The report also translates the ratios into **findings** about the deployment:

```
▲ no-concurrency-scaling (prompt 128, concurrency 16)
  At 16 simultaneous requests, throughput only improves x1.062 while TTFT
  worsens x94.458. The engine is queueing, not serving in parallel.
```

### B — Capability

Accuracy per category with a Wilson 95% interval, never an aggregate across categories. Multiple choice records which extraction strategy was used, and counts unparseable answers separately from wrong ones: not knowing the answer and not respecting the format are different failures. Instruction following uses programmatically checkable constraints, broken down by which kind of instruction the model ignores.

### C — Consistency

Determinism with a fixed seed, serially — 100% there does not guarantee determinism under load, and the report always says so. Robustness against nine adverse prompts, where what is measured is that the engine does not break, not the quality of the answer. And the real window, with a canary that has to survive the prompt: when it is lost, the processed token count distinguishes an engine trimming the prompt from a model losing attention.

### D — Behaviour and safety

Correct refusal and over-refusal are measured with the same items and the same detector, because separately they mean nothing: a model that refuses everything scores 100% on the first and 100% on the second. Hallucination is checked by giving a passage and asking for something that is not in it.

Scoring is deterministic pattern matching, whose hash travels in the report. **No LLM judge is used**: if the score depends on which judge model and version, the certificate stops being reproducible.

With the optional classifiers, faithfulness and toxicity are added — local pinned weights, hashed like any other artifact of the run, and calibrated before use. Step by step in [Measure faithfulness and toxicity](#7-measure-faithfulness-and-toxicity).

### E — Compliance

Half audited checklist, half probes, under one rule: **what is declared and what is probed are never added together**.

```bash
opaas-bench compliance --out declaration.json
```

You fill in the declaration — traceability, license, provenance, AI Act risk classification, logging, air-gapped — each requirement with its evidence and its regulatory reference. A requirement declared as met **without evidence** is reported as `unsupported`, not as met. OPAAS-Bench probes three things itself: whether the engine exposes logprobs, whether it can expose reasoning, and whether it opens external connections during inference. If air-gapped is declared and the probe sees external traffic, the report states the contradiction.

## Datasets

**Real datasets are used by default** — `ifeval,xstest`, 960 items covering blocks B and D — downloaded on first use and reused from the cache. An unconfigured run measures something real.

```bash
opaas-bench datasets
```

Several sources compose into a **single dataset**, because a certificate is issued from one run, not one per block. Your own documents can be mixed in, which is what matters on-prem: whether the model makes things up about your internal documentation, not about Wikipedia.

```bash
opaas-bench run --model my-model --backend vllm --suite all --dataset ifeval,xstest,/data/internal-documentation
```

What is downloaded is verified against a pinned hash: if the source file changes, the run stops instead of certifying against something else.

For a quick check that the pipeline works there is `--dataset opaas-smoke`: 40 trivial items, flagged as smoke, with the report declaring that its results measure no capability and must not be certified.

Format, sample size and how to install other subsets: [docs/datasets.md](docs/datasets.md).

## `run` options

The ones you will actually reach for. Full list with `opaas-bench run --help`.

| Option | Default | Notes |
|---|---|---|
| `--model` | *required* | Model id exactly as the backend serves it |
| `--backend` | `vllm` | `vllm`, `ollama` or `openai` |
| `--suite` | `performance` | Repeatable, or `all` |
| `--base-url` | adapter's own | Your server endpoint |
| `--output` | `runs/<run-id>/report.json` | Report file |
| `--num-ctx` | server's default | Context window the engine must serve (Ollama) |
| `--dataset` | `ifeval,xstest` | Sources, local paths, or `opaas-smoke` |
| `--concurrency` | `1,4,16` | Block A concurrency levels |
| `--prompt-words` | `128,1024` | Prompt lengths to sweep |
| `--quantization` | — | Declared quantization, e.g. `GGUF Q4_K_M` |
| `--weights-path` | — | Local weights, to hash the artifact |
| `--offline` | off | Cached datasets only |
| `--declaration` | — | Block E declaration (JSON) |
| `--toxicity-model` | — | Local weights of the toxicity classifier |
| `--faithfulness-model` | — | Local weights of the faithfulness classifier |
| `--classifier-threshold` | profile's own | Threshold for both classifiers, in [0, 1] |

## Command reference

| Command | What it does |
|---|---|
| `opaas-bench` | Cover page and menu: what you have, what you are missing |
| `opaas-bench run` | Runs the suites and writes the report |
| `opaas-bench show <report>` | Summary of an existing report, with its hash |
| `opaas-bench compare` | Cross-references several reports |
| `opaas-bench keygen` | Generates the root Ed25519 pair (root of trust) |
| `opaas-bench sign` | Issues the signed W3C credential from a report |
| `opaas-bench verify` | Verifies a credential, offline |
| `opaas-bench publish` | Generates the static certificate site |
| `opaas-bench log-verify` | Checks the transparency log |
| `opaas-bench compliance` | Generates the block E declaration template |
| `opaas-bench datasets` | Available datasets and cache status |
| `opaas-bench fetch` | Downloads datasets into the cache |
| `opaas-bench import` | Converts a local IFEval or XSTest file |
| `opaas-bench backends` | Registered adapters and their limitations |
| `opaas-bench suites` | Evaluation blocks |
| `opaas-bench version` | Tool and methodology version |

## How verification works

The credential is signed with Ed25519 over canonical RFC 8785 JSON, using `DataIntegrityProof` with the `eddsa-jcs-2022` cryptosuite. Verifying means canonicalizing, SHA-256 and checking the signature — no JSON-LD processor, no remote contexts to resolve ([why](docs/decisions.md#d1--the-signature-uses-eddsa-jcs-2022-not-ed25519signature2020)).

The certified hash is the hash of **the report's bytes**, so checking that a report is the certified one is `sha256sum result.json`.

`verify` needs the issuer's public key up front — never taking it from the credential itself — and looks for it in this order: `--key` or `--did-document`, then anchors in `~/.opaas-bench/anchors/` (or `$OPAAS_ANCHOR_DIR`), then the anchors bundled with the tool. If it finds none, it fails saying what is missing.

The transparency log is **not** Certificate Transparency: there are no Merkle trees and no independent monitors. It is published by the issuer, so the chain only proves that the published log is internally consistent. Publishing it in a repository with history puts date-stamped copies in third-party hands, which is what gives it value.

## What is not measured

Stated here as plainly as the report states it:

- **Bias.** A classifier is not enough; which axes get measured, and with what prompts, is a decision still open.
- **Toxicity**, unless you install the optional classifiers and point at local weights.
- **Faithfulness and hallucination with a passage**, unless you do the above *and* the dataset carries `grounded_qa` items — the default sources provide none.
- **Model-generated code is not executed.** Without a sandbox, running it would be the tool's problem, not the model's.
- **Determinism under load.** What is measured is serial determinism; with continuous batching the output depends on which other requests yours is batched with.

A run that skips blocks is certified as `evaluated-partial`. The report's `limitations` field is where all of this lands — nothing is quietly reported as a zero.

## Architecture

```
src/opaas_bench/
├── adapters/      InferenceBackend + adapters (vllm, ollama, openai)
├── suites/        Evaluation blocks A–E
├── datasets/      Loading of hashed local datasets + importers
├── classifiers/   Pinned block D classifiers (optional dependency)
├── credential/    Ed25519 root of trust, VC credential, trust anchors
├── cli/           opaas-bench commands
├── telemetry/     GPU sampling (VRAM, power)
├── runner.py      Orchestration of a run
├── report.py      Structured report + hash
├── canonical.py   RFC 8785 JSON canonicalization (JCS)
├── scoring.py     Templates and answer extraction (block B)
├── constraints.py Instruction-following checkers
└── hashing.py     Model/config hashes and run ids
web/               Certificate page and public registry
```

The suites never talk to a specific engine, only to `InferenceBackend`. Supporting another engine means adding an adapter, without touching the suites.

## Documentation

- **[Methodology](docs/methodology.md)** — what is measured, how, and what cannot be claimed from each figure
- **[Certifying yourself without sending us your model](docs/self-evaluation.md)** — self-evaluation and what the signature guarantees
- [Block B datasets](docs/datasets.md) — format, how to install real subsets, sample size
- [Root key custody](docs/key-custody.md)
- [System design](docs/design.md) — credential, A–E parameters, backend architecture, end-to-end flow
- [Design decisions](docs/decisions.md) — where and why the implementation departs from the design

## Development

An editable install with the test tooling:

```bash
uv venv && uv pip install -e ".[dev]"
```

```bash
pytest
```

```bash
ruff check src tests
```

## License

[MIT](LICENSE) — © 2026 onpremIALabs.

The harness is free to use, modify and redistribute: that is what allows a third party to run their own evaluation and lets the methodology withstand scrutiny. What the license does not grant is the ability to **issue onpremIALabs certificates**, which depends on the Ed25519 root key and not on the code.
