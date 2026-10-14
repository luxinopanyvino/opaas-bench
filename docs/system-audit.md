# System audit: one subject, one result format, one certificate

**Status:** proposal (D60). Nothing here is implemented yet; the phases in §11 say in what order it would be.

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

---

## 4. One result format: the measurement

Every suite, old and new, is read into the same record. One record is one cell of one component's row.

```json
{
  "component": "assistant",
  "axis": "safety",
  "dimension": "destructive actions",
  "metric": "destructiveWithoutConfirmation",
  "status": "measured",
  "value": {"point": 0.033, "low": 0.009, "high": 0.115, "n": 60},
  "direction": "lower-is-better",
  "source": {"kind": "smith-report", "file": "smith-assistant.json", "hash": "sha256:4a8f…"},
  "environment": {"bound": true, "inventory": "sha256:…"},
  "threshold": null
}
```

- **`status`** is one of three, and each has a reason when it is not the first:
  - `measured`: there is a value, with its interval and n.
  - `not-measured`: it applies, and could not be measured here (no classifier, no ground truth, no data). Today's `notImplemented` and `available: false` become this.
  - `not-applicable`: it does not apply to this component (auditability of a bare model; ACR on a `bare-model` agent, whose loop OPAAS ran). Today this is reported as "not measured", which reads as a gap in the audit when it is not one.
- **`value`** is always an interval when it is a rate (invariant 11 needs it), or a plain number with its unit when it is not.
- **`source`** names the member document and its file hash: every cell can be traced to the bytes it came from.
- **`threshold`** is the operator's, from `thresholds.json`, or null (N6). When set, the cell also carries the three-valued verdict against it.

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
opaas audit --system system.json --out runs/support/
```

It runs, in this order, only what the manifest calls for:

1. `discover scan` and `gaps`: inventory and the observed composition (everything else binds to it).
2. `bench` for each `model` component.
3. `smith` for each `agent` component, on the operator's scenarios plus the reference set.
4. `swarm analyze` / `attribute` for each declared flow with records.
5. `inquest audit` for each record source.
6. `card build`, then the system report, then signing.

Each step writes its member document exactly as the module does today; `opaas audit` adds the system report on top. The module commands stay, for running one piece. `--only` and `--skip` run part of the plan; a skipped step leaves its cells *not measured*, with "skipped by the operator" as the reason.

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

## 10. Open questions

1. **Who confirms the manifest.** `discover` drafts it. Should the system report record which parts were observed, which declared, and which both, per component (proposed: yes, as `swarm` does for topology)?
2. **Thresholds per system or per component.** `thresholds.json` could key by component id, by kind, or both. Per component is precise and verbose.
3. **Re-certification triggers.** `opaas watch` detects drift for one model. A system changes when any component does; should `watch --system` re-check every member and say which one moved?
4. **Operator scenarios.** Agents need the operator's own scenarios to be certifiable. A scenario template generated from the manifest's tools would lower that bar; it would not write the expectations, which must stay the operator's.
5. **MCP-only agents.** The MCP client adapter is still missing (workplan, phase 2). A system whose agent is reachable only over MCP would have its agent row *not measured* until it exists.

---

## 11. Phases

| Phase | Delivers | Done when |
|---|---|---|
| **S0. Formats** | System manifest schema and validation; the measurement record; extractors for every existing document kind; the three statuses | Every existing document in `runs/` reads into measurements, and a test pins one extractor per kind |
| **S1. System report** | `opaas audit` orchestration with `--only`/`--skip`; declared vs observed composition; coverage; Q1–Q6 from measurements; block E at system level | A one-model system reproduces today's bench figures cell for cell; a model + bare-model agent system shows the agent's auditability as *not applicable* |
| **S2. Certificate** | `OpaasSystemCertificate`, `verify` for both types, printable certificate and site pages for systems, certifiability rules | A signed system certificate verifies offline, and a pre-D60 model certificate still does |
| **S3. Drafting and drift** | `discover` drafts the manifest; `watch --system` | A drafted manifest round-trips through `audit` unchanged on the same host |
| **S4. Gaps** | MCP client adapter; operator scenario template | An MCP-only agent gets measured cells |

S0 comes first because everything else is built on the measurement record, as the `AuditEvent` schema came first for the agent work (D42).
