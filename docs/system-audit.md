# System audit: one subject, one result format, one certificate

**Status:** accepted (D60). Phases S0 to S3 are implemented (`src/opaas/system/`, the system credential in `core/credential/vc.py`, its pages in `web/`); the phases in §11 say what follows.

OPAAS was built in two halves. `bench` certifies a model deployment and has its own report, its own blocks A–E and its own certificate. The agent modules (`discover`, `smith`, `swarm`, `sentinel`, `inquest`, `card`) measure how auditable an agent deployment is, and each writes its own document in its own shape. The deployment manifest (D52) lists them by hash, but nothing turns them into one result a reader can take in, and there is no certificate for anything but a model.

Real use showed the seam. A full audit of qwen2.5:7b-instruct produced a signed model certificate; running the same model as a tool-using agent produced a `smith` document with a different vocabulary (`behaviour`, `auditability`, `certifiable`), different status words, and no certificate at all. An operator asking *"is this system fit and auditable?"* has to read two tools.

This proposal makes the **system** the thing audited, gives every suite **one result format**, and issues **one kind of certificate**, organised around the five dimensions of *Auditable Agents* (Nian et al.) for auditability and around the existing blocks for behaviour. A single model is the smallest system: one component.

---

## 1. What does not change

Every invariant in `CLAUDE.md` holds. In particular:

- **No aggregate score, no tiers** (1). The certificate is a table of components × dimensions; no cell is combined with another.
- **Not measured is declared, never zero** (2). The new format adds a third status, *not applicable*, and it is declared with its reason just the same (§4).
- **The certified hash is the hash of the file's bytes** (4). The system report is a new canonical file; every member report keeps its own bytes and is referenced by their hash, as the deployment manifest already does.
- **Declared and probed are never added** (9). The system's declared composition and the observed one are kept apart, as `swarm` already does for topology.
- **A cloud baseline cannot become a certificate** (10). A system may *depend* on a cloud service; that component is listed as external and is never certified (§6).
- **N1–N6** apply unchanged: three-valued verdicts, structural policies, instrumented ground truth, minimised evidence, environment binding, operator thresholds.

---

## 2. The subject: the system

A **system manifest** describes what is being audited. `discover` drafts it from what it finds on the host; the operator completes and confirms it. It is a declaration, and it is audited against what is observed.

```json
{
  "format": 1,
  "id": "support-assistant",
  "purpose": "Answers employees' questions from internal documents and drafts summaries.",
  "components": [
    {"id": "llm", "kind": "model", "backend": "ollama", "model": "qwen2.5:7b-instruct"},
    {"id": "assistant", "kind": "agent", "adapter": "http", "endpoint": "http://127.0.0.1:9000"},
    {"id": "search", "kind": "tool", "via": "mcp", "server": "docs-mcp"},
    {"id": "docs-mcp", "kind": "mcp-server", "command": "digest-only"},
    {"id": "approver", "kind": "human-approver", "name": "it-oncall"},
    {"id": "gateway", "kind": "proxy", "sentinel": true}
  ],
  "flows": [
    {"from": "assistant", "to": "llm", "via": "gateway"},
    {"from": "assistant", "to": "search"}
  ],
  "scenarios": ["scenarios/support/*.json"],
  "policies": "policies/support.json",
  "records": [{"source": "sentinel", "path": "logs/events.jsonl", "key": "keys/op.pub.pem"}],
  "declaration": "declaration.json",
  "thresholds": "thresholds.json"
}
```

| Component kind | What it is | Audited by |
|---|---|---|
| `model` | An inference endpoint OPAAS can call | `bench` (blocks A–E) |
| `agent` | Something that plans and calls tools | `smith` (scenarios), `inquest` (its records) |
| `tool`, `mcp-server` | What agents act through | `discover` (inventory, gaps), `smith` (mock-tool ground truth) |
| `proxy` | A sentinel on a data path | `sentinel` (records, overhead, gate) |
| `human-approver` | Who answers a gate | `sentinel gate` (approvals as attributable events) |
| `data-store` | Documents or state a component reads | `discover` (declared, never read: N4) |
| `external` | A service outside the operator's control (a cloud API) | listed, never certified (§6) |

`purpose` is the intended use. It is what the AI Act risk classification of block E depends on, and it now sits where it belongs: on the system, not on a model.

---

## 3. Two axes, fixed for every system

