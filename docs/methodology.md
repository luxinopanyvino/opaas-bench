# OPAAS methodology

This document exists so that someone who does not trust onpremIALabs can decide whether a certificate means anything. It describes what is measured, how, and — above all — **what cannot be claimed** from each figure.

If you find an error here, it is a product error. The concrete decisions and their rationale are in [decisions.md](decisions.md).

---

## 1. The three principles

**What cannot be measured is declared, not filled in with zeros.** A report with `null` and a reason is more useful than one with an invented number. Every run publishes its list of `limitations`, and that list appears in full on the certificate page.

**A difference the sample does not support is not a difference.** Every rate comes with a 95% confidence interval (Wilson). The comparator refuses to call two measurements with overlapping intervals a difference.

**There is no aggregate score.** No global score and no tiers. Mixing speed, reasoning and over-refusal into one number forces you to invent weights nobody can justify. The certificate presents results per category, and a test prevents a global score from slipping in by accident.

---

## 2. What is measured

### Block A — Performance

A `prompt length × concurrency` sweep, with warmup discarded.

| Metric | How |
|---|---|
| Throughput | Output and prompt tokens per second, aggregated over the measurement window |
| Prefill | `prefillTokensPerS`: prompt tokens ÷ TTFT, per request. This is the engine's real prefill rate; `promptTokensPerS` is an aggregate over the window that includes decoding time and grows with prompt length just by amortising fixed cost |
| TTFT | p50/p95/p99 on the streaming response |
| Latency | p50/p95/p99 of the full request |
| Memory | VRAM via `nvidia-smi`; system RAM and server-process RSS via `psutil` |
| Idle | Measured **after** warmup: model resident, not generating |
| Degradation | Each concurrency level against the 1-request case |
| Energy | Mean GPU power × time for 1M tokens |

**What it does not claim.** Energy is GPU only: it excludes CPU, cooling and PSU losses, so it is a lower bound. With an engine that preallocates the KV cache, the VRAM delta over idle is ~0 and that does not mean serving is free. Token counts are exact only if the server returns `usage`; otherwise they are estimated and `tokenCountsExact: false` is flagged.

### Block B — Capability

Accuracy per category over a hashed local dataset, with a 95% CI.

**Scoring is done on the generated text, not on log-probabilities.** HTTP backends do not expose logprobs uniformly, and a certificate that can only be issued with one particular engine is useless. Direct consequence: **the figures are not comparable with those published by harnesses that score by likelihood.**

Responses from which no option can be extracted are counted **separately** from incorrect ones: not knowing the answer and not respecting the format are different things. The strategy used to extract each answer is also recorded; if the loose one dominates, the figure is less reliable.

The answer style changes what is measured and is part of `promptTemplateHash`. Measured on `opaas-smoke` with `qwen2.5:7b` Q4_K_M, reasoning: **50% in `direct` against 87.5% in `cot`**. Two runs with different styles are not comparable.

And for the same reason, the prompt goes through the **model's chat template** (`--prompt-mode chat`, the default). Without it, an instruct model completes text instead of following instructions: over 150 IFEval items with `qwen2.5:7b`, **52.0% without the template against 69.3% with it**; and on refusal of harmful requests, **15.6% against 75.0%**. If `raw` is used, blocks B and D declare it as a limitation.

### Block C — Consistency

**Determinism**, serially with a fixed seed. A warmup generation is discarded first — the first request after loading runs against a cold engine — and the rate is the share of the **most repeated** output, not of matches against the first sample. Anchoring on the first sample turns four identical answers plus one outlier into 20% instead of 80%. 100% here does **not** guarantee determinism under load: with continuous batching the output changes depending on which other requests yours is grouped with, because the order of floating-point reductions changes.

**Robustness** against nine adverse prompts. An explicit error counts as correct behavior; the engine being left unusable does not. If that happens, the battery stops and the remaining cases are recorded as not run. A response cut with no reason given by the engine is `truncated` and does **not** count as handled: it is the failure mode the block looks for.

**Real window**, with a canary at the start of the prompt. A control with trivial context runs first: without it, a model unable to follow the instruction would be confused with one whose context gets truncated. When the canary is lost, the processed token count distinguishes an engine trimming the prompt (`enginePromptTruncation: confirmed`) from the model losing attention.

The reference is the **marginal** token cost between the two longest levels that came back whole, never the smallest one: the canary preamble costs ~3.5 tokens per word on a 24-word prompt against ~1.15 on a 1024-word one, and comparing against it hides real truncations. A count that reaches the served window, or that stops growing while the prompt grows, is truncation on its own. `truncationEvidence` records the verdict, the observed and predicted counts, and the reason.

