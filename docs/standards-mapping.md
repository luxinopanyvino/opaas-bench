# How OPAAS maps to ISO/IEC 42001, the AI Act, the ENS and the NIST AI RMF

This document is for an operator, an auditor or a compliance officer who wants to know what an OPAAS certificate can be used as evidence **for**, and, more importantly, what it cannot.

**OPAAS is not a standard and does not certify compliance with any of these frameworks.** It is a methodology published by onpremIALabs ([methodology.md](methodology.md)), and no standards body or notified body has endorsed it. All four frameworks require things OPAAS does not touch: an organisation's policies, roles, risk management and human oversight. What OPAAS supplies is **technical evidence**: reproducible measurements of a specific deployment, signed and verifiable by a third party. A management system, a conformity assessment or an ENS audit can cite that evidence. It cannot replace any of them.

How strongly OPAAS fits each clause:

- **Strong:** an OPAAS run is direct evidence for the clause.
- **Partial:** it supplies part of the evidence, or evidence for a narrower claim than the clause makes.
- **Not covered:** OPAAS has nothing to offer there.

**Sources.** The AI Act references were checked against the regulation's text (Regulation (EU) 2024/1689), the ENS measure titles against the consolidated Real Decreto 311/2022 on boe.es, and the NIST subcategories against NIST AI 100-1. **ISO/IEC 42001 is not freely available**: its references below come from the published structure of Annex A and must be checked against a purchased copy before they are cited formally.

---

## ISO/IEC 42001:2023 — AI management system

ISO/IEC 42001 certifies an **organisation's** management system, not a model. OPAAS fits in as one of the measurement tools that system relies on.

| Clause / control | What OPAAS supplies | Fit |
|---|---|---|
| 9.1 Monitoring, measurement, analysis and evaluation | Reproducible, hash-pinned measurements; re-certification every 90 days (D28) | Strong |
| A.6.2.4 AI system verification and validation | Blocks B, C and D, with 95% intervals and declared limitations | Strong |
| A.6.2.6 AI system operation and monitoring | Measures the deployment as it runs, not the model in the abstract | Partial — a point-in-time check, not continuous monitoring |
| A.6.2.7 AI system technical documentation | The signed report and its credential | Strong |
| A.7 Data for AI systems (provenance) | Datasets, configuration and, with `--weights-path`, weights identified by hash | Partial — OPAAS's own test data, not the training data |
| A.10 Third-party and customer relationships | Evaluating a supplier's model on your own hardware before adopting it | Partial |
| Clauses 4–8 and A.2–A.5 (context, leadership, planning, policies, impact assessment) | Nothing | Not covered |

---

## AI Act — Regulation (EU) 2024/1689

Most obligations fall on the **provider** of a high-risk system or a general-purpose model. An organisation running a model on its own premises is usually a **deployer** (art. 26), and becomes a provider only if it substantially modifies the system or puts it on the market under its own name. Several of the provider obligations below matter to a deployer as *what the provider has to give you*.

| Article | What OPAAS supplies | Fit |
|---|---|---|
| **Art. 15** Accuracy, robustness and cybersecurity | Block B (accuracy with intervals) and block C (robustness, determinism, real context window) | **Strong** for accuracy and robustness |
| Art. 15(5) Resilience against attempts to exploit vulnerabilities | No prompt-injection or adversarial testing | **Not covered** |
| Art. 13(3)(b)(ii) Declared level of accuracy and its metrics | The figures a provider declares can be checked on the deployer's own stack | Strong |
| Art. 13(3)(b)(iv) Capabilities to provide information that explains the output | Block E probes whether the engine exposes logprobs or reasoning | Partial — availability, not quality, of explanation |
| Art. 9(6)–(8) Testing against previously defined metrics | A full, reproducible run with fixed datasets and templates | Strong |
| Art. 11 and annex IV Technical documentation | Metrics, test results, signed report | Strong as one annex IV element |
| Art. 12 and art. 26(6) Record-keeping and log retention | Block E asks the operator to declare it, with evidence | Partial — declared, not verified |
| Art. 26(5) Deployers monitor the operation of the system | A periodic measurement of the deployer's own installation | Partial |
| Art. 53(1)(b), (d) and annex XII Information from general-purpose model providers | Block E asks for training-data, licence and provenance documentation | Partial — declared, not verified |
| Art. 10 Data governance | Nothing beyond the operator's declaration | Not covered |
| Art. 14 Human oversight | Nothing | Not covered |
| Art. 43 Conformity assessment | Can be cited as test evidence; does not replace the procedure or a notified body | — |

