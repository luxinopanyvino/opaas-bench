<img src="docs/img/suricata.svg" width="150" alt="Meerkat in pixels">

# opaas-bench

**On-Prem AI Audit Suite**

Reproducible evaluation and a verifiable digital credential for LLM models deployed **on-prem**, by [onpremIALabs](https://onpremialabs.com).

The meerkat is the group's sentinel: it stands up on its hind legs and keeps watch while the others eat. That is what this tool does with a deployment.

An OPAAS-Bench run answers one concrete question: *this model, with this quantization, on this backend and on this hardware — how does it perform and how does it behave?* The result is a report structured by category — **with no aggregate score and no tiers**, by design — which is then signed as a W3C Verifiable Credential and published with its hash.

> **Status.** All six phases of the plan are implemented: the five blocks (A–E), the signed credential, the offline verifier, web publication, the transparency log and the cloud baseline. The [methodology](docs/methodology.md) is published for scrutiny.
>
> What is missing **inside** each block is declared in the report itself, never omitted: the bundled dataset is a smoke set, model-generated code is not executed without a sandbox, and toxicity and bias are not measured. A run that does not cover all five blocks comes out as `evaluated-partial`, and `verify` warns about it even when the signature is valid.

---

## Installation

**Python 3.11 or later** is required. It is not published on PyPI yet, so it installs from the repository.

The shortest way, if you have [uv](https://docs.astral.sh/uv/): it leaves the `opaas-bench` command on your PATH, with no environment to activate.

```bash
uv tool install git+https://github.com/luxinopanyvino/opas-bench.git
```

With pip, in a virtual environment of your own:

```bash
git clone https://github.com/luxinopanyvino/opas-bench.git
```

```bash
cd opas-bench && python -m venv .venv
```

On Windows (PowerShell):

```bash
.venv\Scripts\Activate.ps1
```

On Linux or macOS:

```bash
source .venv/bin/activate
```

And then, inside the environment:

```bash
pip install .
```

Check that it worked. With no arguments, the cover page says what you have and what you are missing:

```bash
opaas-bench
```

**Block D classifiers** (faithfulness and toxicity). They are separate because torch weighs more than everything else combined; without them the tool works just the same and declares that it did not measure those dimensions:

```bash
pip install "opaas-bench[classifiers] @ git+https://github.com/luxinopanyvino/opas-bench.git"
```

**To work on the code**, an editable install with the test tooling:

```bash
uv venv && uv pip install -e ".[dev]"
```

## Usage

The full cycle: evaluate, certify, verify.

```bash
opaas-bench run --model llama3.2:1b --backend ollama --suite all --output result.json
```

A complete evaluation takes a long time, so `run` reports the percentage and the elapsed time as it goes: a bar that says which block it is on and what it is doing inside it (`[2/5] capability · reasoning · 34/60 items`). Redirected to a file or in CI there is no bar to redraw — the same information comes out as lines, every 15 seconds.

```bash
opaas-bench sign --input result.json --key keys/opaas-root-ed25519.pem --out credential.json
```

```bash
opaas-bench verify --credential credential.json --report result.json
```

The third step works **without a network**: it is the one a customer or an auditor runs when they receive your certificate. See [Verification](#verification) for where it gets the public key.

Available commands:

| Command | What it does |
|---|---|
| `opaas-bench run` | Runs the suites and writes the report |
| `opaas-bench sign` | Issues the signed W3C credential from a report |
| `opaas-bench verify` | Verifies a third-party credential, offline |
| `opaas-bench show <report>` | Summary of an already-generated report, with its hash |
| `opaas-bench backends` | Registered adapters and their limitations |
| `opaas-bench suites` | Implemented and planned blocks |
| `opaas-bench compare` | Cross-references several reports: quantizations, backends, configurations |
| `opaas-bench fetch` | Downloads datasets into the cache, to run later without a network |
| `opaas-bench import` | Converts a local IFEval or XSTest file |
| `opaas-bench datasets` | Available datasets and cache status |
| `opaas-bench compliance` | Generates the block E declaration template |
| `opaas-bench publish` | Generates the static certificate site |
| `opaas-bench log-verify` | Checks the transparency log |
| `opaas-bench keygen` | Generates the root Ed25519 pair (root of trust) |

Backends: `vllm`, `ollama` and `openai` (generic — it covers local NVIDIA NIM, `llama-server`, TGI and LM Studio with no new adapter).

Common `run` options:

```bash
opaas-bench run \
  --model my-model \
  --backend vllm \
  --base-url http://gpu-box.lan:8000/v1 \
  --concurrency 1,4,16 \
  --prompt-words 128,1024,4096 \
  --max-tokens 256 \
  --quantization "GGUF Q4_K_M" \
  --weights-path /models/my-model
```

## What it measures today (block A — Performance)

A `prompt length × concurrency` sweep, and for each scenario:

- **Throughput**, aggregate for generation and for prompt processing (tok/s), plus per-request decoding speed
- **TTFT** p50 / p95 / p99, measured on the streaming response
- **Latency** of the full request, p50 / p95 / p99
- **Memory**: VRAM and RAM, peak and **at idle** — idle is measured after warmup, with the model resident and not generating, and every scenario reports its delta over it. When the server process is local, its RSS is measured rather than the whole host's
- **Degradation under concurrency**: each level compared against the 1-request case
- **Estimated energy cost**: Wh per million tokens generated

On top of that, the report translates the ratios into **findings** about the deployment. A real example against Ollama with `qwen2.5:7b`:

```
▲ no-concurrency-scaling (prompt 128, concurrency 16)
  At 16 simultaneous requests, throughput only improves x1.062 while TTFT
  worsens x94.458. The engine is queueing, not serving in parallel.
```

What cannot be measured is reported as an explicit limitation in the report (`limitations`), never as a zero.

> A `Δ VRAM` near zero or slightly negative is what you expect on engines that preallocate the KV cache when loading the model: serving reserves no additional memory, it was already reserved. The figure that moves there is power.

## What it measures today (block B — Capability)

Accuracy **per category**, with a 95% confidence interval (Wilson), over a local, versioned and hashed dataset. There is no aggregate score across categories.

- **Multiple choice** (reasoning, science): the answer is extracted from the generated text and the strategy used is recorded, because a loose extraction makes the figure less reliable. Unparseable answers are counted separately from incorrect ones: not knowing the answer and not respecting the format are not the same thing.
- **Instruction following**: programmatically checkable constraints (IFEval style), with a breakdown of which kind of instruction the model ignores.

```bash
opaas-bench run --model my-model --backend vllm --suite capability --dataset /data/mmlu-pro-subset
```

**Real datasets are used by default** (`ifeval,xstest`), downloaded on first use and reused from the cache. Nothing needs configuring:

```bash
opaas-bench run --model my-model --backend vllm --suite all
```

For a quick check that the pipeline works there is `--dataset opaas-smoke`: 40 trivial items, flagged as smoke, with the report declaring that its results measure no capability and must not be certified.

**IFEval** (Apache-2.0) for block B and **XSTest** (CC-BY-4.0) for block D. What is downloaded is verified against a pinned hash: if the source file changes, the run stops instead of certifying against something else.

Separated by commas they compose into a single dataset — a certificate is issued from one run, not one per block. And you can mix in your own documents, which is what really matters on-prem: whether the model makes things up about your internal documentation, not about Wikipedia.

```bash
opaas-bench run --model my-model --backend vllm --suite all --dataset ifeval,xstest,/data/internal-documentation
```

With no internet access, populate the cache on a connected machine with `opaas-bench fetch`, copy `~/.opaas-bench/cache` and run with `--offline`. See [docs/datasets.md](docs/datasets.md) for the other sources and for the effect of `--answer-style`.

## What it measures today (block C — Consistency)

- **Determinism**: same seed and temperature, serially. 100% here does not guarantee determinism under load: with continuous batching the output changes depending on which other requests yours is grouped with, and the report always says so.
- **Robustness**: nine adverse prompts (empty, control characters, unclosed JSON, raw special tokens, bidirectional unicode…). What is measured is not the quality of the response but that the engine does not break. An explicit error counts as correct behavior; the engine being left unusable does not — and if that happens, the battery stops and the remaining cases are recorded as not run.
- **Real window**: a canary at the start of the prompt that has to be recovered at the end. A control with trivial context runs first: without it, a model unable to follow the instruction would be confused with one whose context gets truncated. And when the canary is lost, the processed token count distinguishes an engine trimming the prompt (serious, provable) from the model losing attention.

Comparison **across quantizations** does not live here: FP16 and Q4 do not fit on one GPU at the same time, so they are evaluated separately and cross-referenced with `opaas-bench compare`.

## What it measures today (block D — Behavior and safety)

- **Correct refusal and over-refusal**, with the same items and the same detector. Separately they mean nothing: a model that refuses everything scores 100% on the first and 100% on the second.
- **Hallucination with a passage**: a text is given and something that is **not in it** is asked. The correct answer is to say it is not stated; answering anyway is hallucinating, and that can be checked without a judge.

All of this is scored with deterministic patterns, whose hash travels in the report. **No LLM judge is used**: if the score depends on which judge model and which version, the certificate stops being reproducible.

With a **pinned classifier**, two more measurements are added — faithfulness to the passage, and toxicity. A small, local classifier does not break reproducibility the way an LLM judge would: its weights are just another artifact of the run, hashed like the dataset.

```bash
pip install 'opaas-bench[classifiers]'
```

Download the weights once and point at them (nothing is downloaded at run time, so an air-gapped deployment is not broken):

```bash
opaas-bench run --model my-model --backend vllm --suite safety --toxicity-model /models/toxic --faithfulness-model /models/nli
```

The report records the weights hash, the threshold and the score distribution, not just the rate: a threshold turns a continuous measurement into a yes/no, and an auditor has a right to see the margin.

**Before it is used, the classifier is calibrated.** It is fed control cases whose correct answer is obvious. If it does not separate them, the measurement is not published: a figure from an instrument that does not discriminate is worse than not measuring. If it separates them but the threshold is set wrong, the report suggests the right one:

```
The classifier separates the classes (margin 0.014) but the threshold is set
wrong: it fails 1 of 8 cases in en. Try --classifier-threshold 0.9429.
```

This matters more than it looks: classifiers that pass on near-verbatim paraphrase routinely fail on head-on contradiction, and whoever certifies needs to know that before issuing anything.

**Bias is still not measured**: a classifier is not enough, you have to decide which axes get measured and with what prompts, and that decision is still open.

## What it measures today (block E — Compliance)

Half audited checklist, half probes. The rule that governs the block: **what is declared and what is probed are never added together**.

```bash
opaas-bench compliance --out declaration.json
```

You fill in the declaration — traceability, license, provenance, AI Act risk classification, logging, air-gapped — each requirement with its evidence and its regulatory reference. A requirement declared as met **without evidence** shows up as `unsupported`, not as met.

OPAAS-Bench verifies three things on its own: whether the engine exposes logprobs, whether it can expose reasoning, and whether it opens external connections during inference. If air-gapped is declared and the probe sees external traffic, the report shouts the contradiction.

## Comparing configurations

```bash
opaas-bench compare --report fp16.json --report q8.json --report q4.json --out comparison.json
```

It warns when what you are comparing is not comparable (different hardware, another engine, another dataset, another answer style) and, above all, applies one rule: **a difference whose confidence intervals overlap is not a difference**.

## Verification

The credential is signed with Ed25519 over canonical RFC 8785 JSON, using `DataIntegrityProof` with the `eddsa-jcs-2022` cryptosuite. Verifying it means canonicalizing, SHA-256 and checking the signature: no JSON-LD processor and no resolving remote contexts ([why](docs/decisions.md#d1--the-signature-uses-eddsa-jcs-2022-not-ed25519signature2020)).

The certified hash is the hash of **the report's bytes**, so checking that a report is the certified one comes down to `sha256sum result.json`.

`verify` needs the issuer's public key up front — it never takes it from the credential itself — and looks for it in this order:

1. `--key <pub.pem>` or `--did-document <did.json>`
2. anchors installed in `~/.opaas-bench/anchors/` (or `$OPAAS_ANCHOR_DIR`)
3. anchors bundled with the tool

If it finds none, it fails saying what it is missing.

## Publication

```bash
opaas-bench publish --report result.json --credential credential.json --did-document keys/did.json --out site
```

It generates a **static site** — a public registry, one page per certificate, an embeddable badge — that copies to any hosting. The page verifies the signature **in the browser**, with the credential and the DID document embedded in the HTML: it does not ask onpremIALabs whether the certificate is good, because then verifying would mean trusting the issuer.

The badge cannot lie by omission: it says which blocks were evaluated and changes color if the evaluation is partial or the credential has expired.

`publish` refuses to pair a report with a credential that does not certify it. See [web/README.md](web/README.md).

## Transparency log

Every issuance is recorded in a hash chain. Altering, deleting or reordering a past entry is detectable.

```bash
opaas-bench sign --input result.json --key keys/opaas-root-ed25519.pem --out credential.json --log ledger.jsonl
```

```bash
opaas-bench verify --credential credential.json --report result.json --log ledger.jsonl
```

With `--log`, `verify` also checks that the credential is logged and has not changed since it was issued.

**This is not Certificate Transparency**: there are no Merkle trees and no independent monitors. The log is published by the issuer, so the chain only proves that the published log is internally consistent. Publishing it in a repository with history puts date-stamped copies in third-party hands, which is what gives it value.

## Cloud reference

A baseline against a public endpoint measures the quality of the same model under optimal conditions, to know how much you lose by deploying locally.

```bash
opaas-bench run --model meta/llama-3.3-70b-instruct --backend openai --base-url https://integrate.api.nvidia.com/v1 --api-key $NVIDIA_API_KEY --suite capability --reference-type cloud-baseline --output baseline.json
```

It is never mixed into block A: a baseline **refuses** to run performance or compliance, **cannot be signed** as a certificate, and the comparator labels it and excludes it from the performance tables.

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
web/               Certificate page and public registry (Phase 5)
```

The suites never talk to a specific engine: only to `InferenceBackend`. Adding Ollama, llama.cpp, transformers or NIM means adding an adapter, without touching the suites.

## Development

```bash
pytest
```

```bash
ruff check src tests
```

## Documentation

- **[Methodology](docs/methodology.md)** — what is measured, how, and what cannot be claimed from each figure
- **[Certifying yourself without sending us your model](docs/self-evaluation.md)** — self-evaluation and what the signature guarantees
- [System design](docs/design.md) — credential, A–E parameters, backend architecture, end-to-end flow
- [Implementation plan](docs/implementation-plan.md) — phases 0 to 6
- [Design decisions](docs/decisions.md) — where and why the implementation departs from the design
- [Block B datasets](docs/datasets.md) — format, how to install real subsets, sample size
- [Root key custody](docs/key-custody.md)

## License

[MIT](LICENSE) — © 2026 onpremIALabs.

The harness is free to use, modify and redistribute: that is what allows a third party to run their own evaluation and lets the methodology withstand scrutiny. What the license does not grant is the ability to **issue onpremIALabs certificates**, which depends on the Ed25519 root key and not on the code.
