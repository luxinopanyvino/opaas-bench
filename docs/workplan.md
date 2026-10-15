# Work plan: from model certification to auditable agents

**Status:** implemented, phases 0–7 (D40–D52). The [status table](#status) says what each phase delivered and what was left out. This plan splits OPAAS into modules (`discover`, `smith`, `swarm`, `sentinel`, `inquest`, `card`) around one shared evidence core. Each phase lands as its own pull requests with `D<n>` entries. Nothing here changes an invariant in [CLAUDE.md](../CLAUDE.md); where a module needs a new one, it is listed under [New invariants](#new-invariants).

---

## 1. Starting point: what the paper establishes

*Auditable Agents* (Nian, Yuan, Zhang et al., arXiv:2604.05485v4, Sep 2026) is a position paper. Its claim: **no agent system can be accountable without auditability**. It separates three terms. Accountability is the goal: deciding compliance and assigning responsibility. Auditability is the system property that makes accountability possible. Auditing is the process of reconstructing behaviour from trustworthy evidence.

### The audit verdict and its five dimensions

A defensible audit yields a verdict with five slots. Each slot needs one property of the record:

| Slot | Auditor question | Dimension | Existence metric | Diagnostic metric |
|---|---|---|---|---|
| the action | What happened? | Action recoverability | **ACR**, the share of policy-relevant actions recorded; **RF**, the share of required fields recoverable per action | — |
| the context | In what context? | Lifecycle coverage | **LPC**, the share of execution phases observed; **GB**, the gap burden | — |
| the policy verdict | Did it comply? | Policy checkability | **SPDR**, the share of policies decidable from the record | ADL, detection latency |
| the responsible party | Who was responsible? | Responsibility attribution | **AC**, the share of actions with a full responsibility chain | ACD, chain depth |
| the integrity guarantee | Can the evidence be trusted? | Evidence integrity | **IS**: none < append-only < hash-chained < signed | VC, verification cost |

Two results matter most for OPAAS:

- **Policy verdicts have three values: comply, violate, or undecidable.** Proposition 1 shows that one field missing from the record schema makes a whole class of policies undecidable, however many events were logged. *The schema decides what can be audited.*
- **No single vantage point covers all five dimensions.** Each mechanism class sees something the others cannot:
  - **detect**, before deployment: sees code, configuration and permissions, but not realised behaviour;
  - **enforce**, at runtime: sees live actions and can emit protected records, but only inside its boundary;
  - **recover**, post hoc: works on surviving records, but cannot recreate fields that were never captured.

The paper also proposes an **Auditability Card** (questions Q1–Q5, one per dimension, and Q6 on what happens when logs are missing) and six open problems, OP1–OP6.

### Where the paper is weak, and where OPAAS can contribute

| Paper limitation | OPAAS's opening |
|---|---|
| "No end-to-end audit": there is no deployment with **ground-truth violations** on which all five dimensions are measured together | **`smith`**: scripted scenarios with instrumented mock tools *are* the ground truth, so all five metrics become exactly computable on one system |
| All three evidence blocks come from the authors' own tools | OPAAS is independent, open and reproducible: pinned artifacts, hashes, signed reports |
| Thresholds are "deployment-specific" with no calibration guidance | OPAAS never invents thresholds (invariant 1). The operator declares them; OPAAS measures, and the two are compared, never merged (invariant 9) |
| Structural policies only; semantic ones are open (OP4) | The same scope, stated rather than hidden: semantic policies are declared out of scope, with no LLM judge (invariant 3) |
| The safety evaluations it reviews (ToolEmu, R-Judge, Agent-SafetyBench) score with LLMs | Deterministic oracles only |
| Evidence integrity is "nearly absent" across the field | OPAAS already operates at the top level: Ed25519-signed, JCS-canonical, hash-chained (D1, D2, D21) |

**What is not adopted:** implicit execution tracing (IET). It watermarks the token distribution during generation. OPAAS evaluates deployments it does not control and never alters the model's output (D27: an auditor that reconfigures the system certifies a deployment it invented).

---

## 2. Target architecture

```
opaas/
├── core/        evidence + trust: canonical, hashing, credential, transparency, stats,
│                report schema, AuditEvent schema, structural policy engine, metrics (ACR…IS)
├── bench/       today's model certification: blocks A–E, datasets, classifiers
├── discover/    detect · deterministic inventory of the environment and its auditability gaps
├── smith/       detect + ground truth · fixed scenarios, mock tools, deterministic oracles
├── swarm/       multi-model / multi-agent flows · topology, delegation, cascading failure
├── sentinel/    enforce · scheduled checks (today) + real-time observe proxy, signed records
├── inquest/     recover · post-hoc audit of records → three-valued verdicts
├── card/        Auditability Card + deployment certificate, generated from the evidence
└── cli/         one entry point: opaas <module> <command>
```

**One evidence core, many producers.** Every module emits and consumes the same `AuditEvent` records:

| Field | Content |
|---|---|
| `action` | Kind: tool call, model call, delegation, approval… |
| `args` / `output` | A digest by default; the value itself only if the policy needs it (see N4) |
| `phase` | `attempt`, `retry`, `fallback`, `block`, `pending`, `approval`, `handoff` |
| `actor_chain` | user → agent → skill → tool → service |
| `policy_decisions` | The verdicts applied to the action |
| `ts` | Timestamp |

They are serialized through `canonical.py`, hash-chained, and signed in batches. The paper's Proposition 1 makes this schema the most consequential design decision in the whole plan, so it is settled first and in writing.

**Results are bound to an environment.** Every agent or model result carries the environment fingerprint it was measured under: the D34 deployment fingerprint, extended to tools, MCP servers and skills. A result says nothing outside that fingerprint, and `sentinel` detects when the environment leaves it.

---

## 3. Modules

### `discover`: deterministic environment analysis (detect)

**Question:** what is running here, and what can and cannot be audited about it?

1. **Inventory.** Already in progress on `feat/environment-discovery`, against a real Ollama: engines, versions, ports, bind scope, served models and digests, GPUs and driver, endpoints that answer without credentials, mirror endpoints merged. To extend:
   - agent runtimes and MCP servers, found by listening process and by config files **the operator names** (it never crawls the disk);
   - tool and skill manifests, hashed;
   - container images by digest, when Docker or Podman is reachable.
2. **Auditability gap scan (OP1-lite).** Deterministic rules over the configuration the operator names:
   - Do tool calls go through a logging layer?
   - What integrity level do the agent's logs have (none, append-only, chained, signed)?
   - Are tools free-form shell or HTTP?
   - Are skills version-pinned and signed (OP2)?
   - Are approvals represented as events?

   The rule set is versioned and hashed like the refusal patterns (D11). A finding is a *gap*, never a predicted violation, as the paper insists.
3. **Output:** a signed inventory, plus a gap list mapped to the five dimensions and to the Auditability Card's Q1–Q6.

**Precision:** read-only GETs and process metadata. Command lines are never read (they can carry secrets). What it cannot see (remote services, other users' processes, unreadable configs) is listed as *not observed*, never guessed.

### `smith`: fixed scenarios with deterministic oracles (detect + ground truth)

**Question:** how does this agent behave, and how auditable is it, in scenarios whose correct outcome is known?

A **scenario** is a hashed YAML file. It holds:
- an initial state;
- **mock tools** with deterministic responses, which are the *instrumented oracle*: every call is recorded with full fields;
- the user task;
- optional **adversarial content** planted in tool outputs: injection canaries, poisoned documents, permission bait;
- **expected end state** and **forbidden actions**, as structural predicates.

**Measured, per scenario set, each with a Wilson interval:**

| Behaviour | Auditability: the agent's own record against the mock-tool ground truth |
|---|---|
| Task completed, checked by end-state predicate | ACR: did the agent's log record every action the oracle saw? |
| Forbidden or undeclared tool attempted | RF: with every required field? |
| Injection resisted (canary absent from side effects) | LPC: are retries, blocks and approvals visible as phases? |
| Schema-valid tool calls | SPDR: how many of the scenario's policies are decidable from the agent's record alone? |
| Step budget exceeded (loops) | AC: is the actor chain complete? |
| Destructive action without confirmation | IS: the integrity level of the agent's log |

This is the paper's missing end-to-end audit. The ground truth comes from instrumentation, never from a model.

**Agent adapters** follow the `InferenceBackend` pattern, so suites never special-case a framework (invariant 13):
- (a) an HTTP agent endpoint;
- (b) **bare model mode**, where OPAAS runs the tool loop itself, so any model behind an engine can be tested for tool use;
- (c) an MCP client.

**Repeats:** each scenario runs N times. Variation across runs is reported as instability, as block C does for determinism, and never averaged away.

### `swarm`: flows of several models and agents

**Question:** does the whole flow behave as declared, and when it fails, can the failure be attributed?

- **Topology, declared against observed.** The operator declares the flow (agent A → agent B → model X; tool T). OPAAS observes the real calls through the `sentinel` proxy with W3C Trace Context headers. Edges that are undeclared, or declared but never used, are findings (D14: declared and observed are never added).
- **Responsibility attribution:** AC and ACD over delegation chains, and subgraph recovery when an outcome comes from joint behaviour.
- **Fault injection,** deterministic and scripted: one model slow, erroring or returning malformed tool calls. Is the failure contained, retried within budget, or cascaded? Each case is a lifecycle-coverage test, because the retry and fallback phases must show up in the record.
- **Per-edge figures:** latency, error rate and token cost per edge, with intervals. There is no flow-level score (invariant 1).

### `sentinel`: real time (enforce)

**Today:** `opaas watch`, a scheduled, one-shot check that the certificate still holds (D36).

**Added: an observe proxy.** It is an OpenAI-compatible reverse proxy, extendable to MCP, in front of engines and tools. For every call it emits signed, hash-chained `AuditEvent`s, and it evaluates **structural policies in real time**. Delivered in three steps:

1. **observe**: record and alert only. Nothing in the traffic changes.
2. **gate** (opt-in): allow, block or pending, with a human approval queue. This is the paper's Aegis-style mediation.
3. **inventory watch**: `discover` re-run on a schedule, alerting on new uncertified models, engine upgrades, widened bind scope, or changed skills.

**Precision and cost:**
- The proxy's added latency is measured and published: p50, p95 and p99. The paper's reference point is 8.3 ms median.
- Digest-only recording by default, with field-level redaction and an explicit retention policy (N4).
- No content judging. A policy on content must be structural (a regex, a schema, a data-class tag), or it is out of scope.

### `inquest`: post-hoc audit (recover)

**Question:** given the records that exist, what happened, did it comply, and who is responsible?

- **Inputs:** OPAAS sentinel logs, OpenTelemetry GenAI spans, and framework traces (one importer each, following D15: whole records or none).
- **Integrity first.** It verifies chains and signatures and reports the IS level actually found.
- It computes the five-dimension metrics and evaluates the operator's structural policy set. **The verdict is three-valued.** Every *undecidable* names the missing field that made it so (Proposition 1). That tells the operator exactly what to start logging.
- It never fills a gap by inference. What was not recorded is reported as missing.

### `card`: the Auditability Card and the deployment certificate

- **Auditability Card:** Q1–Q6 filled automatically from the `discover`, `smith`, `sentinel` and `inquest` evidence. Each answer links to the evidence and the hash it rests on, and Q6 is answered from `inquest` runs on degraded logs.
- **Deployment certificate:** the multi-model, multi-agent certificate drafted in `runs/mockup/`. It is backed by a signed manifest listing the hash of every report, keeping invariant 4 per report.

---

## 4. New invariants

- **N1. Verdicts are three-valued.** Comply, violate or undecidable. An undecidable is never rounded to either side, and it always names the missing field.
- **N2. Policies are structural.** A predicate over recorded fields. Semantic policies (OP4) are declared out of scope until a deterministic decision procedure exists.
- **N3. Ground truth comes from instrumentation, never from a model.** Mock tools, end-state predicates and recorded events (extends invariant 3).
- **N4. Evidence is minimised by default.** Arguments and outputs are recorded as digests unless a policy needs the value. What is kept, and for how long, is declared on the card.
- **N5. Every environment-dependent result is bound to an environment fingerprint**, and is void outside it.
- **N6. Thresholds are the operator's.** OPAAS reports metrics and the operator's declared thresholds side by side, and never sets thresholds itself.

---

## 5. Phases

| Phase | Deliverable | Depends on | Size |
|---|---|---|---|
| **0. Core** | Module boundaries; `AuditEvent` schema (D-entry, reviewed first); structural policy language; metrics library (ACR…VC) with tests against hand-computed cases | — | M |
| **1. discover** | Finish the inventory branch; add agent/MCP/skill inventory; gap scan; signed inventory; sentinel inventory check | 0 (schema only) | M |
| **2. smith** | Scenario format; mock-tool oracle; bare-model tool loop; HTTP agent adapter; behaviour and auditability metrics; 20 reference scenarios | 0 | L |
| **3. sentinel observe** | Reverse proxy; signed hash-chained events; real-time structural policies; overhead benchmark | 0, 1 | L |
| **4. inquest** | Integrity verification; OTel and sentinel importers; three-valued verdicts with missing-field explanations | 0, 3 | M |
| **5. swarm** | Topology declared against observed; delegation attribution; scripted fault injection | 2, 3 | L |
| **6. card** | Auto-generated Auditability Card; multi-model, multi-agent deployment certificate | all | M |
| **7. sentinel gate** | Allow, block or pending with an approval queue (opt-in) | 3 | M |
| **8. System audit** (accepted, D60) | The system as the audited subject; one measurement format for every suite; `opaas audit`; one system certificate. Phases S0–S4 in [`system-audit.md`](system-audit.md) | all | L |

### Status

| Phase | Delivered | Left out, and why |
|---|---|---|
| **0. Core** | `core/` split out of the flat package (D41). `AuditEvent` schema and the chained, sealed `EventLog` (D42). Structural policy language with Kleene three-valued verdicts (D43). ACR…VC with hand-computed tests (D44). Generic signed evidence documents (D49). | — |
| **1. discover** | Engine inventory (D40). MCP configs, tool manifests, skills, containers by digest, agent runtimes by process name. Gap rules G01–G09, versioned and hashed. Signed inventory via `opaas evidence sign`. `opaas sentinel inventory` and `opaas watch --inventory` (D45). | Agent runtimes are listed, never probed. Skill signatures are reported present, not verified. |
| **2. smith** | JSON scenario format, hashed and validated. Mock-tool oracle. `bare-model` and `http` adapters. Behaviour and auditability metrics, including the misleading-record check. Repeats and instability. 20 reference scenarios, each replayed by a correct solution in the tests (D47). | **YAML** (JSON instead, D47). **MCP adapter** (declared in every report). |
| **3. sentinel observe** | OpenAI/Ollama/MCP-over-HTTP reverse proxy. Digest-only events, hash-chained and sealed with an operational key. Real-time policy decisions with `decidedAt`. In-service and benchmark overhead (D46). | Kept output on streams comes only from the last data line. |
| **4. inquest** | Integrity first (IS and VC). Importers for sentinel logs, OTel GenAI spans and JSONL. Three-valued verdicts naming missing fields. Degradation for Q6 (D48). | ACR and GB are not measured without ground truth, by design. |
| **5. swarm** | Declared vs observed topology with wildcards. Trace-subgraph attribution. Counter-based fault plans injected by the proxy. Contained, cascaded and unrecovered outcomes, with lifecycle visibility. Per-edge latency, errors and tokens (D51). | — |
| **6. card** | Q1–Q6 from evidence, every answer citing file and hash. Q6 measured from degraded audits. Deployment manifest checking one environment. Printable rendering (D52). | The designed visual sheet drafted in `runs/mockup/`. |
| **7. sentinel gate** | Block, or hold for a named approver through a directory queue. Undecidable actions are held. Approvals recorded as attributable events (D50). | Notification of approvers. |
| **8. System audit** | Accepted (D60), open questions settled: [`system-audit.md`](system-audit.md) §10. **S0:** system manifest with strict validation (MCP agents accepted ahead of their adapter); the measurement record with measured / not measured / not applicable; one extractor per document kind, every existing kind covered; `opaas audit validate` and `extract`. **S1:** `opaas audit run`: discover, bench, smith, sentinel, inquest (plus a degraded run for Q6) and swarm, only as the manifest calls for, citing supplied members; the system report with declared vs observed composition, coverage, Q1–Q6 from the cells, block E at system level, and thresholds by component and by kind with three-valued verdicts. **S2:** certifiability rules in the system report (one environment, operator scenarios for every agent, no cloud baseline, no partial bench, no failed step); `OpaasSystemCertificate` signed with `opaas sign`, verified offline with `opaas verify` (model certificates unchanged); the printable system certificate and its site page; environment fixes of D61. **S3:** `opaas audit draft` writes a manifest from the inventory (every served model, named MCP servers and tools; agents, flows and records left as notes; purpose as a marker the report refuses to certify); `opaas watch` on a system certificate re-checks every model, lets a bare-model agent follow its model, compares the environment with the audited one (only models served since the audit alert) and names what moved. | S4: scenario templates from the manifest, the MCP adapter, agent-level bias and toxicity. |

**Open questions (§6), as settled:** 1 — `observe` changes nothing and `gate` is opt-in (D46, D50). 2 — digests by default, the operator adds fields with `--keep` and declares retention on the card; whether a third party may require fields stays open. 3 — yes: a report on reference scenarios alone is `certifiable: false` (D47). 4 — open, listed under the undecided items in `decisions.md`.

**Order rationale:**
- `discover` comes first because every later result binds to its fingerprint (N5).
- `smith` comes before `swarm` because flows are scenarios composed.
- `inquest` follows `sentinel observe`, because the best records to audit are the ones OPAAS itself produces at the strongest integrity level.

---

## 6. Open questions for review

1. **Sentinel reach.** Proxying traffic means OPAAS sits on the data path, which today it never does (it observes deployments; D27). The proposal keeps `observe` passive and makes `gate` opt-in. Is that the right boundary?
2. **Privacy (N4).** Digest-only recording keeps ACR and IS fully measurable, but it lowers RF for policies that need values. Which fields may a deployment keep in clear, and who decides?
3. **Scenario authorship.** OPAAS ships reference scenarios. A certificate for a specific deployment needs the operator's own scenarios. Should a certificate built only on reference scenarios be flagged, the way `opaas-smoke` is?
4. **Bias (D32) and agents.** The counterfactual-pair design extends naturally to agent decisions (same task, one attribute changed: does the agent act differently?). Should it be specified once, in `smith`?

---

## 7. What this plan does not claim

- That OPAAS will certify an agent as *safe*. It measures specified behaviour in specified scenarios and reports how auditable the system is, per dimension, with no aggregate.
- That `discover`'s gap scan predicts violations. It finds missing preconditions for auditing.
- That `inquest` can recover what was never recorded. The paper is explicit about this limit, and so is OPAAS.