Every system is described on the same two axes. Which suite fills a cell depends on the component's kind; the axes and dimensions do not.

**Behaviour.** How the system acts.

| Dimension | Model | Agent | Flow (several components) |
|---|---|---|---|
| Performance | block A | — (not applicable: latency is the model's and the tools') | per-edge latency (`swarm`) |
| Capability | block B | task completion, schema-valid calls (`smith`) | — |
| Consistency | block C | instability across repeats (`smith`) | — |
| Safety | block D | injection resisted, forbidden attempted, destructive without confirmation (`smith`) | contained vs cascaded faults (`swarm`) |
| Compliance | block E | — | — (block E moves to the system, §5) |

**Auditability.** Whether what happened can be reconstructed, checked and attributed: the five dimensions of *Auditable Agents*, with the metrics OPAAS already computes (D44).

| Dimension | Metrics | Card question | Filled by |
|---|---|---|---|
| Action recoverability | ACR, RF | Q1 | `smith` (ACR needs ground truth), `inquest`/`sentinel` (RF) |
| Lifecycle coverage | LPC, GB | Q2 | `smith` (GB needs ground truth), `inquest` with declared phases |
| Policy checkability | SPDR, ADL | Q3 | `sentinel`, `inquest` |
| Responsibility attribution | AC, ACD | Q4 | `sentinel`, `inquest`, `swarm attribute` |
| Evidence integrity | IS, VC | Q5 | `inquest`, `sentinel` |

Q6 (does the audit survive degraded records) stays measured from degraded `inquest` runs, as today.

A model component has no auditability cells of its own: a model keeps no record. Its row on that axis reads *not applicable*, with that reason, and the auditability of the calls made to it is the proxy's or the agent's.

### Blocks A–E, bias and toxicity are kept

Nothing `bench` measures goes away. Blocks A–E are the behaviour axis of every `model` component, cell for cell:

| Model row, behaviour axis | Comes from |
|---|---|
| Performance | block A: throughput, TTFT, VRAM, energy, concurrency scaling |
| Capability | block B: IFEval, multiple choice |
| Consistency | block C: determinism serial and under load, robustness, verified window |
| Safety | block D: refusal and over-refusal, hallucination, faithfulness, toxicity, toxicity under provocation, bias per BBQ category |
| Compliance | block E probes for the engine; the operator's declaration moves to the system (§5) |

Each figure becomes one or more measurement records (§4) with the same value, interval and limitations it has in the bench report, and the bench report itself is a member of the system report, unchanged. A one-model system therefore shows exactly what today's certificate shows, under a components line.

Bias and toxicity can also apply to an **agent** row, measured on what the agent does rather than what the model says: the same task with one personal attribute changed (does the agent act differently?), and the toxicity of the agent's final answers in operator scenarios. Those are new `smith` measurements, not moves of block D; until they exist, the agent row's bias and toxicity cells read *not measured*, with that reason.

---

## 4. One result format: the measurement

Every suite, old and new, is read into the same record. One record is one cell of one component's row.

```json
{
  "component": "assistant",
  "axis": "behaviour",
  "dimension": "safety",
  "aspect": "destructive action without confirmation",
  "metric": "destructiveWithoutConfirmation",
  "status": "measured",
  "value": {"type": "rate", "point": 0.033, "low": 0.009, "high": 0.115, "n": 60},
  "direction": "lower-is-better",
  "source": {"kind": "smith-report", "file": "smith-assistant.json", "hash": "sha256:4a8f…"},
  "environment": {"bound": true, "inventoryHash": "sha256:…"}
}
```

- **`status`** is one of three, and each has a reason when it is not the first:
  - `measured`: there is a value, with its interval and n.
  - `not-measured`: it applies, and could not be measured here (no classifier, no ground truth, no data). Today's `notImplemented` and `available: false` become this.
  - `not-applicable`: it does not apply to this component (auditability of a bare model; ACR on a `bare-model` agent, whose loop OPAAS ran). Today this is reported as "not measured", which reads as a gap in the audit when it is not one.
- **`axis`** is `behaviour` or `auditability`, and **`dimension`** one of that axis's fixed dimensions (§3); **`aspect`** says in words what the cell is, and **`metric`** where to find it in the source.
- **`value`** is typed: a `rate` always with its interval (invariant 11 needs it), a `number` with its unit, a `distribution` (p50/p95/p99 or a median interval) with its unit, or a `verdict` word.
- **`source`** names the member document and its file hash: every cell can be traced to the bytes it came from.
- **Thresholds** are the operator's, from `thresholds.json` (N6), keyed by component and by kind (§10.2). They are applied in the system report (phase S1), which adds the three-valued verdict against them; a measurement read from a document carries none.
- A field the source did not have is absent from the record, never defaulted.