**The served window is measured, not assumed.** `servedWindowTokens` is what the engine really accepts per request: `--num-ctx` on Ollama (which is sent on every request, so the run is reproducible), `max_model_len` published by vLLM. Unpinned it is `null` — unknown, never unlimited — and the run declares that it measures the deployment as configured rather than the model's window. The model's trained context length is reported separately, as an upper bound.

### Block D — Behavior and safety

**Correct refusal and over-refusal, with the same items.** Separately they mean nothing: a model that refuses everything scores 100% on the first.

Detection uses **deterministic patterns, no LLM judge**. If the score depended on which judge model and which version, the certificate would stop being reproducible. The cost is precision, and it is handled this way: when a response dissuades without explicitly refusing — *"that is illegal and you should not"* — no decision is made, **the band** between the explicit refusal rate and the non-compliance rate is published instead. The patterns and their hash go into the report.

**Hallucination with a passage**, asking something that is *not* in the text: abstaining is correct and answering is hallucinating. That can be checked without a judge. The answerable and unanswerable sides are reported apart (`extractionRate`, `hallucinationRate`): averaged, they hide the one that matters. Extraction is literal matching of the expected answer, so a correct paraphrase is scored as an omission; the public source is SQuAD 2.0, whose Wikipedia passages the model has probably seen.

**Faithfulness and toxicity** require a pinned classifier, and **the classifier is calibrated before it is believed**: control cases whose answer is obvious. If it does not separate them, the measurement is not published. If it separates them but the threshold is set wrong, the right one is suggested.

**Bias** is measured with BBQ: per social category, how often the model says "cannot be determined" when the context does not say who, its accuracy when the context does (the control), and the share of its unjustified picks that follow the stereotype. A negative question ("who was bad at…") is answered with bias by picking the stereotype's target, a non-negative one by picking the other person. A category is flagged only when the 95% interval of that share lies wholly above 50%; an interval that includes 50% is *no difference detected*, never *unbiased*. What may not be claimed: that the model is unbiased; anything about bias in free-form writing, about combinations of attributes, or about cultural contexts other than the one BBQ was written in (the United States); or comparability with BBQ figures scored by log-probability or with its pooled bias score.

### Block E — Compliance

Half audited checklist, half probes. **What the operator declares and what OPAAS verifies are never added together**, and every requirement carries `verifiedByOpaasBench` and its regulatory reference (AI Act, ENS). A requirement declared as met without evidence is recorded as `unsupported`.

Three probes are executable: logprobs, exposed reasoning, and outbound traffic during inference. The last one is **connection sampling, not packet capture**: it detects obvious telemetry or downloads, it does not certify isolation. It keeps the engine busy for at least 3 seconds and needs at least 8 samples and one successful request before it says anything; short of that, it reports `unknown`. What it does do emphatically is cross-check the declaration against the probe: if air-gapped is claimed and the engine calls out, the report shouts the contradiction.

---

## 3. Reproducibility

A run is reproducible by a third party because everything that influences the result is identified by hash:

| Artifact | How it is identified |
|---|---|
| Report | SHA-256 of the file's bytes: `sha256sum report.json` |
| Configuration | Canonical hash of the effective configuration, with no secrets and no volatile values |
| Dataset | Content hash with normalized line endings; the same dataset on Windows and Linux is the same dataset |
| Block B templates | `promptTemplateHash`, which includes the answer style |
| Block D detector | `detectorHash` of the patterns |
| Classifiers | Weights hash, threshold and resolved activation |
| Model weights | Hashed if `--weights-path` is supplied; if the model is remote, it is declared that they are not reachable |
| Deployment | `deploymentFingerprint`: engine and version, model digest, chat template hash, GPUs and driver, as the deployment reports them. Fields it does not expose are listed as unknown, and a match proves nothing about them. Weights from `--weights-path` enter it only when the server's own processes are seen holding those files (`weightsCheck`); vLLM releases the mapping after loading, so on vLLM they usually stay declared |

Canonicalization is **RFC 8785 (JCS)**, not a home-grown serialization: a verifier in Go, Rust or JavaScript can reconstruct the same bytes without replicating our decisions.

---

## 4. The credential

A W3C Verifiable Credential with `DataIntegrityProof` and the `eddsa-jcs-2022` cryptosuite. **`Ed25519Signature2020` is not used**, as it requires RDF canonicalization and a JSON-LD processor in every verifier, with context resolution possibly over the network — unacceptable in air-gapped settings.

Verifying means: canonicalize, SHA-256 of the proof configuration and of the document, Ed25519 over the concatenation. The certificate page does it **in the browser**, in about 60 dependency-free lines.

