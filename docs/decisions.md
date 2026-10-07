# Design decisions

A log of decisions that depart from the original design document or that settle points it left open. Each one says what was decided, why, and what is lost.

---

## D1 — The signature uses `eddsa-jcs-2022`, not `Ed25519Signature2020`

**Status:** applied · **Affects:** [`credential/vc.py`](../src/opaas/credential/vc.py)

The design proposed `Ed25519Signature2020`. That suite computes the signature over the **RDF** canonicalization (URDNA2015) of the JSON-LD document, which forces any verifier to bring in a full JSON-LD processor and to resolve the `@context` contexts — potentially over the network, exactly what we do not want in an air-gapped environment.

`DataIntegrityProof` with the `eddsa-jcs-2022` cryptosuite is used instead, equally standard (W3C VC Data Integrity), which canonicalizes with **JCS (RFC 8785)**. Verifying comes down to: canonicalize, SHA-256, Ed25519.

**What is lost:** VC libraries that only implement `Ed25519Signature2020` will not validate these credentials. That is accepted: the target verifier is a technical third party validating a specific certificate, not a generic identity wallet.

## D2 — The certified hash is the hash of the file's bytes

**Status:** applied · **Affects:** [`report.py`](../src/opaas/report.py), [`hashing.py`](../src/opaas/hashing.py)

First version: `report.json` contained `{"report": {...}, "reportHash": "..."}`, with the hash computed over the re-serialized `report` subtree. That forces the verifier to replicate our canonicalization exactly just to check anything.

Now the file contains the report **and nothing else**, written in canonical JCS bytes. Its hash is the file's hash: verifying a report is `sha256sum report.json`. `RunReport.content_hash()` and `hashing.file_digest()` match by construction, and a test pins that invariant.

## D3 — Block coverage travels inside the credential

**Status:** applied · **Affects:** [`suites/__init__.py`](../src/opaas/suites/__init__.py), [`credential/vc.py`](../src/opaas/credential/vc.py)

While blocks B–E are not implemented, a run covers only block A. A credential saying `status: "evaluated"` would be indistinguishable from one that passed all five blocks — exactly the misleading claim this project exists to prevent.

The report carries `coverage` (`blocksEvaluated`, `blocksMissing`, `complete`) and the status becomes `evaluated-partial`. The credential mirrors that coverage in `evalRun`, and `verify` emits a visible warning even when the signature is valid. `--suite all` warns on stderr that it is not equivalent to all five blocks.

## D4 — `verify` never takes the key from the credential

**Status:** applied · **Affects:** [`credential/trust.py`](../src/opaas/credential/trust.py)

Verifying offline requires having the issuer's public key beforehand. Resolution, in order: explicit `--key` / `--did-document` → anchors installed in `~/.opaas/anchors/` (or `$OPAAS_ANCHOR_DIR`) → anchors bundled with the tool.

If there is none, it fails saying what is missing. It never uses a key embedded in the credential itself: that would make it self-verifying and the signature would prove nothing.

The bundled anchors directory is **empty** until the production root key exists, published at `https://onpremialabs.com/.well-known/did.json`.

## D5 — Numbers outside the canonicalizable range fail instead of being signed

**Status:** applied · **Affects:** [`canonical.py`](../src/opaas/canonical.py)

JCS delegates number serialization to ECMAScript, whose exponential notation rules we do not fully replicate. For |x| ≥ 1e21 or |x| < 1e-6 a `CanonicalizationError` is raised instead of emitting bytes another language could reproduce differently.

Signing non-reproducible bytes produces a credential nobody can verify and that also looks correct at issuance time. A loud failure is preferable.

---

## D35 — Weights on disk enter the fingerprint only when the server is seen holding them

**Status:** applied · **Affects:** [`fingerprint.py`](../src/opaas/fingerprint.py), [`telemetry/system.py`](../src/opaas/telemetry/system.py), [`hashing.py`](../src/opaas/hashing.py)

Outside Ollama the fingerprint (D34) was weak: vLLM and the OpenAI-compatible servers (`llama-server`, TGI, LM Studio) publish no model digest and no template. That is exactly the setup where the operator has the weights on disk, and `--weights-path` already hashed them. But hashing files proves what is in the files, not what the engine loaded. Putting that hash in the fingerprint as if the deployment had reported it would be D14's mistake, a declaration passing for an observation.

**So it is checked.** When the server runs on this host, OPAAS already knows its PID (block A uses it for RAM). `psutil` lists the files that process **and its children** have memory-mapped or open. The children matter: Ollama serves from a runner spawned by `ollama serve`, and vLLM runs its engine core in a separate process. The declared weight files are compared with that set:

| `weightsCheck` | Meaning | In the fingerprint |
|---|---|---|
| `confirmed` | Every declared weight file is held by the server's process tree | `weightsHash` and `tokenizerFilesHash` |
| `partial` | Only some are: it may be serving a different set | No |
| `not-observed` | None are | No |
| `unverifiable` | Remote server, unidentified process, or no permission | No |
| `not-declared` / `not-hashed` | No `--weights-path`, or nothing hashable there | No |

The declared hash stays in `model.weightsHash` in every case. Checked on Windows against a real child process that memory-maps a file and closes its handle, the way llama.cpp loads a GGUF.

**Template and tokenizer files are hashed too.** `chat_template.jinja`, `tokenizer_config.json`, `generation_config.json` and their siblings change what the model is asked as much as the weights change what it knows (D25). They go in `model.tokenizerFilesHash`, and into the fingerprint alongside confirmed weights. A GGUF file embeds its template, so its weights hash already covers it.

**The weights are hashed once.** The runner computes the hash and passes it to both the model section and the fingerprint. Hashing 40 GB twice to fill two fields would be absurd, and a test pins it.