---

## ENS — Real Decreto 311/2022, annex II

The ENS has no measures specific to AI, but an LLM deployment is an information system and several measures apply to it directly. The titles below are the official ones.

| Measure | What OPAAS supplies | Fit |
|---|---|---|
| **mp.sw.2** Aceptación y puesta en servicio | Evaluating the deployment before it goes into production, with a signed record | **Strong** |
| **op.pl.4** Dimensionamiento / gestión de la capacidad | Block A: throughput, latency and degradation across concurrency, VRAM and RAM | **Strong** |
| op.exp.1 Inventario de activos | Model, weights and configuration identified by hash | Strong |
| op.mon.2 Sistema de métricas | Reproducible measurements comparable over time (`opaas compare`) | Partial |
| mp.com.1 Perímetro seguro | Outbound connection probe during inference; cross-checked against the air-gapped claim | Partial — connection sampling, not packet capture |
| op.exp.8 Registro de la actividad | Block E asks the operator to declare inference logging | Partial — declared, not verified |
| op.ext.1 Contratación y acuerdos de nivel de servicio | A third-party, verifiable figure to put in a contract | Partial |

---

## NIST AI RMF 1.0 (NIST AI 100-1)

OPAAS sits almost entirely in the **MEASURE** function. GOVERN, MAP and MANAGE are organisational.

| Subcategory | What OPAAS supplies | Fit |
|---|---|---|
| MEASURE 1.1 Metrics selected; what will not or cannot be measured is documented | Unmeasured dimensions are declared in `limitations` and `notImplemented`, never zeroed | **Strong** — it is OPAAS's first principle |
| MEASURE 1.3 Independent assessors involved | A certificate any third party can verify offline | Strong |
| MEASURE 2.1 Test sets, metrics and tools documented | Everything identified by hash | Strong |
| MEASURE 2.3 Performance demonstrated for conditions similar to deployment | Measures the real deployment, on its hardware, through its engine | **Strong** |
| MEASURE 2.5 Valid and reliable; limits of generalisability documented | Confidence intervals; limitations printed on the certificate | Strong |
| MEASURE 2.6 Evaluated for safety risks | Block D: refusal of harmful requests together with over-refusal | Strong for content safety |
| MEASURE 2.12 Environmental impact | Energy per million tokens | Partial — GPU only, a lower bound |
| MEASURE 2.4 Monitored in production | Re-certification every 90 days | Partial — periodic, not continuous |
| MEASURE 2.7 Security and resilience | No adversarial testing | **Not covered** |
| MEASURE 2.11 Fairness and bias | Bias is not measured | **Not covered** |
| GOVERN, MAP, MANAGE | Nothing | Not covered |

---

## Gaps common to several frameworks

In order of how much each one would close:

1. **Adversarial security testing** (prompt injection, jailbreaks). One block would address AI Act art. 15(5), NIST MEASURE 2.7 and the ENS protection measures at once. It is the largest gap.
2. **Bias.** NIST MEASURE 2.11. Already listed as undecided in [methodology.md](methodology.md): the open question is which axes and which prompts, not the tooling.
3. **Per-block references.** Today only block E's catalogue carries regulatory references. Blocks A–D are where the strongest fits are (art. 15, mp.sw.2, op.pl.4, MEASURE 2.3), and the report does not say so.

## What must not be claimed

- That an OPAAS certificate makes a deployment **compliant** with the AI Act, the ENS or any standard. It is evidence for specific clauses, listed above.
- That OPAAS is **ISO/IEC 42001 certified** or that using it certifies an organisation. Only an accredited certification body certifies a management system.
- That block E's declared items were **verified**. Those items are the operator's claims; only the probed ones are checked by OPAAS (D14).