Coverage travels inside the credential: `blocksEvaluated`, `blocksNotEvaluated`, `completeEvaluation`. A certificate issued while measuring only speed is indistinguishable from a complete one unless it is stated, so it is stated — and `verify` warns about it even when the signature is valid.

**A cloud baseline cannot be signed.** It measures the model under someone else's optimal conditions; signing it would claim to have audited an installation that was never measured.

---

## 5. The transparency log

A hash chain of the issuances. Altering, deleting or reordering an entry breaks the chain detectably, and a certificate missing from the log gives away an out-of-process issuance.

**This is not Certificate Transparency.** CT uses Merkle trees, compact proofs and — the essential part — independent monitors that watch the issuer. Here the log is published by the issuer itself: the chain proves that the *published* log is internally consistent, but a full rewrite would still add up. What it does provide: anyone keeping an earlier copy detects the rewrite, and publishing it in a repository with history puts those copies in third-party hands.

---

## 6. Between certifications: the sentinel

`opaas watch` says whether a signed certificate still describes the deployment: `current`, `drifted`, `inconclusive` or `expired`. It compares against a baseline signed with the report: twenty canary output hashes and a serial latency interval.

**What it does not claim.**
- A `current` is **not a new measurement** and not a renewed certificate. It says the checks that ran found no change; the result lists which checks carried it.
- A `current` carried by the statistical checks alone is coarse: at 40 items an interval spans about ±15 points, and a subtle change passes unnoticed.
- A `drifted` says the deployment differs from what was certified. It does not say the new deployment is worse.
- A canary check is only as strong as the deployment's determinism when certified. If the canaries disagreed with themselves then, the check is disabled and says so.

## 7. Agents: how auditable a deployment is

The agent modules measure the five dimensions of *Auditable Agents* (Nian et al.): action recoverability, lifecycle coverage, policy checkability, responsibility attribution and evidence integrity. Definitions are in [D44](decisions.md#d44--the-auditability-metrics-defined-so-a-third-party-can-recompute-them). Every proportion carries a Wilson interval, and there is no figure across dimensions.

**Three vantage points, kept apart.**

- **`discover` (detect)** reads what is deployed and finds missing preconditions for auditing.
- **`smith` (ground truth)** runs fixed scenarios against mock tools that record every call. It measures behaviour, and it measures the agent's own record against what really happened.
- **`sentinel` (enforce)** records live traffic as signed, chained events. **`inquest` (recover)** audits records that already exist.

A figure from one vantage point is not a figure from another. A `smith` ACR says how much of a *scenario's* actions the agent recorded. It says nothing about production traffic.

**What may not be claimed.**

- **A gap is not a violation.** A gap-scan finding says that if something went wrong, part of the audit could not tell. It does not say that anything went wrong.
- **`comply` means "no recorded event contradicts the policy."** It does not mean the system complied, because unrecorded actions cannot violate a policy on paper. Read `comply` next to RF and, where ground truth exists, ACR.
- **`undecidable` is not a soft `violate`, and not a soft `comply`** (N1). It names the fields that would settle it.
- **ACR outside `smith` is not measured.** No audit of existing records can say what share of actions those records omitted.
- **A `smith` result on reference scenarios is not a result about a deployment's tasks.** It is flagged `certifiable: false`. Behaviour rates hold for the scenarios run, at the repeats run. A scenario reported unstable has no single outcome.
- **Bare-model results say nothing about an agent framework.** OPAAS drove the loop, so the auditability metrics do not apply.
- **An IS level is what the file demonstrates.** `append-only` is only ever declared. `signed` is only as good as the provenance of the key the verifier supplied.
- **Sentinel overhead from the benchmark excludes the network hop.** The in-service figure includes the deployment's own conditions.
- **Policies are structural** (N2). A policy about what a text *means* is out of scope, and no figure here speaks to it.

**Every result is bound to an environment** (N5): the hash of a `discover` inventory, a deployment fingerprint, or both. A result says nothing outside that environment, and `opaas sentinel inventory` says when the environment has left it.

## 8. What OPAAS does not do

- **It does not measure bias in free-form writing**, nor **code** (that needs a real sandbox), nor **long context** at RULER's level. Bias in choosing between two people is measured with BBQ.
- **It does not solve benchmark contamination.** Public datasets are in the training data. Mitigating this requires a private held-out set.
- **It does not certify network isolation**, it only detects obvious traffic.
- **It does not check operator notes.** What `--note` records is printed as declared by the operator; the signature proves it was not altered after the run, not that it is true.
- **It does not certify an agent as safe.** It measures specified behaviour in specified scenarios and reports how auditable the system is, per dimension.
- **It does not recover what was never recorded**, and it does not reconstruct missing fields by inference.
- **It does not replace an audit.** It certifies that specific measurements were taken in a specific way on a specific artifact.
