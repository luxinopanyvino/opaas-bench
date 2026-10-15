<div align="center">

<img src="docs/img/mascot.svg" width="140" alt="OPAAS meerkat">

# OPAAS

### On-Prem AI Audit Suite

**Audit the AI systems you run yourself — models, agents and the infrastructure under them —<br>
and prove it with a certificate anyone can verify offline.**

[![CI](https://github.com/luxinopanyvino/opaas-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/luxinopanyvino/opaas-bench/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/luxinopanyvino/opaas-bench?color=1f4e3d)](https://github.com/luxinopanyvino/opaas-bench/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Methodology 1.0-draft](https://img.shields.io/badge/methodology-1.0--draft-8fae6a)](docs/methodology.md)

[Quick start](#quick-start) ·
[Audit modes](#what-opaas-audits) ·
[Methodology](#methodology) ·
[Certificates](#certificates) ·
[Use cases](#use-cases) ·
[Docs](#documentation) ·
[Changelog](CHANGELOG.md)

by **[onpremIA Labs](https://onpremialabs.com)**

</div>

---

## Why OPAAS

Leaderboards measure weights. What decides whether an AI system works, and whether you can
defend it in an audit, lives between the weights and the user: an engine that silently trims
prompts to a quarter of the window you configured, a default that serves one request at a
time, a chat template that never gets applied, an agent that deletes before it asks, a log
that cannot tell who did what.

OPAAS measures **the deployment you actually run** — model, quantization, engine, hardware,
agents, tools and records — and turns the result into a **signed W3C Verifiable
Credential** that a third party checks without trusting you, or us.

```
│ prompt (words) │ conc. │ out tok/s │ TTFT p50 / p95 / p99 (s) │ VRAM peak │ Wh / 1M tok │
│            128 │     1 │      79.4 │  0.102 / 0.113 / 0.117   │      7671 │       571.8 │
│            128 │    16 │      82.4 │ 37.883 / 38.200 / 38.322 │      7860 │       602.0 │

▲ no-concurrency-scaling (prompt 128, concurrency 16)
  At 16 simultaneous requests, throughput only improves x1.038 while TTFT worsens
  x372.868. The engine is queueing, not serving in parallel.
```

<sub>A real run: qwen2.5:7b, Q4_K_M, Ollama. The model is fine — the deployment serves one
request at a time, and no model benchmark would ever say so.</sub>

**Three rules make the numbers worth keeping:**

- **No aggregate score, no tiers.** Results are per category, with 95% intervals.
- **What could not be measured is declared** — never omitted, never reported as a zero.
- **No LLM judge anywhere in scoring**, so a third party recomputes a figure instead of trusting it.

**Nothing leaves your network.** Your documents stay local, only metrics and hashes are
published, and `--offline` forbids the network outright. The one sovereignty claim OPAAS will
not take on your word is air-gapped operation: it probes the deployment and reports the
contradiction if it calls out.

---

## What OPAAS audits

| Mode | Question it answers | Command | Output |
|---|---|---|---|
| **Model deployment** | How does this model, at this quantization, on this engine and hardware, perform and behave? | `opaas run` | Report, blocks A–E → `OpaasModelCertificate` |
| **Environment & configuration** | What is serving here, how is it exposed, and what cannot be audited about it? | `opaas discover scan` / `gaps` | Signed inventory, gap scan |
| **Agent behaviour** | Does the agent complete tasks, resist injections, ask before destroying, treat people alike? | `opaas smith run` | Behaviour against mock-tool ground truth |
| **Agent auditability** | Can what the agents did be reconstructed, checked and attributed afterwards? | `opaas sentinel` · `inquest` · `swarm` · `card` | The five *Auditable Agents* dimensions, Q1–Q6 |
| **Whole system** | Is this system — every model, agent, tool and record — fit and auditable? | `opaas audit draft` → `audit run` | System report → `OpaasSystemCertificate` |
| **Continuous monitoring** | Does the certificate still describe what is deployed? | `opaas watch` | Current / drifted / inconclusive, per component |

OPAAS evaluates LLMs and SLMs served over HTTP, on servers or edge devices, and the agents
built on them: **Ollama**, **vLLM** and any **OpenAI-compatible** engine (llama-server, TGI,
LM Studio, NVIDIA NIM); agents over **HTTP** or **MCP**, and A2A agents and agent frameworks
it identifies on the host. It never deploys or downloads the model under evaluation.

---

## Quick start

**Install** (Python 3.10+, not on PyPI yet):

```bash
uv tool install git+https://github.com/luxinopanyvino/opaas-bench.git
```

**Audit a model you already serve** — the five blocks, then the summary:

```bash
opaas run --model qwen2.5:7b-instruct --backend ollama --num-ctx 8192 --suite all --output report.json
```

```bash
opaas show report.json
```

**Certify it** — a key once, then sign and verify offline:

```bash
opaas keygen --out keys
```

```bash
opaas sign --input report.json --key keys/opaas-root-ed25519.pem --out credential.json
```

```bash
opaas verify --credential credential.json --report report.json --key keys/opaas-root-ed25519.pub.pem
```

**Audit a whole system** — let OPAAS find its components, then audit and certify it:

```bash
opaas audit draft -o system.json --records events.jsonl
```

```bash
opaas audit run system.json -o out/
```

```bash
opaas sign --input out/system-report.json --key keys/opaas-root-ed25519.pem --out out/credential.json
```

<details>
<summary><b>One command from a fresh clone: <code>start.sh</code></b></summary>

From a fresh clone to a verified, printable certificate with nothing prepared beforehand. It
needs bash (on Windows, Git Bash or WSL) and an inference server already serving the model:

```bash
OPAAS_MODEL=qwen2.5:7b-instruct ./start.sh
```

It installs `uv` if missing and OPAAS exactly as `uv.lock` pins it (classifiers included),
downloads every dataset and classifier once (each verified against its pinned hash),
evaluates blocks A–E with `--offline`, generates the signing key on first use, then signs,
verifies, renders the printable certificate and the static site. On PowerShell, set the
variables first: `$env:OPAAS_MODEL = "qwen2.5:7b-instruct"; & "C:\Program Files\Git\bin\bash.exe" ./start.sh`.

| Variable | Default | Effect |
|---|---|---|
| `OPAAS_MODEL` | `llama3.2:1b` | Model id exactly as the backend serves it |
| `OPAAS_BACKEND` | `ollama` | `vllm`, `ollama` or `openai` |
| `OPAAS_BASE_URL` | adapter's own | Server endpoint |
| `OPAAS_NUM_CTX` | `8192` on Ollama | Context window to serve. Without it Ollama silently drops the start of long prompts |
| `OPAAS_DATASET` | `ifeval,xstest,squad2,bbq,rtp` | Sources, or a local path to compose your own documents in |
| `OPAAS_CAPABILITY_MAX_TOKENS` | `2048` | Block B generation cap. At 1024, a real run cut 16 of 508 IFEval answers |
| `OPAAS_CLASSIFIERS` | `1` | `0` skips the classifiers and torch: toxicity and faithfulness are then declared unmeasured |
| `OPAAS_DECLARATION` | none | Block E compliance declaration (JSON), written with `opaas compliance` |
| `OPAAS_KEY_PASSPHRASE` | asked for | Passphrase of the signing key |

Any argument is passed on to `opaas run`. The first run downloads a few MB of datasets,
about 800 MB of classifiers and, for torch, several hundred MB to a few GB.

</details>

<details>
<summary><b>Other installation options</b></summary>

With pip, in a virtual environment of your own:

```bash
git clone https://github.com/luxinopanyvino/opaas-bench.git && cd opaas-bench && python -m venv .venv
```

Activate it (`.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` elsewhere), then:

```bash
pip install .
```

The block D classifiers (toxicity, faithfulness) are an optional extra because torch weighs
more than everything else combined:

```bash
pip install "opaas[classifiers] @ git+https://github.com/luxinopanyvino/opaas-bench.git"
```

`opaas` with no arguments shows the cover page and a menu that reports what you have and
what you are missing. An NVIDIA GPU is not required, but without `nvidia-smi` there are no
VRAM or power figures, and the report says so.

</details>

---

## Audit modes

### 1 · Model deployment

```bash
opaas run --model my-model --backend vllm --base-url http://gpu-box.lan:8000/v1 --suite all --output report.json
```

Runs the five blocks of the [methodology](#methodology) against the endpoint and writes one
report: per category, with confidence intervals, findings about the deployment and declared
limitations. Its SHA-256 is what a certificate certifies. Without `--suite`, only block A
runs.

> **Pin the context window.** Without `--num-ctx`, Ollama serves its own default — usually
> far below what the model supports — and long prompts are trimmed with nothing warning you.
> The report records the window it measured as `servedWindowTokens`.

<details>
<summary>Make a run finish sooner</summary>

A full run is dominated by generation. First check the engine actually serves in parallel —
block A reports it: flat throughput with growing TTFT means the engine is queueing (vLLM
`--max-num-seqs`, `OLLAMA_NUM_PARALLEL`). Declare what you configured and the report
cross-checks it:

```bash
opaas run --model my-model --backend vllm --suite all --max-concurrency 8 --suite-concurrency 8
```

Lowering the generation caps (`--capability-max-tokens`, `--safety-max-tokens`) is the
other lever; every response cut by a cap is counted and declared. Trimming the dataset with
`--limit` is the most expensive lever: the confidence interval pays for it. And run only what
you need: `--suite performance` while tuning the server, `--suite consistency` (seconds)
while checking a context setting.

</details>

<details>
<summary>Compare configurations, cloud baselines, air-gapped runs</summary>

FP16 and Q4 do not fit on one GPU at once, so each is evaluated separately and compared. A
difference whose confidence intervals overlap is not a difference:

```bash
opaas compare --report fp16.json --report q8.json --report q4.json --out comparison.json
```

A **cloud baseline** measures the same model on a public endpoint, to know what deploying
locally costs. It refuses performance and compliance, cannot be signed, and is excluded from
performance tables:

```bash
opaas run --model meta/llama-3.3-70b-instruct --backend openai --base-url https://integrate.api.nvidia.com/v1 --api-key $NVIDIA_API_KEY --suite capability --reference-type cloud-baseline --output baseline.json
```

**Air-gapped**: populate the cache on a connected machine with `opaas fetch`, copy
`~/.opaas/cache`, and forbid the network with `--offline`. A missing artifact fails before
the model is touched, naming what to supply.

</details>

### 2 · Environment & configuration

```bash
opaas discover scan --well-known -o inventory.json
```

```bash
opaas discover gaps -i inventory.json --log events.jsonl -o gaps.json
```

A read-only inventory of the host: engines and versions, ports and bind scope (loopback or
network), served models and their digests, GPUs and driver, MCP servers in the
configurations of known clients (Claude Desktop, Claude Code, Cursor, VS Code, Windsurf,
Continue), tool manifests, skills, containers by image digest, and **agents that identify
themselves** (an A2A agent card, Open WebUI, an agent framework by its OpenAPI title). It
records process names, never command lines, and names, never secrets. The gap scan applies
nine versioned rules (logging layer, log integrity, free-form shell or HTTP tools, unpinned
skills, approvals as events, anonymous engines, actor chains, image digests): a gap is a
missing precondition for auditing, never a predicted violation.

Every model report also records the **deployment fingerprint** — engine version, model
digest, chat template, GPU driver — and everything else the host serves.

### 3 · Agent behaviour

```bash
opaas smith run --agent mcp --endpoint http://localhost:9000/mcp --scenarios scenarios/ -n 3 -o smith.json
```

Fixed scenarios with **mock tools that record every call**, so the correct outcome is known
without a judge: task completed, forbidden tool attempted, injection resisted, schema-valid
calls, step budget, destructive action without confirmation, instability across repeats.
Adapters: `http`, `mcp` (OPAAS is the agent's MCP client and the MCP server of its tools) and
`bare-model` (any model in OPAAS's own tool loop). Two agent-level measurements come with it:

- **Bias** from counterfactual scenarios — the same task with one personal attribute changed,
  actions compared, a difference counted only when each value is stable across repeats.
- **Toxicity** of the agent's final answers, by the same calibrated classifier as block D.

21 reference scenarios ship with OPAAS; a result on them alone is flagged not certifiable.
`opaas audit template` writes skeletons for your own tools and leaves every expected answer
to you. The report carries answer digests, never their text, unless `--keep-finals`.

### 4 · Agent auditability

```bash
opaas sentinel observe -u http://127.0.0.1:11434 --log events.jsonl -p policies.json --key ops.pem
```

```bash
opaas inquest audit events.jsonl -p policies.json --key ops.pub.pem -o audit.json
```

The **sentinel** is a reverse proxy in front of the engine: every call becomes a
hash-chained `AuditEvent` sealed with an operational key, bodies kept as digests. Callers say
who they act for with `X-OPAAS-Actor-Chain: user:alice>agent:planner`. `--mode gate` (opt-in)
blocks what policies forbid and holds actions for a named human approver. **Inquest** audits
existing records — OPAAS logs, OpenTelemetry GenAI spans, JSONL — integrity first, then every
structural policy as **comply, violate or undecidable**, each undecidable naming the fields
that would settle it. **Swarm** compares declared multi-agent flows with observed ones,
per-edge latency and errors, and contained versus cascaded faults. **Card** answers the
Auditability Card's Q1–Q6 from that evidence.

```json
[{"id": "confirm-before-delete", "kind": "precedes", "enforce": "approve",
  "scope": {"field": "action", "eq": "tool_call"},
  "match": {"field": "target", "eq": "delete_file"},
  "before": {"field": "action", "eq": "approval"}}]
```

### 5 · Whole system

```bash
opaas audit draft -o system.json --records events.jsonl
```

```bash
opaas audit template system.json -o scenarios/
```

```bash
opaas audit run system.json -o out/ --member llm=report.json
```

The **system** is the audited subject: a purpose, the components that serve it and the flows
between them. `audit draft` **identifies the components itself** — every served model, the
agents that identify themselves, the MCP servers of known clients, and, from the records of
the running system, every agent that acted, the models and tools it actually called, and the
flows. `audit run` runs only what the manifest calls for (bench per model, smith per agent,
sentinel per proxy, inquest and swarm per record source), cites documents you already have
with `--member`, and writes a system report: members by hash, declared against observed
composition, a **components × dimensions table** where every cell is *measured*, *not
measured* or *not applicable* with its reason, Q1–Q6, block E for the system's purpose, and
your thresholds judged **comply, violate or undecidable**. An agent OPAAS cannot drive yet is
kept with a pending adapter — listed, never dropped.

### 6 · Continuous monitoring

```bash
opaas watch --report report.json --credential credential.json --key keys/opaas-root-ed25519.pub.pem --log watch.jsonl
```

A one-shot check for cron or a timer: the deployment fingerprint first (no inference), then
canary prompts compared by hash with those signed at certification, then coarse statistics
when those could not decide. On a system certificate it re-checks every component and names
the one that moved. Exit `0` current, `1` drifted or expired, `3` inconclusive, `2` bad
inputs. Nothing is published; `--log` keeps a hash-chained history.

---

## Methodology

**Principles.** Every figure is per category with a Wilson 95% interval; a difference whose
intervals overlap is not a difference. Scoring is deterministic — pattern matching whose hash
travels in the report, and small local classifiers pinned to a commit, hashed like any other
artifact and **calibrated on control cases before they are believed**. Datasets are pinned
by SHA-256. Prompts go through the chat template. Operator declarations are never added to
what OPAAS verifies. Every result is bound to the environment it was measured in. The full
methodology, and what may **not** be claimed from each figure: [docs/methodology.md](docs/methodology.md).

### Model blocks A–E

| Block | `--suite` | What it measures |
|---|---|---|
| **A** Performance | `performance` | Throughput, TTFT and latency p50/p95/p99, VRAM and RAM, concurrency scaling, Wh per million tokens, findings about the deployment |
| **B** Capability | `capability` | Accuracy per category (IFEval instruction following, multiple choice), unparsed answers apart from wrong ones |
| **C** Consistency | `consistency` | Determinism serially and **under concurrent load**, robustness to nine adverse prompts, the real context window with a canary |
| **D** Behaviour & safety | `safety` | Correct refusal vs over-refusal (XSTest), hallucination with a passage (SQuAD 2.0), faithfulness, toxicity and **toxicity under provocation** (RealToxicityPrompts), bias per social category (BBQ) |
| **E** Compliance | `compliance` | Operator declaration (license, provenance, AI Act risk class, logging, air-gapped) beside OPAAS's own probes (logprobs, reasoning, egress) |

<details>
<summary>Block details</summary>

**A — Performance.** A `prompt length × concurrency` sweep. Ratios become findings, such as
`no-concurrency-scaling` when the engine queues instead of serving in parallel.

**B — Capability.** Accuracy per category, never aggregated. Instruction following uses
programmatically checkable constraints, broken down by which instruction the model ignores.

**C — Consistency.** Determinism with a fixed seed, serially and with the same request sent
among `--load-concurrency` other users' requests. Robustness measures that the engine does not
break, not the answer's quality. A lost canary is attributed, from token counts, to the
engine trimming the prompt or the model losing attention.

**D — Behaviour and safety.** Refusal and over-refusal on the same items with the same
detector — apart, because a model that refuses everything scores 100% on both. Hallucination
asks for something the passage does not contain; answerable and unanswerable are reported
apart. Bias: per BBQ category, abstention when the context does not say, accuracy when it
does, and the share of unjustified picks that follow the stereotype, flagged only when that
interval lies wholly above 50%. Toxicity of ordinary responses and of continuations of
provoking openings, reported apart and per band; long responses scored in windows.

**E — Compliance.** A requirement declared met without evidence is reported `unsupported`. If
air-gapped is declared and the egress probe sees external traffic, the report states the
contradiction. Write the declaration with `opaas compliance --out declaration.json`.

</details>

### Agent auditability

The five dimensions of *Auditable Agents* (Nian et al.), with metrics a third party can
recompute:

| Dimension | Metrics | Card question |
|---|---|---|
| Action recoverability | ACR, RF | Q1 — what happened? |
| Lifecycle coverage | LPC, GB | Q2 — in what context? |
| Policy checkability | SPDR, ADL | Q3 — did it comply? |
| Responsibility attribution | AC, ACD | Q4 — who was responsible? |
| Evidence integrity | IS, VC | Q5 — can the evidence be trusted? |
| Degradation | audits of damaged records | Q6 — what is lost when logs are incomplete? |

Verdicts are three-valued (comply, violate, undecidable — never rounded). Policies are
structural predicates over recorded fields, never questions about what a text means. Ground
truth comes from instrumentation, never from a model. Evidence is digests by default.
Thresholds are yours: OPAAS never sets one.

---

## Certificates

| You run | You get | It shows |
|---|---|---|
| `opaas run` → `sign` | **`OpaasModelCertificate`** | One deployment: a line per block A–E, complete or partial |
| `opaas audit run` → `sign` | **`OpaasSystemCertificate`** | A system: purpose, components, the components × dimensions table, thresholds, and whether it is **certifiable** — with every reason when it is not |
| `opaas evidence sign` | **`OpaasEvidence`** | That one document (inventory, smith report, audit…) is unaltered — not a certificate of fitness |

**Always the same:** a W3C credential, Ed25519 over the SHA-256 of the report's bytes
(`eddsa-jcs-2022`, RFC 8785), verified **offline** with `opaas verify`, valid **90 days**,
with a printable A4 certificate and QR code (`opaas certificate`), a static site with
in-browser verification and badge (`opaas publish`), an optional hash-chained issuance log,
every declared limitation, and **never an overall score**. A system is certifiable only when
every member was measured in one environment, every agent ran your own scenarios, no
component is a cloud baseline, no bench report is partial and no step failed.

The certified hash is the hash of the report's bytes, so checking that a report is the
certified one is `sha256sum report.json`. The issuance log is **not** Certificate
Transparency: no Merkle trees, no independent monitors; it proves only that the published log
is internally consistent, which is why publishing it in a repository with history matters.
The certificate page verifies the signature in the browser, with the credential and the DID
document embedded, so it never asks the issuer whether a certificate is good.

`verify` never takes the issuer's key from the credential: `--key` or `--did-document`, then
pinned anchors (`opaas trust --add did.json`), then those bundled with the tool. By default a
key signs under its own name (`did:key`); to sign as your organisation, generate it with
`--did did:web:yourdomain.com` and publish `did.json` there. The root key is the whole root of
trust: see [key custody](docs/key-custody.md).

---

## Use cases

- **Choosing a quantization, an engine or a GPU.** Evaluate each configuration and `compare`:
  only differences whose intervals do not overlap count.
- **Capacity planning.** Block A's concurrency sweep says how many simultaneous users the
  deployment serves before latency hurts, and whether the engine queues.
- **Regulatory evidence (AI Act, ENS, ISO/IEC 42001, NIST AI RMF).** Block E separates what
  you declare from what is verified, cited to the clause; the system certificate binds it to
  the system's purpose. See the [standards mapping](docs/standards-mapping.md).
- **Accepting an agent before production.** Your own scenarios with smith: injections,
  destructive actions without confirmation, permission bait, counterfactual bias.
- **Auditing a multi-agent system.** Sentinel records, inquest verdicts and swarm attribution
  answer who did what, under which policy, and whether the record can be trusted.
- **Hallucination on your own documents.** Compose a dataset of your passages in: what
  matters on-prem is whether the model makes things up about your documentation.
- **Checking a vendor's claims.** A signed report anyone can recompute, instead of a
  benchmark nobody can reproduce.
- **After every upgrade.** `opaas watch` says whether a new driver, engine or re-pulled model
  changed what was certified, and which component moved.
- **Sovereign and air-gapped deployments.** Everything runs locally, `--offline` forbids the
  network, and the egress probe checks the air-gapped claim.

---

## Datasets

Real datasets by default, downloaded on first use, verified against a pinned SHA-256 and
cached in `~/.opaas/cache`:

| Source | Block | What it adds | License |
|---|---|---|---|
| `ifeval` | B | Instruction following, 510 programmatically checkable items | Apache-2.0 |
| `xstest` | D | 250 benign prompts that look dangerous + 200 harmful ones | CC-BY-4.0 |
| `squad2` | D | Hallucination with a passage, 200 questions, half unanswerable | CC-BY-SA-4.0 |
| `bbq` | D | Social bias, 1,800 questions across 9 categories | CC-BY-4.0 |
| `rtp` | D | Toxicity under provocation, 200 openings of the challenging subset | Apache-2.0 |

The default is `ifeval,xstest`; `start.sh` uses all five. Sources compose into one dataset —
a certificate is issued from one run — and your own documents can be mixed in
(`--dataset ifeval,xstest,/data/internal-docs`). `--dataset opaas-smoke` validates the
pipeline and is flagged as measuring nothing. Format and subsets: [docs/datasets.md](docs/datasets.md).

---

## Reference

<details>
<summary><b><code>run</code> options</b></summary>

The ones you will reach for. Full list with `opaas run --help`.

| Option | Default | Notes |
|---|---|---|
| `--model` | *required* | Model id exactly as the backend serves it |
| `--backend` | `vllm` | `vllm`, `ollama` or `openai` |
| `--suite` | `performance` | Repeatable, or `all` |
| `--base-url` | adapter's own | Your server endpoint |
| `--output` | `runs/<run-id>/report.json` | Report file |
| `--num-ctx` | server's default | Context window the engine must serve (Ollama) |
| `--max-concurrency` | — | What the engine is configured to serve at once. Declared by you, cross-checked by block A |
| `--capability-max-tokens` | dataset's (IFEval: 700), else `400` | Block B cap; when set, it wins over the dataset's. Cut answers are counted and declared |
| `--safety-max-tokens` | dataset's (XSTest: 320), else `320` | Block D cap; when set, it wins over the dataset's |
| `--suite-concurrency` | `4` | Parallel requests the harness sends while scoring blocks B and D |
| `--load-concurrency` | `4` | Simultaneous requests in block C's determinism-under-load check; `1` skips it and declares it unmeasured |
| `--dataset` | `ifeval,xstest` | Sources (`ifeval`, `xstest`, `squad2`, `bbq`, `rtp`), local paths, or `opaas-smoke` |
| `--concurrency` | `1,4,16` | Block A concurrency levels |
| `--prompt-words` | `128,1024` | Prompt lengths to sweep |
| `--quantization` | — | Declared quantization, e.g. `GGUF Q4_K_M` |
| `--weights-path` | — | Local weights; hashed, and part of the fingerprint when the server is seen holding them |
| `--offline` | off | Cached datasets and classifiers only |
| `--declaration` | — | Block E declaration (JSON) |
| `--note` | — | Context you state about the run. Repeatable. Published as declared by the operator, never as a finding |
| `--toxicity-model` | pinned default | Your own toxicity weights |
| `--faithfulness-model` | pinned default | Your own faithfulness weights |
| `--classifier-threshold` | profile's own | Threshold for both classifiers, in [0, 1] |

</details>

<details>
<summary><b>Command reference</b></summary>

| Command | What it does |
|---|---|
| `opaas` | Cover page and menu: what you have, what you are missing |
| `opaas run` | Runs blocks A–E and writes the report |
| `opaas show <report>` | Summary of an existing report, with its hash |
| `opaas compare` | Cross-references several reports under the interval-overlap rule |
| `opaas keygen` | Generates the root Ed25519 pair (root of trust) |
| `opaas sign` | Issues the signed credential from a model or system report |
| `opaas verify` | Verifies a credential, offline |
| `opaas trust` | Lists pinned trust anchors, or pins one after showing its fingerprint |
| `opaas certificate` | Printable certificate (A4, to PDF) with a QR code to the published page |
| `opaas publish` | Static certificate site with in-browser verification and badges |
| `opaas watch` | Checks a certificate still describes the deployment, or every component of a system (exit 0/1/3) |
| `opaas log-verify` | Checks the transparency log |
| `opaas admit` | Checks a report against a public registry's admission rules |
| `opaas compliance` | Writes the block E declaration template |
| `opaas datasets` / `fetch` / `import` | Dataset and classifier cache: status, download, convert |
| `opaas backends` / `suites` / `version` | Adapters, blocks, tool and methodology version |
| `opaas discover scan` / `gaps` | Inventory of engines, agents and agent stack; auditability gap scan |
| `opaas smith scenarios` / `run` | Validate scenarios; run them against an agent |
| `opaas sentinel observe` | Recording (and opt-in gating) reverse proxy |
| `opaas sentinel approvals` / `approve` / `deny` | The gate's approval queue |
| `opaas sentinel inventory` / `overhead` | Environment watch; proxy overhead benchmark |
| `opaas inquest audit` | Post-hoc audit with three-valued verdicts |
| `opaas swarm analyze` / `attribute` | Flow topology, per-edge figures, faults; attribution subgraph |
| `opaas card build` / `manifest` | Auditability Card; deployment manifest |
| `opaas evidence sign` / `verify` | Sign and verify any evidence document |
| `opaas audit draft` | Draft a system manifest: models, agents, tools and flows the host and records show |
| `opaas audit template` | Scenario templates for the manifest's tools; you write the answers |
| `opaas audit run` | Audit a system from its manifest and write the system report |
| `opaas audit validate` / `extract` | Check a system manifest; read any document into measurements |

</details>

<details>
<summary><b>Classifiers: faithfulness and toxicity</b></summary>

Install the `classifiers` extra and these dimensions measure themselves: the pinned weights
download once, at a commit revision, and are reused.

| Dimension | Default weights | License |
|---|---|---|
| toxicity | `unitary/toxic-bert` | Apache-2.0 |
| faithfulness | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | MIT |

Your own weights win (`--toxicity-model`, `--faithfulness-model`); any
`AutoModelForSequenceClassification` with a `config.json` works. Before either classifier is
used it is fed control cases with obvious answers: if it does not separate them, the
measurement is **not published**; if the threshold is off, the report suggests the right one.
Faithfulness needs passages (`squad2` or your documents).

</details>

---

## What is not measured

Stated as plainly as the report states it:

- **Bias**, unless the dataset includes `bbq` — and then it is choosing between two people,
  in English, as stereotypes are understood in the United States. For agents, only on the
  counterfactual scenarios run.
- **Toxicity**, unless the classifiers extra is installed. Toxicity under provocation also
  needs `rtp`, and is not comparable with the paper's 25-sample figures.
- **Faithfulness and hallucination with a passage**, unless the dataset carries passages.
  Scoring is literal: a correct paraphrase counts as an omission.
- **Determinism under every load** — only at the `--load-concurrency` level and companions run.
- **Model-generated code is not executed.**
- **Whether an agent is "safe".** Behaviour in the scenarios run and how auditable the
  deployment is; semantic policies are out of scope.
- **ACR on existing records** — only smith, which has ground truth, measures it.
- **The behaviour of an agent no adapter reaches** — it is listed with a pending adapter.
- **General infrastructure security** — OPAAS is not a pentest or an OS hardening audit; it
  looks at what touches the AI system, such as an engine exposed without credentials.

A run that skips blocks is certified as `evaluated-partial`; every gap lands in the report's
`limitations`, and nothing is quietly reported as a zero. Context only you know goes in with
`--note`, printed as declared by the operator.

---

## Architecture

```
src/opaas/
├── core/        evidence and trust: JCS, hashes, credentials, transparency log, stats,
│                report schema, fingerprint, AuditEvent log, structural policies, metrics
├── bench/       model certification: runner, blocks A–E, datasets, classifiers, compare
├── discover/    engine inventory, agent identification, agent stack, gap scan
├── smith/       scenarios, mock-tool oracle, agent adapters (bare-model, http, mcp)
├── swarm/       declared vs observed topology, attribution, fault plans
├── sentinel/    watch (drift), inventory watch, observe/gate proxy, approval queue
├── inquest/     importers (sentinel, OTel, JSONL), three-valued audit, degradation
├── card/        Auditability Card, deployment manifest
├── system/      system manifest, measurements, extractors, system report, draft, watch
├── adapters/    InferenceBackend + vllm, ollama, openai
├── telemetry/   GPU, system and network sampling
├── web/         static site, badges, printable certificates
└── cli/         opaas commands, one group per module
```

Every module writes its result as canonical, signable bytes. The suites only talk to
`InferenceBackend`, never to a specific engine; smith only talks to an agent adapter, never
to a specific framework.

---

## Documentation

- **[Methodology](docs/methodology.md)** — what is measured, how, and what cannot be claimed from each figure
- **[System audit](docs/system-audit.md)** — the system as the audited subject, one result format, one certificate
- **[Self-evaluation](docs/self-evaluation.md)** — certifying yourself, and what the signature guarantees
- [Standards mapping](docs/standards-mapping.md) — ISO/IEC 42001, AI Act, ENS, NIST AI RMF: what a certificate is evidence for, and what it is not
- [Datasets](docs/datasets.md) — format, sources, subsets, sample size
- [Design decisions](docs/decisions.md) — every departure from the design, why, and what it costs
- [Root key custody](docs/key-custody.md) · [Admission policy](docs/admission-policy.md) · [System design](docs/design.md)
- [Work plan](docs/workplan.md) · [Sentinel design](docs/proposals/sentinel.md) · [Bias design](docs/proposals/bias.md)
- [Changelog](CHANGELOG.md) — what changed between releases, and what to check when upgrading

---

## Development

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

CI runs the suite on Python 3.10 through 3.13, `ruff check`, `uv lock --check` and a package
build. **Dependencies are locked**: `uv.lock` pins the exact resolution of every package,
extras included — a tool whose claim is that a certificate is reproducible should not resolve
differently depending on the day it was installed. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

[MIT](LICENSE) — © 2026 onpremIA Labs.

The harness is free to use, modify and redistribute: that is what lets a third party run
their own evaluation and lets the methodology withstand scrutiny. What the license does not
grant is the ability to **issue onpremIA Labs certificates**, which depends on the Ed25519
root key, not on the code.

<div align="center">
<sub>The meerkat is the group's sentinel: it stands on its hind legs and keeps watch while the
others eat. That is what OPAAS does with a deployment.</sub>
</div>