**Existing documents are read, not rewritten.** An extractor per document kind (`bench` report, `smith-report`, `inquest-audit`, `swarm-analysis`, `sentinel-overhead`, `gap-scan`) turns it into measurements. The member files keep their bytes and their hashes, so every report already signed stays verifiable and every invariant about it holds.

---

## 5. The system report and its certificate

**The system report** is one canonical file (JCS, like every report):

- the manifest, and its hash;
- the **observed** composition next to the declared one, and what differs;
- every member document by file hash, with the environment it is bound to;
- the measurement table;
- **coverage**: for each component and dimension, measured, not measured or not applicable;
- the Auditability Card Q1–Q6, each answer citing the measurements it rests on;
- block E, now at system level: the operator's declaration for the system's purpose, and the probes OPAAS ran, apart;
- limitations, bubbled up from every member with the component they belong to.

**The certificate** (`OpaasSystemCertificate`, a W3C VC over the system report's hash, the same Ed25519/JCS discipline as today):

- **Heading:** the system's id and purpose, the issuer, the validity.
- **Components:** each with its kind and what identifies it (model digest, agent endpoint, scenario set hash).
- **Behaviour table:** components × behaviour dimensions, one summary per cell, *not applicable* and *not measured* shown as plainly as values.
- **Auditability table:** the five dimensions, with the components and records each rests on.
- **What this certificate does not claim:** every limitation, with its component.
- **Verification:** the system report hash, every member hash, the environment binding, the QR.

**When a system report is certifiable.** All of these hold, and the report says which do not when one fails:

1. Every member is bound to the same environment inventory (N5). Members measured in different environments describe a system that never existed at one time (the manifest already checks this, D52).
2. Every agent component was run on at least one operator scenario, not only OPAAS's reference set (D47).
3. No component is a cloud baseline (D22). An `external` component is allowed, listed as not certified.
4. No member report is `partial` without the operator having signed it knowingly (as `start.sh` already refuses to do).

A single model run with `opaas run` is the one-component case: a system report with one `model` component, and its certificate.

---

## 6. External components

On-prem systems often call something they do not run: a hosted embedding API, a cloud model as fallback. Pretending they are not there would certify a system that does not exist; certifying them would break D22. They are therefore **listed and excluded**: the certificate names each one, says it was not evaluated, and every flow through it is marked as leaving the operator's control. `discover`'s egress checks and block E's network probe are where such a component shows up undeclared.

---

## 7. One command

```bash
opaas audit run system.json --out runs/support/ --member llm=runs/certified/report.json
```

It runs, in this order, only what the manifest calls for (implemented in S1):

1. `discover scan` and `gaps`: inventory and the observed composition (everything else binds to it).
2. `bench` (`opaas run`, every block unless `--bench-args` chooses) for each `model` component.
3. `smith` for each `agent` component: the reference set, plus the operator's scenarios when the manifest names them. A `bare-model` agent takes its backend and model from its own fields or from the model it flows to. An MCP agent is recorded as *pending* until its adapter exists.
4. `sentinel overhead` for each `proxy`.
5. `inquest audit` for each record source, and again with every tenth record dropped, for Q6.
6. `swarm analyze` against a topology built from the declared flows. A model is named by its model name, as records name a call's target; principals (users, services, schedules) may reach any declared agent or model.

The card is not a separate step: Q1–Q6 are answered in the system report from its auditability cells.

Each step writes its member document exactly as the module does today; `opaas audit run` adds the system report on top. A document the operator already has is cited with `--member component=path` and that step is not run again: a bench run takes hours, and the certified one is the one to cite. The module commands stay, for running one piece. `--only` and `--skip` run part of the plan; a skipped step leaves its cells *not measured*, with "skipped by the operator" as the reason, and a failed one says so and makes the command exit 1.

**Thresholds** (`thresholds.json`): `{"byComponent": {id: {metric: rule}}, "byKind": {kind: {metric: rule}}}`, a rule being `{"min": x}`, `{"max": x}` (with `"stat": "p95"` for a distribution) or `{"in": [words]}` for a verdict. The metric is the cell's `metric`. Each matching cell gets the rule, its scope, and a verdict: comply or violate when the whole interval is on one side, undecidable when it straddles the bound or the cell is not measured (N1).

---

## 8. Compatibility

- **Model certificates already issued keep verifying.** `OpaasModelCertificate` stays a recognised credential type; `verify` accepts both.
- **`opaas run`, `start.sh` and the bench report** do not change shape. The report becomes a member format.
- **Evidence kinds** gain `system-report`; the `OpaasEvidence` credential stays for single documents.
- **The static site and the printable certificate** render both kinds; a system with one model component looks like today's certificate with a components line added.

---

## 9. What is lost

- **Simplicity for the single-model case.** Its certificate gains a components line and an empty auditability table that reads *not applicable*. The alternative, two certificate types forever, is the seam this proposal removes.
- **A new schema to version.** The measurement record and the system manifest are formats third parties will parse; changing them later is a methodology change (`OPAAS_SPEC_VERSION`).
- **Comparability across the change.** A system report and a pre-D60 model report do not line up cell for cell without the extractor; `compare` would compare measurements, not reports.
- **Longer certificates** for real systems. A five-component system has a five-row table on each axis, and every limitation with its component. That length is the content.

---

## 10. Settled questions

Settled by the operator on review of this proposal:

1. **Observed and declared, per component.** The system report records, for each component and each flow, whether it was declared, observed, or both. A component declared and never observed, or observed and never declared, is a finding in its own right, as an undeclared edge is for `swarm`.
2. **Thresholds by component and by kind.** `thresholds.json` accepts both keys. A threshold keyed by component id wins over one keyed by kind, and the system report says which applied to each cell.
3. **`watch` re-checks every component of a system certificate** and names the one that moved. A system with one changed member is a changed system; which member changed is what the operator needs to know first. As built (S3): `opaas watch --report system-report.json --credential …` re-checks each model as `opaas watch` checks one (each member file checked against the hash the system report records), a bare-model agent follows its model, the environment is compared with the audited inventory (a model served since the audit is an alert; one already there was a composition finding), and anything that cannot be re-checked on its own is listed as *not checked*.
4. **Scenario templates are generated from the manifest.** `opaas audit template system.json -o scenarios/` writes one scenario skeleton per declared tool and per risk its name (or, with `--manifest`, its description) points to, by fixed keyword lists: a task template always; a destructive one with a confirmation tool and a `precedes` policy; a permission one with the tool as bait and a `forbid` policy; an injection one with a planted instruction and a canary. Mock tools take their description and parameters from a supplied tool manifest. The user's request, what the real tool returns and what a correct answer contains are left as `TO COMPLETE`: a template that guessed them would put OPAAS's judgement where the operator's belongs. `smith` refuses a scenario that still carries the marker, so an unfinished template can never make an agent certifiable (implemented in S4).
5. **MCP-only agents are prepared for.** The manifest accepts `"adapter": "mcp"` today. Until the client adapter exists, that agent's cells read *not measured: the MCP client adapter is not implemented*, and the plan keeps its place; when the adapter is registered, the same manifest measures it with no other change.

---

## 11. Phases

| Phase | Delivers | Done when |
|---|---|---|
| **S0. Formats** | System manifest schema and validation; the measurement record; extractors for every existing document kind; the three statuses | Every existing document in `runs/` reads into measurements, and a test pins one extractor per kind |
| **S1. System report** | `opaas audit` orchestration with `--only`/`--skip`; declared vs observed composition; coverage; Q1–Q6 from measurements; block E at system level | A one-model system reproduces today's bench figures cell for cell; a model + bare-model agent system shows the agent's auditability as *not applicable* |
| **S2. Certificate** | `OpaasSystemCertificate`, `verify` for both types, printable certificate and site pages for systems, certifiability rules | A signed system certificate verifies offline, and a pre-D60 model certificate still does |
| **S3. Drafting and drift** | `discover` drafts the manifest; `watch --system` re-checking every component (§10.3) | A drafted manifest round-trips through `audit` unchanged on the same host |
| **S4. Gaps** | Scenario templates from the manifest (§10.4); MCP client adapter, the manifest already accepting it (§10.5); bias and toxicity on agent rows | An MCP-only agent gets measured cells with no change to its manifest |

S0 comes first because everything else is built on the measurement record, as the `AuditEvent` schema came first for the agent work (D42).
