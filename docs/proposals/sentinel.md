# Proposal: the sentinel, `opaas watch`

**Status:** implemented (D36), with the revisions recorded there: fixed canary prompts instead of items drawn from the dataset, determinism checked by the canaries themselves, and the open questions settled (proposed sample sizes, results stay local, exit code 3).

## The problem

A certificate is valid for 90 days (D28). It can become false the day after it is issued: someone updates the engine, pulls a new build of the model under the same tag, changes the quantization or upgrades the GPU driver. Today nobody finds out until the next certification. The badge stays green and the printed certificate keeps describing a deployment that no longer exists.

The frameworks ask for exactly this gap to be covered:

- **AI Act art. 26(5):** the deployer monitors the operation of the system.
- **NIST AI RMF MEASURE 2.4:** functionality and behaviour monitored in production.
- **ENS op.mon:** monitoring of the system.

See [standards-mapping.md](../standards-mapping.md).

## What the sentinel is, and what it is not

**It answers one question: does the signed certificate still describe this deployment?** The answer is one of four states:

| State | Meaning |
|---|---|
| `current` | Every check matches the certified baseline |
| `drifted` | Something changed. Which check, and the evidence, are in the result |
| `inconclusive` | It could not tell: endpoint down, a field the engine does not expose, too few samples |
| `expired` | The credential is past its validity date. Nothing else is checked |

**It is not a certificate.** It never signs anything, and it never produces a figure that could be quoted as a measurement of the model. It compares against the signed baseline and says whether the baseline still holds.

**It is not production monitoring.** It never reads user traffic. It sends a small, fixed set of synthetic requests, the same ones every time. Real-time observability of production (engine metrics, request logs) is a job for existing tools: vLLM's Prometheus metrics, the operator's logging stack. OPAAS can accept their output as declared evidence in block E, but it does not reimplement them.

**It is not continuous load.** It runs at concurrency 1, with a bounded number of requests. It must be safe to schedule against production.

## The three checks

Cheapest first. The first check that reports `drifted` ends the run, so a changed engine is reported in seconds without sending any inference request.

### 1. Deployment fingerprint (no inference)

The identity of what is serving. It is compared field by field with what the certified report recorded:

| Field | Source | If the engine does not expose it |
|---|---|---|
| Engine and version | Adapter metadata (`engineVersion`) | `inconclusive` for this field |
| Model id | Request | — |
| Model digest | Ollama publishes one per model; vLLM does not | Declared unknown, as `weightsHash` already is |
| Quantization | Adapter metadata | Declared unknown |
| Served context window | `context_window()` | Declared unknown (`None` means unknown, never unlimited) |
| GPU model and driver version | `nvidia-smi` | Declared unavailable, as GPU telemetry already is |
| Prompt mode and chat template | Adapter metadata | — |

**A field that is unknown both at certification and now is not a match.** It is reported as *not comparable*. A deployment whose engine exposes nothing can never be `current` on the fingerprint alone: the result says so, and the behaviour checks have to carry it.

**Prerequisite change to `run`: applied (D34).** `configHash` identifies OPAAS's own run configuration, not the deployment, so `run` now records a signed `deploymentFingerprint`. It narrows the table above to what the deployment reports about itself: engine and version, model id and digest, chat template hash, GPUs and driver. Quantization, the served window and the prompt mode are left out, because they are declared by the operator or chosen by OPAAS, and they already live in the configuration.

### 2. Exact-output canaries (serial, temperature 0)

A fixed set of prompts, chosen by seed from the certified dataset (proposed: 20), run serially at temperature 0. The hash of each output is compared with the hash recorded at certification.

This is the most sensitive check there is. If the stack was deterministic when certified, then the same prompts producing different text means something underneath changed: weights, template, engine kernels, sampling defaults. It detects that even when every fingerprint field claims nothing changed.

**It only applies if block C measured 100% serial determinism at certification.** Otherwise outputs are expected to vary, a difference proves nothing, and the check is disabled and declared, never silently skipped. The certified report records the canary ids and output hashes in a `sentinelBaseline` section, which is signed.

### 3. Statistical drift (small samples, coarse on purpose)

These run only when the first two are inconclusive or disabled, or when asked for explicitly with `--full`.

