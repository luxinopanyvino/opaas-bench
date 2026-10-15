# Changelog

Every change that alters what OPAAS measures, writes or certifies is recorded
with its reasoning, and what it costs, in [`docs/decisions.md`](docs/decisions.md);
the `D<n>` numbers below point there. This file says what changed between
releases, for someone deciding whether to upgrade.

The methodology version embedded in every report (`OPAAS_SPEC_VERSION`,
`1.0-draft`) is separate from the package version, and did not change.

## [0.2.1] — 2026-10-15

A patch release. **Upgrade if you installed 0.2.0 without the `classifiers`
extra:** `opaas audit run` failed there before running anything.

### Upgrading

- **Nothing to migrate.** Reports, credentials and certificates written by
  0.2.0 verify unchanged, and the methodology version is the same.
- **`discover scan` now sends a few read-only GETs to listeners that are not
  engines** (D63), looking for agents that identify themselves. A scan of the
  same host may therefore find agents 0.2.0 did not; an inventory in which
  none is found keeps the hash 0.2.0 gave it.

### Fixed

- `opaas audit run` imported `click`, which OPAAS does not depend on: typer
  stopped requiring it in 0.17, and it was only present when the
  `classifiers` extra pulled it in through `huggingface-hub`. A test now fails
  on any import of a package `pyproject.toml` does not declare.

### Added — identifying a system's agents (D63)

- `opaas discover scan` recognises agents that identify themselves: A2A agent
  cards, Open WebUI, and OpenAPI documents whose title names an agent
  framework. Each one found says on what basis; `--agent-endpoint` names a
  remote one.
- `--well-known` reads the MCP configurations of common clients (Claude
  Desktop, Claude Code, Cursor, VS Code, Windsurf, Continue) from their usual
  files, names only, never a value.
- `opaas audit draft --records` drafts the agents that acted in a sentinel
  log, OTel spans or JSONL, with the models and tools each one called and the
  flows between them.
- An agent smith cannot drive yet (`a2a`, `open-webui`, a framework, or known
  only from records) is part of the system with a pending adapter: its
  behaviour reads *not measured* with the reason, and the system is not
  certifiable until it is measured.

### Documentation

- The README is rewritten around what OPAAS audits, the audit modes, the
  methodology and the certificates each one produces.

## [0.2.0] — 2026-10-15

The first tagged release. 0.1.0 was never tagged: it is the model
certification of D1–D27 (blocks A–E, signed W3C credentials, the hash-chain
issuance log, the static site). 0.2.0 makes OPAAS audit a **system** — the
models, agents, tools and records that serve one purpose — and issues one
certificate for it.

### Upgrading

- **Reports and certificates from 0.1.0 still verify.** `opaas verify` accepts
  `OpaasModelCertificate` as before, next to the new `OpaasSystemCertificate`.
- **Smith reports now carry a digest of the agent's final answer, not the
  text** (D62). Reports written before keep the text: review them before
  publishing.
- **The public identifier on badges, QR codes and certificates changed** from
  `onpremialabs:cert:…` to the issuer's domain, or `opaas:cert:…` for a
  `did:key` issuer (D58). The signed credential id is unchanged.
- **Inventory hashes** differ from 0.1.0's on a host where Ollama had a model
  loaded (D61): `opaas watch --inventory` against an old baseline may report
  that once.
- **A run with `rtp` and the default load check takes longer**: 200 more
  generations, and five rounds of concurrent requests in block C (D59).

### The system audit (D60)

- `opaas audit draft` writes a system manifest from what the host serves.
- `opaas audit validate` checks it; `opaas audit template` writes scenario
  templates for its tools, leaving every answer to the operator, and smith
  refuses a template still marked `TO COMPLETE`.
- `opaas audit run` runs only what the manifest calls for (discover, bench per
  model, smith per agent, sentinel per proxy, inquest and swarm per record
  source), cites documents the operator already has (`--member`), and writes
  the system report: members by hash, declared against observed composition,
  one cell per component and dimension (measured, not measured or not
  applicable, always with a reason), the Auditability Card Q1–Q6, block E for
  the system's purpose, and operator thresholds judged comply, violate or
  undecidable.
- `opaas audit extract` reads any OPAAS document into the common measurement
  format.
- `OpaasSystemCertificate`, signed, verified, printed and published with the
  existing commands; certifiable only when every member shares one
  environment, every agent ran the deployment's own scenarios, no component is
  a cloud baseline, no bench report is partial and no step failed.
- `opaas watch` on a system certificate re-checks every component and names
  the one that moved.

### Agents (D41–D52, D60)

- One evidence core (`core/`): the `AuditEvent` schema, hash-chained and sealed
  logs, structural policies with three-valued verdicts, and the auditability
  metrics ACR, RF, LPC, GB, SPDR, ADL, AC, ACD, IS and VC.
- `discover` (inventory and gap scan), `smith` (fixed scenarios with mock-tool
  ground truth; `bare-model`, `http` and `mcp` adapters), `swarm` (declared
  against observed flows, fault injection), `sentinel` (observe and gate proxy,
  approvals), `inquest` (post-hoc audit, degradation for Q6) and `card`.
- Agent-level bias from counterfactual scenarios, compared on actions and
  undecidable when the agent is unstable within one value, and toxicity of the
  agent's answers (D60, S4).

### Model certification

- Hallucination with a passage from SQuAD 2.0, answerable and unanswerable
  reported apart (D53).
- Bias with BBQ, per social category, never pooled (D55).
- Toxicity under provocation with RealToxicityPrompts' challenging subset,
  reported per band and apart from ordinary responses (D59); long responses
  scored in windows rather than truncated (D56).
- Determinism under concurrent load in block C, `--load-concurrency` (D59).
- The deployment fingerprint and the host inventory in every report (D34, D40),
  and `opaas watch` between certifications (D33, D36).
- A printable certificate with a QR code (D29), 90-day validity (D28), operator
  notes printed as unverified (D31), and block E references cited to the clause
  (D30).

### Fixed

- Keys signed as onpremIALabs unless told otherwise (D39).
- Block E probes reported what they did not observe (D38).
- The operator's token cap was ignored in favour of each item's (D54).
- The sentinel lost its last seal on SIGTERM, and accepted an actor chain it
  could not complete (D57).
- One environment was read as two because of Ollama's model runner and mixed
  hash granularities (D61).
- Smith reports carried the agent's answers verbatim (D62).

[0.2.1]: https://github.com/luxinopanyvino/opaas-bench/releases/tag/v0.2.1
[0.2.0]: https://github.com/luxinopanyvino/opaas-bench/releases/tag/v0.2.0
