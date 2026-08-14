# Certifying yourself without sending us your model

If your weights cannot leave your network — the norm in regulated on-prem settings — they do not have to. You run the evaluation and send us the report.

This document says how to do it and, more importantly, **what onpremIALabs can and cannot verify about a report it did not produce**. That distinction is what makes the certificate honest.

---

## The flow

**1. You run the evaluation in your environment.**

```bash
opaas-bench run --model <your-model> --backend vllm --base-url http://your-gpu:8000/v1 --suite all --dataset ifeval --declaration declaration.json --quantization "<yours>" --weights-path /models/your-model --output report.json
```

`--weights-path` matters: it hashes the weights artifact. Without it, the certificate cannot state *which* model was evaluated, only what it was called.

**2. You send us `report.json`.** That is all. It contains none of your weights, none of your prompts and none of your data: it is metrics, hashes and the compliance declaration you filled in. It is worth reviewing before sending — it is a document that will be published — and in particular `configuration.effectiveConfig`, although API keys already come out redacted.

**3. We verify what is verifiable, sign and publish.**

```bash
opaas-bench sign --input report.json --key <root-key> --out credential.json --log ledger.jsonl
```

**4. You verify, and so can anyone, without depending on us.**

```bash
opaas-bench verify --credential credential.json --report report.json --log ledger.jsonl
```

---

## What the signature guarantees, exactly

This is the part a buyer of yours has a right to understand.

**What the signature does guarantee:**

- That **that specific report** has not been altered since it was signed. One byte different and verification fails.
- That it was issued by onpremIALabs and not by a third party.
- That it was issued within the process, if it appears in the transparency log.
- That the certificate expires, and when.

**What the signature does NOT guarantee:**

- **That the measurements are true.** onpremIALabs was not standing in front of your GPU. A self-run report is signed as *"this is what the operator declares having measured"*, not as *"we have checked this"*.
- **That the evaluated model is the deployed one.** Only if you supplied `--weights-path` does the certificate anchor a weights hash; then anyone can recompute it over the model in production and check that it matches. Without that, the link is the name, which is not a link.
- **That the measured configuration is the production one.** It is trivial to evaluate at a concurrency you do not use.

That is why `configuration.configHash` and `model.weightsHash` are in the credential: they are what lets a third party check that the deployment in front of them is the certified one. **A certificate with no weights hash is worth considerably less**, and it is better that you know that before your customer does.

---

## How to make your report worth more

**Supply the weights hash.** `--weights-path`. It is the difference between certifying a name and certifying an artifact.

**Use a real and sufficient dataset.** With 30 items the confidence intervals distinguish nothing. Aim for 300–500 per category; the report warns when the sample is small.

**Cover all five blocks.** `--suite all`. A partial evaluation is certified just the same, but the badge says so and the certificate lists which blocks were not measured.

**Fill in the declaration with evidence.** A requirement declared as met without evidence is recorded as `unsupported`, which is worse than honestly declaring it `partial`.

**Keep a copy of the transparency log** the day you receive your certificate. It is your proof against a later rewrite.

---

## Reproducing it yourself

The whole harness is MIT and published. Anyone can repeat the evaluation:

```bash
opaas-bench verify --credential credential.json --report report.json
```

```bash
sha256sum report.json
```

And compare your deployment against other configurations, or against a cloud reference for the same model:

```bash
opaas-bench compare --report report.json --report cloud-baseline.json
```

If you repeat the run and get different figures from the certified ones, that is not necessarily fraud: the report declares its sources of variance — determinism, sampling, hardware — and the comparator will tell you whether the difference is statistically supportable or noise.