- **Latency and throughput** at concurrency 1, on one prompt length (proposed: 20 requests). The certified report would also record a **distribution-free 95% interval for the TTFT median** and for tokens per second. Order statistics need no assumptions about the distribution, and block A records only percentiles today. Drift is reported only when the sentinel's interval does not overlap the certified one, the same rule as `compare` (invariant 11).
- **Behaviour.** A fixed sample of refusal items and capability items (proposed: 40 + 40), each rate with a Wilson interval, under the same overlap rule.
- **Egress.** The network probe again, during the canaries. If the certificate recorded `pass` and the probe now sees outbound connections, that is `drifted`.

**What this check cannot do, stated plainly:** at 40 items an interval spans about ±15 points. The sentinel catches gross regressions, such as a model that stopped refusing or a latency that tripled. It does not catch subtle ones. That is what re-certification is for, and the result says so.

## Where the result goes

- **A result file** (`watch.json`) with the state, each check, the evidence for a `drifted` and the reason for an `inconclusive`.
- **Optionally, a hash-chained log** (`--log watch.jsonl`), built like `transparency.py`. A history of sentinel results that cannot be edited without detection is the monitoring evidence AI Act art. 26(5) asks a deployer to keep, and it can be cited in block E at the next certification.
- **Exit codes for schedulers:** `0` current, `1` drifted or expired, `2` user error, `3` inconclusive. The `3` departs from the CLI's 0/1/2 convention on purpose: a scheduler has to tell "the deployment changed" from "the check could not run", and folding both into `1` would page someone for a network blip.

## What it deliberately does not do

- **It never holds the signing key.** It runs unattended, on a schedule, next to production. The Ed25519 root key is the whole root of trust ([key-custody.md](../key-custody.md)) and must never sit on a machine that runs cron jobs. That is also why the sentinel cannot sign its own results.
- **It does not revoke the certificate.** A signed credential cannot be un-signed. The sentinel supplies the *trigger* for the revocation policy that [decisions.md](../decisions.md) still lists as undecided. The *mechanism*, such as a W3C status list published with the site, is a separate decision.
- **It does not ship a daemon.** `opaas watch` is a one-shot command. Scheduling it is cron, a systemd timer or Windows Task Scheduler, just as deploying the site is the operator's own step (D19).
- **It never reads user prompts or responses.** Only its own synthetic requests.

## Usage

```bash
opaas watch --credential credential.json --report report.json --base-url http://gpu-01:8000 --log watch.jsonl
```

It verifies the credential first, with the same key resolution as `verify`. It then reloads the backend from the certified report's own effective configuration, so the canaries go through the same prompt mode and context window that were certified.

## Implementation

| Piece | Where |
|---|---|
| `deploymentFingerprint` and `sentinelBaseline` in the report | `report.py`, filled by `runner.py` and block C. **This changes what `run` writes**, so it lands first and on its own |
| Model digest from Ollama; driver version from `nvidia-smi` | `adapters/ollama.py`, `telemetry/gpu.py` |
| Distribution-free interval for the median | `stats.py`, next to `wilson_interval` |
| The three checks and the states | New `sentinel.py`; it talks only to `InferenceBackend` (invariant 13) |
| Hash-chained log | Reuses the chain from `transparency.py` |
| Command | `cli/main.py`, `watch` |

## What it costs

- **One more section in every report**, and a change to what `run` writes. Reports certified before it have no baseline, and the sentinel can only check their fingerprint loosely and say so.
- **Load on production:** about 20 serial requests for the canaries, about 100 with `--full`. It is small, but it is not zero, and the scheduling interval is the operator's call.
- **False reassurance.** A `current` from a deployment that exposes no fingerprint and was not deterministic rests on coarse statistics only. The result has to say which checks actually carried it, or `current` will be read as more than it is.

## Open questions for review

1. **Canary count** (20) and **sample sizes** (20 latency requests, 40 + 40 items). More catches more, at more load on production.
2. **Should a `drifted` result be publishable**, for example as a flag on the static site next to the certificate, or stay local to the operator until the revocation policy is decided?
3. **Exit code 3 for `inconclusive`:** accept the departure from the CLI convention, or report inconclusive as `0` with a warning?
