<img src="docs/img/mascot.svg" width="120" align="right" alt="Meerkat in pixels">

# OPAAS

[![CI](https://github.com/luxinopanyvino/opaas-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/luxinopanyvino/opaas-bench/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Methodology 1.0-draft](https://img.shields.io/badge/methodology-1.0--draft-8fae6a)](docs/methodology.md)

**LLM & SLM deployment audit** · quantization · inference engines · on-prem & edge
· reproducible evidence · no aggregate score · AI Act / ENS

**On-Prem AI Audit Suite** — measures an LLM *deployment*, not a model, and reports
what it found without inventing a score. By [onpremIALabs](https://onpremialabs.com).

```bash
uv tool install git+https://github.com/luxinopanyvino/opaas-bench.git
```

A run answers one concrete question: *this model, at this quantization, on this
engine, on this hardware — how does it perform and how does it behave?* Leaderboards
cannot answer it. They measure weights, and most of what goes wrong lives between the
weights and the server — an engine silently trimming prompts to a quarter of the
window you configured, a default that serves one request at a time, a chat template
that never gets applied.

```
│ prompt (words) │ conc. │ out tok/s │ TTFT p50 / p95 / p99 (s) │ VRAM peak │ Wh / 1M tok │
│            128 │     1 │      79.4 │  0.102 / 0.113 / 0.117   │      7671 │       571.8 │
│            128 │    16 │      82.4 │ 37.883 / 38.200 / 38.322 │      7860 │       602.0 │

▲ no-concurrency-scaling (prompt 128, concurrency 16)
  At 16 simultaneous requests, throughput only improves x1.038 while TTFT worsens
  x372.868. The engine is queueing, not serving in parallel.
```

*A real run: qwen2.5:7b, Q4_K_M, Ollama, RTX 5060 Ti. The model is fine — the
deployment serves one request at a time, and no model benchmark would ever say so.*

Three rules make the numbers worth keeping: results are **per category, with no
aggregate score and no tiers**; **what could not be measured is declared**, never
omitted and never reported as a zero; and **no LLM judge takes part in scoring**, so
a third party can recompute a figure instead of trusting it.

Nothing you evaluate leaves your network: your documents stay local and only metrics
and hashes are published, no external service takes part in scoring, and `--offline`
forbids the network outright. The one sovereignty claim the tool will not take your
word for is air-gapped operation — it probes your deployment and reports the
contradiction if it calls out.

## What it answers

| your question | command | the number that decides |
|---|---|---|
| Does it fit in my GPU? | `--suite performance` | `vramPeakMb` against idle |
| How many users before it hurts? | `--concurrency 1,4,16` | `ttftP99` |
| Does Q4 cost me accuracy? | `compare` | whether the 95% intervals overlap |
| Which engine for this workload? | same model, another `--backend` | throughput, trimming, logprobs |
| Did the upgrade break anything? | `--suite consistency` | verified window, determinism |
| Can I defend this in an audit? | `--suite compliance` | AI Act / ENS, declared vs probed |

## Who it is for

R&D groups, research labs, universities, startups and small teams running their own
inference — anyone who has to justify a model, an engine, a quantization or a GPU
purchase with something better than a vendor benchmark.

## Scope

**LLMs and SLMs served over HTTP**, on servers or on edge devices. Vision, detection
and other modalities are out of scope: every measurement here assumes tokens, prompts
and generated text. Agentic behaviour — tool calling, multi-turn state — is not
measured either; the concurrency sweep and the real context window are the closest
proxies.

The meerkat is the group's sentinel: it stands on its hind legs and keeps watch while
the others eat. That is what this tool does with a deployment.

---

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Your first run](#your-first-run)
- [Execution flows](#execution-flows) — evaluate, certify, publish, print, compare, air-gapped, cloud baseline, classifiers
- [What is measured](#what-is-measured) — the five blocks
- [Datasets](#datasets)
- [`run` options](#run-options)
- [Command reference](#command-reference)
- [How verification works](#how-verification-works)
- [What is not measured](#what-is-not-measured)

---

## Requirements

- **Python 3.10 or later** — the floor is 3.10 so it installs on edge devices; JetPack 6 ships 3.10
- **An inference server already running** with the model loaded. OPAAS evaluates a deployment; it does not deploy anything and never downloads the model under evaluation.
- Supported engines: **vLLM**, **Ollama**, and **`openai`** — a generic OpenAI-compatible adapter that covers local NVIDIA NIM, `llama-server`, TGI and LM Studio with no new code.
- An NVIDIA GPU is not required, but without `nvidia-smi` there are no VRAM or power figures, and the report says so.

## Installation

Not published on PyPI yet, so it installs from the repository.

**With [uv](https://docs.astral.sh/uv/)** — leaves the `opaas` command on your PATH, with no environment to activate:

```bash
uv tool install git+https://github.com/luxinopanyvino/opaas-bench.git
```

**With pip**, in a virtual environment of your own:

```bash
git clone https://github.com/luxinopanyvino/opaas-bench.git
```

```bash
cd opaas-bench && python -m venv .venv
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
opaas
```

**Optional — block D classifiers** (faithfulness and toxicity). A separate extra because torch weighs more than everything else combined. Installing it is enough: the pinned weights download once on first use and those two dimensions start being measured. Without it the tool works the same and declares them unmeasured. Details in [Measure faithfulness and toxicity](#7-measure-faithfulness-and-toxicity).

## Your first run

Five minutes, against a model you already serve. This example uses Ollama; for vLLM, swap the backend and point `--base-url` at your server.

**1. Make sure the engine is serving the model.**

```bash
ollama run llama3.2:1b "hello"
```

**2. Run the five blocks.** The first execution downloads the evaluation datasets into `~/.opaas/cache` and reuses them from then on:

```bash
opaas run --model llama3.2:1b --backend ollama --suite all --output result.json
```

A full evaluation takes a while, so `run` prints the percentage and elapsed time as it goes, naming the block and what it is doing inside it (`[2/5] capability · reasoning · 34/60 items`). Redirected to a file or in CI there is no bar to redraw: the same information comes out as lines, every 15 seconds.

**3. Read the result.**

```bash
opaas show result.json
```

You now have `result.json`: the full report, per category, with its confidence intervals, its findings about the deployment and its declared limitations. That file is the artifact everything else is built on — its SHA-256 is what a certificate certifies.

> **Pin the context window.** Without `--num-ctx`, Ollama serves whatever its own default is — usually far below what the model supports — and a long prompt gets trimmed with nothing warning you. The run stays valid, but it measures the deployment as configured, and says so. To measure the model's window instead, pass `--num-ctx 32768`. On vLLM the window is fixed at server start (`--max-model-len`) and is read from the server. Either way it lands in the report as `servedWindowTokens`.

## Execution flows

### 1. Evaluate a deployment

The common case: measure what you have and read the report.

```bash
opaas run --model my-model --backend vllm --base-url http://gpu-box.lan:8000/v1 --suite all --output result.json
```

```bash
opaas show result.json
```

Without `--suite`, only block A (performance) runs. Without `--output`, the report goes to `runs/<run-id>/report.json`.

### 1b. Make a run finish sooner

A full run is dominated by generation: blocks B and D score ~960 items, and on an engine serving one request at a time that is most of the wall clock. Measured on an RTX 5060 Ti with `qwen2.5:7b` on Ollama, of a 50-minute run: capability 51%, safety 37%, performance 11%, the rest under 1%.

**First, check the engine is actually serving in parallel.** Block A reports it: if throughput stays flat while TTFT grows, the engine is queueing, and no flag here will help — it is the deployment's own limit (vLLM `--max-num-seqs`, `OLLAMA_NUM_PARALLEL`). Declare what you configured and the report cross-checks it:

```bash
opaas run --model my-model --backend vllm --suite all --max-concurrency 8 --suite-concurrency 8
```

`--max-concurrency` is what you say the engine serves; `--suite-concurrency` is what the harness sends. Raising the second above what the engine honours only fills a queue.

**Lowering the generation caps is the other lever on the same 88%.** Blocks B and D stop at 400 and 320 tokens per item; most of the wall clock is spent producing them.

```bash
opaas run --model my-model --backend vllm --suite all --capability-max-tokens 256 --safety-max-tokens 256
```

It is not free either, and the report will not let it pass quietly: a cut answer fails constraints it might have met, and a refusal cut mid-sentence can stop matching the detector and be scored as compliance — the model blamed for a setting of yours. Every response that hits the cap is counted per task and declared as a limitation, so if you cut too deep the report says so instead of returning a worse figure that looks real.

**Trimming the dataset is the third lever, and the most expensive.** `--limit` cuts items per task, and the confidence interval pays for it — at 69% accuracy, n=510 gives a 8.0pp interval, n=100 gives 17.8pp, n=50 gives 24.7pp. Below a few hundred items you can no longer tell two configurations apart. The report declares the trim, so it stays honest; use it while iterating, not to certify.

**And run only what you need**: `--suite performance` while tuning the server, `--suite consistency` (seconds) while checking a context setting.

### 2. Issue a certificate

Turns a report into a signed credential. The key pair is generated **once** and is your root of trust — see [key custody](docs/key-custody.md).

```bash
opaas keygen --out keys
```

It asks for a passphrase at the keyboard and writes `keys/opaas-root-ed25519.pem` (private), `keys/opaas-root-ed25519.pub.pem` and `keys/did.json`.

```bash
opaas sign --input result.json --key keys/opaas-root-ed25519.pem --out credential.json --log ledger.jsonl
```

```bash
opaas verify --credential credential.json --report result.json --log ledger.jsonl
```

`verify` is the step your customer or auditor runs when they receive the certificate, and it works **without a network**. `--log` records the issuance in a hash chain and checks, on verification, that the credential is logged and has not changed since.

A run that does not cover all five blocks is certified as `evaluated-partial`, and `verify` warns about it even when the signature is valid.

A credential is valid for **90 days** by default (`--validity-days` changes it). After that the deployment needs re-certifying: a new driver, engine release or quantization changes the results even for the same model.

### 3. Publish the certificate

`publish` generates a **static site** — public registry, one page per certificate, embeddable badge. No server, no database, no backend that could lie.

```bash
opaas publish --report result.json --credential credential.json --did-document keys/did.json --out site
```

To review it before deploying it anywhere:

```bash
cd site && python -m http.server 8000
```

The certificate page verifies the signature **in the browser**, with the credential and the DID document embedded in the HTML: it does not ask onpremIALabs whether the certificate is good, because then verifying would mean trusting the issuer. It needs a secure context, which `localhost` and HTTPS both provide.

Deploying is your own step — copy `site/` to any hosting. Details in [web/README.md](web/README.md).

### 3b. Print the certificate

For the document you hand to an auditor or attach to a tender, `certificate` renders one self-contained HTML page laid out for A4 landscape. Open it in a browser and print to PDF.

```bash
opaas certificate --report result.json --credential credential.json --key keys/opaas-root-ed25519.pub.pem --url https://example.com/cert/<id>/index.html --out certificate.html
```

It shows the deployment, one line per block (a block that was not evaluated says so), the validity dates, every limitation the run declared, and the identifiers `verify` checks. The quantization says whose word it is, the engine's or yours (`--quantization`), and the deployment hash is spelled out: engine and version, model digest, GPU and driver, and which of those the deployment did not expose. `--url` is the published certificate page and becomes a **QR code**: paper is easy to edit, so the QR is how a reader gets from the sheet to the signature. There is no overall score or grade on it, because OPAAS has none.

It checks the signature first, with the same key resolution as `verify`, and writes nothing if the credential does not verify.

### 4. Compare configurations

FP16 and Q4 of the same model do not fit on one GPU at the same time, so each configuration is evaluated separately and the reports are cross-referenced:

```bash
opaas compare --report fp16.json --report q8.json --report q4.json --out comparison.json
```

It warns when what you are comparing is not comparable (different hardware, another engine, another dataset, another answer style) and applies one rule above all: **a difference whose confidence intervals overlap is not a difference**.

### 5. Run without internet access

Datasets and classifier weights download on first use and are cached; `--offline` forbids both and fails saying what is missing, rather than breaking an evaluation halfway through. Populate the cache on a connected machine — this fetches the datasets, plus the pinned classifiers if that extra is installed:

```bash
opaas fetch
```

Copy `~/.opaas/cache` to the isolated machine and forbid the network explicitly:

```bash
opaas run --model my-model --backend vllm --suite all --offline
```

### 6. Cloud baseline

A reference against a public endpoint measures the same model under optimal conditions, to know how much you lose by deploying locally:

```bash
opaas run --model meta/llama-3.3-70b-instruct --backend openai --base-url https://integrate.api.nvidia.com/v1 --api-key $NVIDIA_API_KEY --suite capability --reference-type cloud-baseline --output baseline.json
```

A baseline **refuses** to run performance or compliance, **cannot be signed** as a certificate, and the comparator labels it and excludes it from the performance tables. Somebody else's shared endpoint describes no deployment of yours.

### 7. Measure faithfulness and toxicity

Two dimensions of block D that need a classifier. **Install the extra and they measure themselves** — the weights download once into the cache, at a pinned revision, and are reused from then on. Roughly 2.5 GB with CUDA on Windows:

```bash
pip install "opaas[classifiers] @ git+https://github.com/luxinopanyvino/opaas-bench.git"
```

From there the ordinary command measures them, with no extra flags:

```bash
opaas run --model my-model --backend vllm --suite all
```

The pinned defaults, one per dimension:

| Dimension | Default weights | License |
|---|---|---|
| toxicity | `unitary/toxic-bert` | Apache-2.0 |
| faithfulness | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | MIT |

**Pinned to a commit, not to a tag**, exactly like datasets carry a pinned SHA-256: two runs of the identical command on different days must not silently use different weights. `opaas datasets` shows which are cached, and the report records the repo and revision that measured, so a reader never assumes weights you chose.

**Your own weights still win.** An explicit path is used as-is and downloads nothing:

```bash
opaas run --model my-model --backend vllm --suite all --toxicity-model /models/toxic-bert --faithfulness-model /models/nli
```

Any model compatible with `AutoModelForSequenceClassification` works, as long as the directory has a `config.json`:

| `--faithfulness-profile` | Expects |
|---|---|
| `nli` (default) | A DeBERTa-MNLI or similar. The passage is the premise, the response the hypothesis |
| `hhem` | Vectara's HHEM-2.1-Open. Needs `--allow-remote-code`: third-party code, run to measure safety. No default is pinned for it |

**Air-gapped still works.** `--offline` forbids the classifier download as firmly as the dataset one, and fails saying what to supply. Pre-populate on a connected machine and copy `~/.opaas/cache`:

```bash
opaas fetch
```

**Faithfulness also needs a passage to score against.** It only reaches items of type `grounded_qa`, and `ifeval,xstest` provides none — with the classifier ready and no such items, the report still says *"There are no items with a passage and a response to score"*. Compose in a dataset of your own documents, which is the on-prem case that matters:

```bash
opaas run --model my-model --backend vllm --suite all --dataset ifeval,xstest,/data/internal-documentation
```

Toxicity has no such requirement: it scores every response block D generates.

**The calibration gate.** Before either classifier is used it is fed control cases whose answer is obvious. If it does not separate them the measurement is **not published** — a figure from an instrument that does not discriminate is worse than no figure. If it separates them but the threshold is off, the report says so and suggests the right one:

```
The classifier separates the classes (margin 0.014) but the threshold is set
wrong: it fails 1 of 8 cases in en. Try --classifier-threshold 0.9429.
```

That is also what catches the most common mistake: an NLI model whose label order does not match what the profile expects. It is the mapping that is wrong, not the model.

### 8. Watch a certified deployment between certifications

A certificate can stop describing the deployment the day after it is issued: an engine update, a re-pulled model, a new driver. `watch` checks, on a schedule, whether the signed certificate still holds:

```bash
opaas watch --report result.json --credential credential.json --key keys/opaas-root-ed25519.pub.pem --log watch.jsonl
```

It is a one-shot command for cron, a systemd timer or Windows Task Scheduler. It never reads user traffic and signs nothing. It runs the cheapest check first and stops at the first change:

1. **Deployment fingerprint**, with no inference: engine version, model digest, confirmed weights, chat template, GPU driver.
2. **Twenty canary prompts** at temperature 0, compared by hash with the outputs signed at certification. Used only if the deployment was deterministic when certified.
3. **Coarse statistics** (serial latency, egress, a 40-item sample of blocks B and D), under the confidence-interval overlap rule. They run when the first two could not decide, or always with `--full`.

| Exit code | State | Meaning |
|---|---|---|
| `0` | `current` | The checks that could run match the certificate |
| `1` | `drifted` / `expired` | Something changed, or the certificate is past its date |
| `3` | `inconclusive` | It could not tell: endpoint down, nothing identifying exposed |
| `2` | — | The inputs are wrong, or the certificate does not verify |

The result goes to `watch.json`; `--log` appends it to a hash-chained history, the monitoring record a deployer keeps. It stays local: nothing is published. Pass `--weights-path` to re-hash weights on disk (slow for large models). Every `run` signs the baseline the sentinel needs (`sentinelBaseline`), at the cost of about 60 extra serial requests.

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
opaas compliance --out declaration.json
```

You fill in the declaration — traceability, license, provenance, AI Act risk classification, logging, air-gapped — each requirement with its evidence and its regulatory reference. A requirement declared as met **without evidence** is reported as `unsupported`, not as met. OPAAS probes three things itself: whether the engine exposes logprobs, whether it can expose reasoning, and whether it opens external connections during inference. If air-gapped is declared and the probe sees external traffic, the report states the contradiction.

## Datasets

**Real datasets are used by default** — `ifeval,xstest`, 960 items covering blocks B and D — downloaded on first use and reused from the cache. An unconfigured run measures something real.

```bash
opaas datasets
```

Several sources compose into a **single dataset**, because a certificate is issued from one run, not one per block. Your own documents can be mixed in, which is what matters on-prem: whether the model makes things up about your internal documentation, not about Wikipedia.

```bash
opaas run --model my-model --backend vllm --suite all --dataset ifeval,xstest,/data/internal-documentation
```

What is downloaded is verified against a pinned hash: if the source file changes, the run stops instead of certifying against something else.

For a quick check that the pipeline works there is `--dataset opaas-smoke`: 40 trivial items, flagged as smoke, with the report declaring that its results measure no capability and must not be certified.

Format, sample size and how to install other subsets: [docs/datasets.md](docs/datasets.md).

## `run` options

The ones you will actually reach for. Full list with `opaas run --help`.

| Option | Default | Notes |
|---|---|---|
| `--model` | *required* | Model id exactly as the backend serves it |
| `--backend` | `vllm` | `vllm`, `ollama` or `openai` |
| `--suite` | `performance` | Repeatable, or `all` |
| `--base-url` | adapter's own | Your server endpoint |
| `--output` | `runs/<run-id>/report.json` | Report file |
| `--num-ctx` | server's default | Context window the engine must serve (Ollama) |
| `--max-concurrency` | — | What the engine is configured to serve at once. Declared by you, cross-checked by block A |
| `--capability-max-tokens` | `400` | Token cap per block B instruction item. Cut answers are counted and declared |
| `--safety-max-tokens` | `320` | Token cap per block D item. Cut answers are counted and declared |
| `--suite-concurrency` | `4` | Parallel requests the harness sends while scoring blocks B and D |
| `--dataset` | `ifeval,xstest` | Sources, local paths, or `opaas-smoke` |
| `--concurrency` | `1,4,16` | Block A concurrency levels |
| `--prompt-words` | `128,1024` | Prompt lengths to sweep |
| `--quantization` | — | Declared quantization, e.g. `GGUF Q4_K_M` |
| `--weights-path` | — | Local weights (a directory or a GGUF file). Hashes them and the template/tokenizer files beside them; if the server runs on this host and is seen holding those files, they enter the deployment fingerprint |
| `--offline` | off | Cached datasets and classifiers only |
| `--declaration` | — | Block E declaration (JSON) |
| `--note` | — | Context you state about the run, e.g. `"shared workstation"`. Repeatable. Published as declared by the operator, never as a finding |
| `--toxicity-model` | pinned default | Your own toxicity weights, instead of the pinned default |
| `--faithfulness-model` | pinned default | Your own faithfulness weights, instead of the pinned default |
| `--classifier-threshold` | profile's own | Threshold for both classifiers, in [0, 1] |

## Command reference

| Command | What it does |
|---|---|
| `opaas` | Cover page and menu: what you have, what you are missing |
| `opaas run` | Runs the suites and writes the report |
| `opaas show <report>` | Summary of an existing report, with its hash |
| `opaas compare` | Cross-references several reports |
| `opaas keygen` | Generates the root Ed25519 pair (root of trust) |
| `opaas sign` | Issues the signed W3C credential from a report |
| `opaas verify` | Verifies a credential, offline |
| `opaas trust` | Lists pinned trust anchors, or pins one after showing its fingerprint |
| `opaas admit` | Checks a report against the public registry's admission rules |
| `opaas publish` | Generates the static certificate site |
| `opaas watch` | Checks between certifications that a certificate still describes the deployment (exit 0/1/3) |
| `opaas certificate` | Renders a printable certificate (A4, to PDF) with a QR code to the published page |
| `opaas log-verify` | Checks the transparency log |
| `opaas compliance` | Generates the block E declaration template |
| `opaas datasets` | Available datasets and cache status |
| `opaas fetch` | Downloads datasets into the cache |
| `opaas import` | Converts a local IFEval or XSTest file |
| `opaas backends` | Registered adapters and their limitations |
| `opaas suites` | Evaluation blocks |
| `opaas version` | Tool and methodology version |

## How verification works

The credential is signed with Ed25519 over canonical RFC 8785 JSON, using `DataIntegrityProof` with the `eddsa-jcs-2022` cryptosuite. Verifying means canonicalizing, SHA-256 and checking the signature — no JSON-LD processor, no remote contexts to resolve ([why](docs/decisions.md#d1--the-signature-uses-eddsa-jcs-2022-not-ed25519signature2020)).

The certified hash is the hash of **the report's bytes**, so checking that a report is the certified one is `sha256sum result.json`.

`verify` needs the issuer's public key up front — never taking it from the credential itself — and looks for it in this order: `--key` or `--did-document`, then anchors in `~/.opaas/anchors/` (or `$OPAAS_ANCHOR_DIR`), then the anchors bundled with the tool. If it finds none, it fails saying what is missing.

Pinning an anchor once removes that step for good:

```bash
opaas trust --add did.json
```

It shows the fingerprint and waits, because pinning is the moment everything else rests on: afterwards, anything that issuer signs verifies without further questions. Check that fingerprint against a channel other than the one the file came through.

The transparency log is **not** Certificate Transparency: there are no Merkle trees and no independent monitors. It is published by the issuer, so the chain only proves that the published log is internally consistent. Publishing it in a repository with history puts date-stamped copies in third-party hands, which is what gives it value.

## What is not measured

Stated here as plainly as the report states it:

- **Bias.** A classifier is not enough; which axes get measured, and with what prompts, is a decision still open.
- **Toxicity**, unless the classifiers extra is installed — then it measures itself.
- **Faithfulness**, unless the classifiers extra is installed *and* the dataset carries `grounded_qa` items; the default sources provide none.
- **Hallucination with a passage**, unless the dataset carries `grounded_qa` items. No classifier can substitute for the data.
- **Model-generated code is not executed.** Without a sandbox, running it would be the tool's problem, not the model's.
- **Determinism under load.** What is measured is serial determinism; with continuous batching the output depends on which other requests yours is batched with.

A run that skips blocks is certified as `evaluated-partial`. The report's `limitations` field is where all of this lands — nothing is quietly reported as a zero.

Context only you know — the machine was shared, the engine was restarted before block A — goes in with `--note`. It is signed with the report and printed on the certificate under **Declared by the operator**, apart from the limitations, because OPAAS did not check it.

## Architecture

```
src/opaas/
├── adapters/      InferenceBackend + adapters (vllm, ollama, openai)
├── suites/        Evaluation blocks A–E
├── datasets/      Loading of hashed local datasets + importers
├── classifiers/   Pinned block D classifiers (optional dependency)
├── credential/    Ed25519 root of trust, VC credential, trust anchors
├── cli/           opaas commands
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
- [The sentinel](docs/proposals/sentinel.md) — design of `opaas watch`, now implemented (D36)
- [Proposal: measuring bias](docs/proposals/bias.md) — under review; how block D would measure bias without a judge
- [Standards mapping](docs/standards-mapping.md) — what a certificate is evidence for under ISO/IEC 42001, the AI Act, the ENS and the NIST AI RMF, and what it is not
- [Admission policy](docs/admission-policy.md) — what a public comparison accepts, flags and refuses
- [Root key custody](docs/key-custody.md)
- [Remediation plan](docs/remediation-plan.md) — what is left, and what each item unblocks
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

CI runs both on Python 3.10 through 3.13, plus `uv lock --check` and a package build. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

**The dependencies are locked.** `pyproject.toml` declares what is needed (`>=`); `uv.lock` pins the exact resolved version of all 72 packages, extras included. A tool whose whole claim is that a certificate is reproducible should not be built on a stack that resolves differently depending on the day it was installed.

```bash
uv sync --extra dev --extra classifiers
```

That installs exactly what the lockfile pins. `uv lock` after changing a dependency in `pyproject.toml`; `uv lock --check` fails if the lockfile is out of date with it. Installing with plain `pip` ignores the lockfile and resolves fresh, which is fine for trying the tool and not for reproducing a run.

## License

[MIT](LICENSE) — © 2026 onpremIALabs.

The harness is free to use, modify and redistribute: that is what allows a third party to run their own evaluation and lets the methodology withstand scrutiny. What the license does not grant is the ability to **issue onpremIALabs certificates**, which depends on the Ed25519 root key and not on the code.