**What it costs:** **vLLM usually reads `not-observed`.** It copies the weights to the GPU and releases the mapping, so on vLLM the weights stay declared, and the detail says that is what `not-observed` looks like there. It is a limit of observation, not evidence of other weights. Template files are trusted to come from the confirmed model's directory: a server started with an explicit `--chat-template` elsewhere is not covered. And the check only works when OPAAS runs on the serving host.

## D34 — The report records what was serving, not only how OPAAS ran

**Status:** applied · **Revises:** the fingerprint fields proposed in [D33](#d33--between-certifications-a-sentinel-checks-the-certificate-still-holds) · **Affects:** [`fingerprint.py`](../src/opaas/fingerprint.py), [`adapters/`](../src/opaas/adapters), [`telemetry/gpu.py`](../src/opaas/telemetry/gpu.py), [`credential/vc.py`](../src/opaas/credential/vc.py)

`configHash` is the hash of the run's effective configuration: suites, options, prompt mode, model id. It is the right identity for *how OPAAS ran*, and it says nothing about the deployment underneath. Re-pull `llama3:8b` onto different weights, upgrade the engine or the GPU driver, and the configuration hash stays the same while the certificate quietly stops describing what is serving. A test now shows exactly that: same run, new model digest, identical `configHash`.

The report gains a signed `deploymentFingerprint` with the deployment's identity, as the deployment reports it:

| Field | Source |
|---|---|
| Engine and version | Adapter metadata |
| Model id and **model digest** | `InferenceBackend.model_digest()`; Ollama publishes one per model in `/api/tags` |
| **Chat template hash** | `InferenceBackend.chat_template_hash()`; Ollama returns the template in `/api/show` |
| GPUs and **driver version** | `nvidia-smi`, now also queried for `driver_version` |

The credential carries its hash as `configuration.deploymentHash`. The certificate page lists every field the deployment did not expose, and the printable certificate shows the hash.

**Only what the deployment reports about itself.** D33 proposed quantization, the served window and the prompt mode too. They are left out: quantization is often the operator's `--quantization`, and the window and prompt mode are what OPAAS chose (`--num-ctx`, `--prompt-mode`). A declaration in a fingerprint would pass for an observation (D14), and those values are already in the configuration.

**Unknown is listed, never guessed.** vLLM and generic OpenAI-compatible servers publish no model digest and no template, so on them the fingerprint rests on the engine version and the GPU. The `unknown` map says so with the reason, and two fingerprints with the same hash prove nothing about a field unknown on both sides.

**What it costs:** one more section in every report, and one more field in the credential, which changes the signed bytes of every new credential. Reports and credentials issued before this have no fingerprint; the site says the report predates it rather than showing nothing. And a change on the server side of vLLM, such as a new `max_model_len`, is not in the fingerprint; it stays visible in `backendMetadata`.

## D33 — Between certifications, a sentinel checks the certificate still holds

**Status:** proposed, not implemented · **Affects:** [`proposals/sentinel.md`](proposals/sentinel.md)

A certificate is valid for 90 days and can become false the day after it is issued, when the engine, the model build, the quantization or the driver changes. Monitoring production in real time is not the answer: that traffic is not reproducible, has no ground truth to score against without a judge (D11), and carries users' personal data.

The proposal is `opaas watch`, a one-shot command for a scheduler. It answers one question, whether the signed certificate still describes this deployment, with `current`, `drifted`, `inconclusive` or `expired`. It runs three checks, cheapest first:

1. **The deployment fingerprint:** engine version, model digest, quantization, served window, GPU driver. No inference is needed.
2. **Exact-output canaries** at temperature 0. Applied only if block C certified 100% serial determinism.
3. **Coarse statistical drift**, under the same overlap rule as `compare`.

It never signs, never holds the key, never reads user traffic and never revokes. It supplies the trigger for the revocation policy that is still undecided, not the mechanism.

**What it would cost:** `run` has to record a signed `deploymentFingerprint` and `sentinelBaseline`, because today's `configHash` identifies OPAAS's run configuration, not the deployment. Reports certified before that can only be checked loosely. It adds a little load on production per check, and it carries a risk of false reassurance, which the result has to counter by saying which checks actually carried a `current`.

## D32 — Bias is measured with stereotype questions and counterfactual pairs

**Status:** proposed, not implemented · **Settles (once applied):** the bias methodology left open in block D · **Affects:** [`proposals/bias.md`](proposals/bias.md)

Bias has been declared unmeasured in every run. What was missing was a decision on which axes and which prompts, not a tool. The proposal makes it with two deterministic measurements inside block D:

- **Stereotype questions** from BBQ (English) and EsBBQ (Spanish). When the passage gives no reason to pick anyone, picking someone is choosing on a stereotype.
- **Counterfactual pairs** written by the operator for their own domain: does the decision change when only a protected attribute changes?

Both are scored by answer extraction, with no judge. Bias is reported only when the 95% interval excludes 50%. BBQ's combined bias score is deliberately left out: it would hide which of the two failure modes produced it.

**What it would cost:** about 2,000 more items per run, two more pinned sources, and inheriting the benchmark authors' judgement of which answer is stereotype-aligned. Free-generation bias and intersectionality stay unmeasured and declared.

It is recorded as *proposed* so the review happens before the code: the open questions are listed at the end of the proposal.

## D31 — The operator can add context, and it is printed as unverified

**Status:** applied · **Affects:** [`report.py`](../src/opaas/report.py), [`runner.py`](../src/opaas/runner.py), [`web/`](../src/opaas/web)

`limitations` is what the tool knows it could not measure. Some context only the operator knows: the machine was shared and background load was not controlled, the engine was restarted before block A, the GPU was power-capped. There was no place to say it, so it either went unsaid or invited editing the report, which breaks the signature.

`opaas run --note "..."` (repeatable) records it in a new report field, `operatorNotes`. It is signed with the rest of the report, and the CLI summary, the certificate page and the printable certificate show it under **Declared by the operator**, with the words "not verified by OPAAS".

It is **never added to `limitations`**. That is D14's rule applied to the whole report: a claim nobody checked is not mixed with a finding. A note saying "the network was isolated" next to the probe's own limitation would read as if OPAAS had confirmed it.

Notes are capped at 500 characters each, because each one is printed on the certificate, and an empty note is refused. Both checks run before the backend loads.

**What it costs:** a free-text field in a public, signed document. OPAAS cannot tell whether a note is true, or whether it leaks something it should not; the help text warns against secrets, and `_publishable_config` does not apply because the note is the operator's own words. A false note stays what it was labelled: the operator's claim, which the issuer's signature proves unaltered, not true.

## D30 — Block E's regulatory references are cited to the clause and checked

**Status:** applied · **Revises:** the framework references of the first block E catalogue · **Affects:** [`declaration.py`](../src/opaas/declaration.py), [`standards-mapping.md`](standards-mapping.md)

Each block E requirement carries the regulatory references it relates to, and they travel in every report. Checked against the official texts, several did not hold:

- **ENS op.acc.5** is *Mecanismo de autenticación (usuarios externos)* and was cited for air-gapped operation. **ENS mp.info.1** is *Datos personales* and was cited for the model licence. **ENS op.pl.2** (*Arquitectura de seguridad*) was cited for training-data documentation. None of the three is about what the requirement asks; they are removed.
- **"AI Act art. 53"** pointed at a list of provider obligations without saying which. Now **53(1)(b)** with **annex XII 1(h)** (the licence) and **2(c)** (training data), and **53(1)(d)** for the public summary of training content.
- **"AI Act art. 13"** for logprobs and exposed reasoning is now **13(3)(b)(iv)**, the capabilities to provide information that explains the output. The article as a whole is about the provider's instructions for use.
- Inference logging adds **art. 26(6)**, the deployer's duty to keep logs, alongside art. 12.

A reference in a signed report is a claim. A wrong one tells an auditor the requirement concerns something it does not, and a reference that is too broad cannot be checked at all. A test now pins the ENS measures the catalogue may cite, with their official titles, so a new one has to be checked before it goes in.

**ISO/IEC 42001 and the NIST AI RMF are not added to the catalogue.** The NIST subcategories that fit OPAAS (MEASURE 1.1, 2.3, 2.5…) describe the measurement blocks, not block E's documentary requirements. ISO/IEC 42001 is not freely available, so its control numbers cannot be checked the way the others were. Both are mapped in [standards-mapping.md](standards-mapping.md), which also records the gaps: adversarial security testing and bias.

**What it costs:** air-gapped operation is left with a single ENS reference, mp.com.1, and nothing in the AI Act. That is accurate, not a loss: the AI Act does not require air-gapped operation. And the blocks where OPAAS fits the frameworks best, A to D (art. 15, mp.sw.2, op.pl.4, MEASURE 2.3), still carry no references in the report; doing that is a separate change.

## D29 — The printable certificate is a rendering, and only of a credential that verifies

**Status:** applied · **Affects:** [`web/certificate.py`](../src/opaas/web/certificate.py), [`cli/main.py`](../src/opaas/cli/main.py)

The signed credential is JSON and the certificate page lives on a site. Neither is what an operator hands to an auditor or attaches to a tender, and people asked for a certificate "like the one you get for finishing a course". `opaas certificate` renders one self-contained HTML page laid out for A4 landscape, which the browser prints to PDF.

Three things keep it honest:

1. **It is only rendered for a credential that verifies**, with the same key resolution as `verify` (D4) and the report checked against the certified hash. A printed certificate for a failing signature would be a forgery with good typography.
2. **It points back to the signature.** Paper and PDFs are trivially edited, so the page carries a QR code to the published certificate page (D19), where the signature is checked in the browser, plus the report hash and credential id that `opaas verify` checks offline. It says in so many words that the credential, not the page, is the certificate.
3. **It reads like a diploma without grading like one.** One line per block, a block that was not evaluated says so just as visibly, and there is no final mark, because there is no aggregate score. Every limitation the run declared is printed, even when that takes a second sheet.

**HTML, not PDF.** Generating the PDF directly would need a rendering engine (WeasyPrint and its native libraries, or a headless browser), much heavier than everything else OPAAS depends on. Every browser prints HTML to PDF already.

**What it costs:** one more runtime dependency, `segno`, to draw the QR code. It is pure Python with no dependencies of its own. Writing a QR encoder in-house was the alternative, and a QR code that scans wrong on a certificate is worse than having none. A one-line summary per block also leaves out the confidence intervals and the detail; the page says where the full report is, and the QR leads to it.

## D28 — Credentials are valid for 90 days, not 6 months

**Status:** applied · **Settles:** the re-certification frequency the design left open · **Affects:** [`credential/vc.py`](../src/opaas/credential/vc.py)

The design suggested 6 months and left it open; it was implemented as 182 days. It is now **90 days** by default. On-prem LLM stacks change faster than that: engine releases (vLLM, Ollama) come out every few weeks, and a driver update, a new engine version or a different quantization changes block A's results, and sometimes block B's, even with the same weights. A certificate describing a stack that was replaced three months ago is describing a different deployment.

`--validity-days` still sets any other period for a given credential. `verify`, the badge and the site page already treated expiry as invalid, so nothing else changes.

**What it costs:** re-certifying twice as often, with a full run each time. A key compromise is also contained sooner (see [key custody](key-custody.md)).

## D25 — Without the chat template you are not measuring instruction following

**Status:** applied · **Fixes:** a mistake of our own in both adapters · **Affects:** [`adapters/`](../src/opaas/adapters)

The Ollama adapter used `raw: true` and the OpenAI-compatible one `/v1/completions` by default. Both **skip the model's chat template**. I chose that so the template would not pollute block A's throughput measurement, and it was an optimization of block A at the expense of every other one.

An instruct model without its template is not acting as an assistant: it completes text. Measured with `qwen2.5:7b` over 150 IFEval items and 144 XSTest ones, changing only the mode:

| Measurement | `raw` (no template) | `chat` (with template) |
|---|---|---|
| IFEval | 52.0% (44.1–59.8) | **69.3%** (61.6–76.1) |
| Refusal of harmful requests | 15.6% (8.7–26.4) | **75.0%** (63.2–84.0) |
| Over-refusal of benign ones | 2.5% (0.7–8.7) | 3.8% (1.3–10.4) |

The comparator confirms that the IFEval difference is real: the intervals do not overlap. And the 69.3% agrees with what is published for this model (70–75%), while the 52% agreed with nothing — that was the signal that something else was being measured.

**The refusal figure is worse still.** Without the template the model barely refused: it was not acting as an assistant, so its safety training did not kick in. A certificate would have claimed it refuses 15.6% of harmful requests when it refuses 75%. That is the kind of defamation that sinks a certifier.

The breakdown gave it away from the start — `starts_with 2/12`, `paragraph_count 1/11`, `bullet_count 1/13`: massive failures precisely on the formatting instructions an instruct model follows effortlessly.

*(A first check with 25 items gave 52% against 88%. The good figure is the one from 150: at n=25 the interval ran from 70% to 96%. A reminder of why sample size matters even for diagnosis.)*

**The default becomes `chat`** in both adapters. `--prompt-mode raw` is still available for anyone who wants engine throughput without the template, but blocks B and D **detect the mode from the backend metadata and declare it as a limitation**, with the concrete figure. A rule that only lives in the documentation gets broken.

`--api` disappears from the CLI: it was a detail of the OpenAI protocol leaking into a decision that applies to every engine. The `api` key still works for programmatic use.

## D24 — The default dataset is real, and whatever is missing is declared

**Status:** applied · **Affects:** [`datasets/sources.py`](../src/opaas/datasets/sources.py), [`suites/safety.py`](../src/opaas/suites/safety.py)

The default was `opaas-smoke`, so an unconfigured run produced a report that declared itself useless. With automatic downloading already solved (D20), that stopped making sense: the default becomes `ifeval,xstest`. `opaas-smoke` is still available to check that the pipeline works.

A side effect that had to be closed: `ifeval,xstest` provides no `grounded_qa` items, so **the hallucination measurement vanished from the report without a trace**. Now a dimension missing for lack of data is declared just like an unimplemented one, with the concrete instruction for covering it — in the hallucination case, composing with your own documents.

The `CapabilityOptions` and `SafetyOptions` defaults still point at the smoke dataset on purpose: programmatic use or a test that forgets to name a dataset must not hit the network unasked. The real default lives in the CLI, which is the product.

## D23 — A certificate is issued from one run, so the dataset is composed

**Status:** applied · **Extends:** D20 · **Affects:** [`datasets/cache.py`](../src/opaas/datasets/cache.py)

`--dataset ifeval,xstest` composes the sources into a single dataset. If block B and block D needed different datasets, two runs and two certificates would be required for the same deployment, which is the opposite of what is wanted.

The order is normalized: `ifeval,xstest` and `xstest,ifeval` produce the same artifact and the same hash.

**Your own documents can be mixed in.** The design asked for measuring hallucination "over your own documents", and that is the on-prem differentiator: whoever deploys cares far more about whether the model makes things up about their internal documentation than about whether it gets a Wikipedia passage right. They are written in the already-documented format and composed with the public sources. Their contents never leave the customer's network: only metrics and the hash are published.

Two safeguards that came out of testing it:

- **A task name clash fails before anything is written.** On re-import, a task with the same name replaces the previous one — correct for re-importing a source, disastrous when composing: somebody's data would have vanished silently.
- **If one part is a smoke set, the whole thing is flagged.** Composing a real dataset with `opaas-smoke` cannot produce something presented as a measurement.

## D21 — The log is a hash chain, and it is not called Certificate Transparency

**Status:** applied · **Affects:** [`transparency.py`](../src/opaas/transparency.py)

Every issuance chains to the previous one. Altering, deleting or reordering an entry breaks the chain detectably, and a certificate missing from the log gives away an out-of-process issuance. `verify --log` adds the inclusion check.

**What it is not.** CT uses Merkle trees, compact inclusion and consistency proofs, and — the essential part — independent monitors that watch the issuer. Here the log is published by the issuer itself: the chain only proves that the *published* log is internally consistent. If onpremIALabs rewrote the entire log and republished it, it would still add up.

What it does provide: anyone keeping an earlier copy detects the rewrite, and publishing the log in a repository with history puts those date-stamped copies in third-party hands. Calling it CT would be selling a guarantee it does not give; moving to real CT requires external monitors, and that is a product decision — who operates them and why you would trust them — before it is a code one.

## D22 — The cloud baseline cannot contaminate the certificate

**Status:** applied · **Affects:** [`runner.py`](../src/opaas/runner.py), [`credential/vc.py`](../src/opaas/credential/vc.py), [`compare.py`](../src/opaas/compare.py)

The design asked for a quality reference against cloud endpoints, marked as `cloud-baseline` and **never** mixed with block A. It is implemented with three barriers, not with a footnote:

1. **A baseline refuses blocks A and E.** A shared endpoint's throughput depends on other people's load and on hardware you do not control; block E asks about *your* deployment, and somebody else's endpoint is not it.
2. **A baseline cannot be signed.** `build_credential` rejects it: signing it would claim to have audited an installation that was never measured.
3. **The comparator labels it and excludes it** from the performance tables, warning that its quality does compare — that is what it is for — but its performance describes no deployment.

A rule written in the documentation gets broken; one the code rejects does not.

## D27 — The engine's concurrency limit is declared, not guessed

**Status:** applied · **Affects:** [`adapters/base.py`](../src/opaas/adapters/base.py), [`suites/performance.py`](../src/opaas/suites/performance.py)

Block A could tell that an engine was queueing — flat throughput, exploding TTFT — but not why, so its advice ended at "check the server's concurrent request limit". The obvious fix was to read the limit from the engine. It cannot be done: vLLM's `/v1/models` publishes `max_model_len` but not `max_num_seqs`, its metrics expose current utilisation (`vllm:num_requests_running`) rather than the configured ceiling, and Ollama does not expose `OLLAMA_NUM_PARALLEL` at all.

So `max_concurrent_requests()` follows `context_window()`: it returns what is known and `None` otherwise, where **None means unknown, never unlimited**. The default source is the operator declaring it with `--max-concurrency`, and an adapter overrides the method if its engine ever does publish the number.

Being a declaration, it obeys D14: **declared and observed are never added together.** They are compared. Declaring 8 and then serializing under load produces `concurrency-contradicts-declaration` — the same shape as block E's air-gapped contradiction, and far more useful than a generic hint. Declaring 1 and serializing is just an accurate declaration, and produces the plain finding.

When nothing is declared and more than one concurrency level was swept, the report says the limit is unknown and that queueing was **inferred from ratios, not read from the server**. Without that line a reader could take the finding for a measurement of the engine's configuration, which it is not.

**Separately, `--suite-concurrency`.** Blocks B and D hardcoded 4 parallel requests and the CLI could not reach it: `--concurrency` only ever fed block A's sweep. It is engine-agnostic — the harness's own knob, not the engine's — and it changes how long a run takes, never what it measures.

**What is deliberately not done:** OPAAS does not set `OLLAMA_NUM_PARALLEL` or any other engine setting. It evaluates a deployment that is already running; an auditor that reconfigures the system first is certifying a deployment it invented, not the customer's.

## D26 — The block D classifiers download themselves too, against a pinned revision

**Status:** applied · **Revises:** D17 · **Affects:** [`classifiers/registry.py`](../src/opaas/classifiers/registry.py), [`classifiers/cache.py`](../src/opaas/classifiers/cache.py), [`suites/safety.py`](../src/opaas/suites/safety.py)

D17 rule 1 said nothing is downloaded at run time, and the weights are supplied by whoever evaluates. The reasoning was D9's, and D20 already revised it for datasets and left classifiers behind — inconsistently, because the argument is identical. The consequence was the one D20 named: `--suite all` ran the five blocks and quietly declared two dimensions unmeasured, and the fix was a documentation page telling you to go download two models by hand. Most people never did, so most reports never measured toxicity.

Revised: with the classifiers extra installed and no `--toxicity-model` / `--faithfulness-model` given, the pinned default is fetched once into `~/.opaas/cache/classifiers/<task>/` and reused. What is **not** given up:

- **The revision is a full commit SHA, not a branch.** `main` would mean two runs of the identical command on different days silently using different weights — the exact failure the dataset SHA-256 exists to prevent. Bumping it is a reviewed change to the registry, and a cached directory whose recorded revision does not match the pinned one is re-downloaded rather than trusted.
- **An explicit path still wins and downloads nothing.** The operator's choice is never silently replaced.
- **`--offline` forbids this download as firmly as the dataset one**, failing with what to supply instead of trying. `opaas fetch` pre-populates both.
- **Calibration (D18) still gates everything.** A default that fails the control set publishes no figure, exactly like one you picked yourself. The default is a convenience, not a promise that it discriminates.
- **The report says which weights measured.** When the pinned default did the work, the repo and revision go into `limitations`, so a reader never assumes weights the operator chose. The weights hash travels as before.

**What it costs.** A first run on a fresh machine now pulls ~800 MB it did not pull before, and the operator did not ask for it explicitly — the trade D20 already made for datasets, with a bigger number. It is gated on the extra being installed, which is itself an explicit `pip install`, and `opaas datasets` shows what is cached before a run starts.

**What is deliberately not automated.** Block E's seven declared requirements are claims the operator makes under their own responsibility, with evidence; auto-filling them would let a certificate assert compliance nobody verified, which is the failure mode this project exists to prevent. And `grounded_qa` items have no generic artifact to download: the point is whether the model invents things about *your* documents.

## D20 — Datasets download themselves, but against a pinned hash

**Status:** applied · **Revises:** D9 and D15 · **Affects:** [`datasets/sources.py`](../src/opaas/datasets/sources.py), [`datasets/cache.py`](../src/opaas/datasets/cache.py)

D9 and D15 established that nothing is downloaded at run time. The reason was good — air-gapped operation and reproducibility — but the consequence was bad: the first run required preparing files by hand before seeing anything work.

Revised: `--dataset ifeval` downloads on first use and reuses the cache from then on. What is **not** given up is what made the original decision valuable:

- **Every source carries its verified SHA-256.** If what is downloaded does not match, the run stops showing both hashes. "Download if it is not cached" is convenient; "trust whatever comes down the wire" would make the certificate non-reproducible. A legitimate upstream change is reviewed and the hash updated by hand.
- **The cache is persistent and the second run does not touch the network.** `opaas fetch` populates it on a connected machine and the directory is copied; `--offline` forbids the network and fails saying what is missing, before loading the model.
- **The dataset version is the source hash**, not the download date: two people downloading the same thing get the same version in the manifest and in the certificate.

A side effect that came out of testing it: the importer wrote its own fixed license and ignored the one the source declared. Today they match, but a registered source pointing at a different file would have lied in the manifest. Now the license and the URL come from whoever supplies the file.

## D19 — Publication is a static site verified client-side

**Status:** applied · **Affects:** [`web/`](../src/opaas/web)

The plan asked for a `publish` command that "uploads the result". It is implemented as a **static site generator**, not as an upload to an API. No server, no database, no backend that could lie: the site copies to any hosting and the public registry is audited with a `git diff`. Deploying is a separate step, with everyone's own tooling.

**Verification happens in the browser.** The page carries the credential and the DID document embedded and reimplements in JavaScript the same algorithm as `opaas verify`: JCS canonicalization, SHA-256 of the proof configuration and of the document, and Ed25519 via WebCrypto. If the page asked a server whether the certificate is good, verifying would mean trusting the issuer — exactly what the signature exists to avoid.

JCS in JavaScript turns out to be almost free: `Array.sort()` already sorts by UTF-16 code units and `JSON.stringify` serializes numbers and strings as the specification requires. The complete verifier fits in about 60 dependency-free lines.

**The badge cannot lie by omission.** It is the only thing most people will see. A green "OPAS certified" over an evaluation that only measured speed would be the misleading claim the project exists to prevent: the badge says which blocks were evaluated and changes color if the evaluation is partial or the credential has expired.

**`publish` refuses to pair things wrongly.** Before generating anything it checks that the report's hash matches the one the credential declares. Publishing a certificate next to a report that is not its own would be worse than not publishing it.

Validated in the browser: valid signature over the real certificate, and `Credential NOT valid — the signature does NOT match` when altering a single field of the embedded credential.

## D18 — A classifier is calibrated before it is believed

**Status:** applied · **Affects:** [`classifiers/calibration.py`](../src/opaas/classifiers/calibration.py)

A classifier loaded and nothing more is a black box that returns numbers. Before letting it decide whether a response is faithful or toxic, it is fed control cases whose correct answer is obvious. If it does not separate them, the measurement **is not published**. It is the same principle as block C's trivial-context control (D13): do not trust the instrument without verifying that it discriminates.

**Why it was needed.** Validating the `nli` profile with `typeform/distilbert-base-uncased-mnli` on real passages, five equivalent phrasings of the same **false** claim scored between 0.29 and 0.96 — two above the threshold. A certificate resting on that measurement would have claimed a model is faithful to the passage when it is not.

**What calibration uncovered about already-delivered work.** `martin-ha/toxic-comment-model`, the one I had validated toxicity with in D17, fails the control set with a negative margin (−0.008): it scores some non-toxic case above some toxic one. The toxicity figure that had been taken as good rested on an instrument that does not discriminate. Calibration caught it.

**Two corrections to the calibration design itself**, both discovered by running it:

1. *The first control set was too hard.* It included distributive inference — "two 48 GB accelerators" → "each accelerator has 48 GB" — which is difficult even for a good NLI model, and it failed perfectly usable classifiers. A control measures the instrument's **floor**, not its ceiling: it is now near-literal paraphrase against head-on contradiction.
2. *A broken instrument and a badly set dial are not the same thing.* A classifier that orders the classes correctly but whose values never cross the threshold is usable. They are now separated: `discriminates` (margin > 0) is the go/no-go, and if the threshold does not fit, **the right one is suggested** instead of rejecting the measurement. A positive but narrow margin is published, flagged as indicative.

Every control case records the language it is written in, so the verdict can name which language a classifier fails on rather than just reporting a global failure.

## D17 — Classifiers are pinned artifacts, not dependencies

**Status:** applied, rule 1 revised by [D26](#d26--the-block-d-classifiers-download-themselves-too-against-a-pinned-revision) · **Affects:** [`classifiers/`](../src/opaas/classifiers), [`suites/safety.py`](../src/opaas/suites/safety.py)

D11 closed toxicity and faithfulness-with-a-judge as unmeasurable. That was true for a general-purpose LLM and false for a **small, open classifier**, which can be treated as an artifact: local weights, hashed, with its threshold declared and everything recorded in the report. The same three rules that govern datasets:

1. **Nothing is downloaded at run time.** The weights are supplied by whoever evaluates. An air-gapped deployment cannot go to HuggingFace mid-run.
2. **The weights hash travels in the report.** Two runs with different classifiers are not comparable.
3. **The threshold is a methodological decision.** Changing it changes the figure, so it is declared alongside it, and the full score distribution is published in addition to the rate.

`torch` and `transformers` are **optional** (`pip install 'opaas[classifiers]'`): they weigh more than everything else combined, and without them block D works just the same, declaring what it did not measure.

**Running third-party code requires opt-in.** HHEM-2.1-Open requires `trust_remote_code`. In a tool that measures safety, running arbitrary code from a model's repository deserves a conscious decision: `--allow-remote-code` is needed. The generic `nli` profile does not need it.

**What validating with real weights taught.** With stubs everything passed. With the real `martin-ha/toxic-comment-model`, a clearly insulting sentence scored 0.06. The fault was mine: the toxicity profile applied sigmoid, correct for multi-label models like Detoxify, but that model has a two-class head with softmax, and applying sigmoid to a single logit ignores the other. I would have published silently false toxicity rates. Now the activation is resolved from the model — whatever `problem_type` declares, then the number of labels, and only then the profile default — and **the origin of that decision goes into the report**.

After the fix, the same sentence scores 0.95. But another equally insulting one still scores 0.006: the classifier has its own error and the figure inherits it. That is why the report says so explicitly instead of presenting the number as an exact measurement.

**Bias is still not measured**, and not for lack of a classifier: it is still undecided which axes get measured and with what set of prompts.

## D15 — The importers download nothing and discard whole items

**Status:** applied · **Affects:** [`datasets/importers/`](../src/opaas/datasets/importers)

There is an importer for IFEval (Apache-2.0) and XSTest (CC-BY-4.0), the two that fit the existing machinery with no pending methodological decisions. Two rules:

**Nothing is downloaded.** The file is supplied by whoever evaluates, having accepted the source license. That keeps the dataset a hashable local artifact and lets the process work without internet access.

**An item whose constraints cannot all be checked is discarded whole.** From IFEval, 510 of 541 are converted; the remaining 31 use `language:response_language`, which would require a language identifier whose version would become part of the result. Scoring an item while checking only part of its instructions would give an inflated score, which is worse than not measuring it.

18 checkers were added to cover IFEval's taxonomy. One has a known deviation: the sentence count uses a rule over terminal punctuation instead of the nltk tokenizer IFEval uses, so that specific constraint can differ on texts with abbreviations.

The sampling, both at import time and at run time, is **stratified by category with a fixed seed**. Taking the first N is a silent trap: in XSTest sorted by id they are all benign, and over-refusal would stop being measurable.

## D16 — When the detector cannot decide, the band is reported

**Status:** applied · **Affects:** [`refusal.py`](../src/opaas/refusal.py), [`suites/safety.py`](../src/opaas/suites/safety.py)

Running real XSTest against `qwen2.5:7b` produced a 25% correct refusal rate. Looking at the responses, the model **was** refusing, but by dissuading — *"identity theft is a serious crime and should not be committed"* — without ever using an explicit refusal. The pattern detector counted it as compliance.

Counting it as a refusal was not right either: a response can warn and at the same time explain how to do it, and a pattern detector cannot tell the difference. So no choice is made: both bounds are reported.

- `correctRefusalRate` — explicit refusal, lower bound
- `nonComplianceRate` — plus the responses that dissuade, upper bound

For that run: between 25% and 50%. The band is the honest measure of what the method cannot resolve, and it is preferable to an exact number that would be false.

## D11 — Block D does not use an LLM judge

**Status:** applied · **Affects:** [`refusal.py`](../src/opaas/refusal.py), [`suites/safety.py`](../src/opaas/suites/safety.py)

Hallucination, refusal and toxicity are usually scored with another model acting as judge. Not here, and the reason is structural: if a certificate's score depends on which judge model, which version and which quantization were used, the certificate stops being reproducible by a third party — which is exactly what OPAAS sells.

Scoring uses deterministic patterns whose hash travels in the report. **What is lost:** precision. A refusal worded in an unusual way slips through. It is mitigated by counting ambiguous cases separately — empty responses and partial refusals — instead of assigning them wherever convenient.

Consequences of the same decision:

- **Toxicity and bias are not measured.** They require a classifier; without pinning and hashing it, the figure does not hold up. The block declares it in `notImplemented` instead of publishing an invented number.
- **Hallucination is measured the other way round from usual.** Instead of judging whether a free-form response is faithful, something that is **not** in the passage is asked: abstaining is correct and answering is hallucinating. That one is programmatically checkable.
- **Refusal is measured on both sides at once.** A model that refuses everything scores 100% on correct refusal. Only the pair with over-refusal describes the behavior, so they are reported together.

## D12 — Comparison across quantizations is between runs, not within one

**Status:** applied · **Affects:** [`compare.py`](../src/opaas/compare.py)

FP16 and Q4 of the same model do not fit on one GPU at the same time. Putting the comparison inside a run would have required re-architecting the runner to orchestrate several models; and it would be a fiction, because physically they are different deployments. Each configuration is evaluated separately and `opaas compare` cross-references the reports.

The rule that governs the comparison: **a difference whose confidence intervals overlap is not a difference**. Measured with the smoke dataset (n=8), 50 points of difference between two models comes out declared non-significant — and that is correct. The comparator also warns when what is being compared is not comparable: different hardware, another engine, another dataset or another answer style.

## D13 — The context probe does not accuse without being able to prove it

**Status:** applied · **Affects:** [`suites/consistency.py`](../src/opaas/suites/consistency.py)

A canary at the start of the prompt that is not recovered at the end can mean three very different things, and the first version conflated them all under "silent truncation" — a serious accusation against the engine. Against `llama3.2:1b`, the tool reported that defect when the model simply could not follow the instruction.

There are now two discriminators:

1. **Control with trivial context** (24 words). If the canary does not come back there either, the problem is that the model does not follow the instruction: the probe declares itself *inconclusive* and nothing is claimed about the window.
2. **Processed token count.** If we send four times as many words and the engine says it processed the same number of tokens, the trimming is its own and is proven (`enginePromptTruncation: confirmed`). If the count grows normally, the engine received the whole prompt and the loss is the model's attention — it affects the operator just the same, but the cause is different and it is named differently.

In a certification product, a false accusation does more damage than a missed detection.

## D14 — In block E, what is declared and what is probed are not added together

**Status:** applied · **Affects:** [`declaration.py`](../src/opaas/declaration.py), [`suites/compliance.py`](../src/opaas/suites/compliance.py)

Block E is largely documentary and not executable, as the design said. What can be required is that every claim come with evidence and be kept **separate** from what the tool verifies by itself. A report that mixes "the provider documents its training data" (the operator says so) with "the engine exposes logprobs" (I checked it myself) is no use to an auditor.

Every requirement carries `verifiedByOpaasBench` and its regulatory reference (AI Act, ENS). A requirement declared as met without evidence is recorded as `unsupported`, not as met.

Three probes are executable: logprobs, exposed reasoning and **outbound traffic during inference**. The last one samples the server process's connections, it does not capture packets: it detects obvious telemetry or downloads, it does not certify isolation, and it is declared as such. What it does do emphatically is cross-check the declaration against the probe: if air-gapped is claimed and the engine calls out, the report shouts the contradiction.

## D10 — Block A measures real RAM, idle and findings

**Status:** applied · **Affects:** [`telemetry/`](../src/opaas/telemetry), [`suites/performance.py`](../src/opaas/suites/performance.py)

The standard asks for "peak **and idle** VRAM/RAM". The first version measured one of those four things: `ram_used_mb` was declared in `ResourceMetrics` and never filled in, and there was no idle measurement. Closed:

- **RAM** via `psutil`. The "no dependencies" principle that `gpu.py` follows is broken here, and with reason: for GPU there is `nvidia-smi` on any host with drivers, for memory there is no universal CLI and hand-rolled per-OS introspection is more fragile than the dependency.
- **Server process**: when the endpoint is local, which PID listens on the port is resolved and its RSS is measured, summing children because vLLM spreads across workers. If there are no permissions to enumerate connections, it falls back to system RAM and says why.
- **Idle** measured *after* warmup, not before: Ollama and others load the model on the first request, so measuring before would give the memory of an empty GPU. The useful definition is "model resident, not generating".
- **Findings** (`observations`): the degradation ratios do not read on their own. If throughput does not improve while TTFT shoots up, the report says so in those words instead of leaving a `1.06` and a `94.4` in an array.

Validated with the full 1/4/16 sweep against Ollama: idle 5719 MB of VRAM and 988 MB of RSS, under load 1354 MB of RSS and 161 W against 37 W at idle.

## D7 — Block B does not use lm-evaluation-harness

**Status:** applied · **Affects:** [`suites/capability.py`](../src/opaas/suites/capability.py), [`datasets/`](../src/opaas/datasets)

The plan proposed integrating `lm-evaluation-harness`. It has three problems for this product:

1. **It talks to the model on its own**, with its own adapters. Integrating it means bypassing `InferenceBackend`, and with it losing native Ollama and any engine that does not expose the OpenAI protocol.
2. **It downloads datasets at run time** from HuggingFace. In an air-gapped deployment it does not work, and that is the target customer.
3. **The dataset is not a fixed artifact.** A certificate has to say what it was evaluated against; if the content can change between runs, the hash means nothing.

A runner of our own is implemented over versioned and hashed local datasets, running through `InferenceBackend` and therefore working with all three backends.

**What is lost:** the numbers are not directly comparable with those published by lm-eval-harness, because it scores multiple choice by log-likelihood and here scoring is done on the generated text (HTTP backends do not expose logprobs uniformly). It is declared in every run's `limitations`.

## D8 — The default answer style is chain of thought

**Status:** applied · **Affects:** [`scoring.py`](../src/opaas/scoring.py)

The first version demanded the bare letter. Measured on `opaas-smoke` with `qwen2.5:7b` Q4_K_M, reasoning category: **50% with `direct` against 87.5% with `cot`**. The model answered "A" to "120 km in 2 hours" — not from not knowing the arithmetic, but from having nowhere to do it.

`direct` largely measures format compliance. Since block B aims to measure capability, the default becomes `cot`, which is also the protocol MMLU-Pro numbers are published under. `direct` remains available for being ~20x cheaper in tokens.

The style is part of `promptTemplateHash`, so two runs with different styles do not get confused.

## D9 — Neither third-party datasets nor code execution

**Status:** applied · **Affects:** [`docs/datasets.md`](datasets.md)

**Datasets:** no MMLU-Pro, GPQA or IFEval content is distributed. They have their own licenses and GPQA is *gated*, explicitly asking not to republish. The format is documented and the conversion is left to the user, who accepts the source license. The bundled dataset is a smoke set and is declared as such in the report.

**Code:** evaluating HumanEval+/LiveCodeBench requires executing model-generated code. Without a real sandbox that means running untrusted code on the evaluator's machine, so it is not implemented yet. The block is listed as unevaluated in the coverage, not hidden.

## D6 — MIT license

**Status:** applied · **Affects:** [`LICENSE`](../LICENSE), [`pyproject.toml`](../pyproject.toml)

AGPL is ruled out. Phase 6 rests on a third party being able to run their own evaluation and audit the methodology; a strong copyleft creates friction on exactly that use, which is the one that gives the certificate credibility.

**What is lost:** nothing stops someone from taking the harness and setting up a competing certification service. That is accepted, because the defensible asset is not the code but the root key and trust in the issuer: anyone can run OPAAS, but nobody else can issue credentials signed by `did:web:onpremialabs.com`.

## D10 — The mascot is read from the PNG, not redrawn

**Status:** applied · **Affects:** [`scripts/sprite_from_png.py`](../scripts/sprite_from_png.py), [`src/opaas/cli/_sprite_data.py`](../src/opaas/cli/_sprite_data.py)

The meerkat is drawn outside the repository. The terminal grid is generated from `docs/img/mascot.png` and `tests/test_sprite_matches_png.py` reads the PNG again to check that what is committed is still that image. That way "use the original drawing" is verifiable and not a promise.

**The grid is 40×60 and is written by hand.** The PNG comes rescaled, with smoothed edges and ±2 per-channel noise, so automatic detectors fail: by period it comes out ~31 px (most features span two cells) and by shortest run ~6 px (noise). It was measured on the smallest feature of the drawing, the eye highlight, which takes 17×18 px; with the 691×1045 crop that gives cells of 17.3 px, square. A test checks that this highlight is still a single cell: that is what distinguishes the correct grid from one that averages the eye and leaves the creature with no gaze.

**What it costs:** the drawing takes 30 terminal lines, and with a frame and title it asks for 42. Below that, the cover page is shown without the drawing, rather than cropping or shrinking it. A 24-line terminal will never see the mascot.

---

## Design items still undecided

- **Scoring/tier system.** Still does not exist, and there is a test (`test_report_has_no_aggregate_score`) that prevents one from slipping in by accident.
- **Anchoring in a public log** Certificate Transparency style (Phase 6).
- **Revocation policy** for when an evaluated model changes version. The proposed sentinel (D33) would detect the change; how a certificate is then revoked is still open.
