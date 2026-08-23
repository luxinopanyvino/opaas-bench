# Remediation plan

What is left to do, ordered by dependency rather than by ease. Each item says
what it unblocks and how you know it is done. Phase 1 is complete; the rest is
tracked here so it does not live in someone's memory.

---

## Phase 1 — Reach and credibility ✅

| # | Task | Status | Acceptance |
|---|---|---|---|
| 1.1 | Python floor at 3.10 | done | Suite green on 3.10 and 3.13 |
| 1.2 | CI on every push and PR | done | `.github/workflows/ci.yml`, 3.10–3.13 |
| 1.3 | `CONTRIBUTING.md` | done | Grounded in the invariants, not generic |
| 1.4 | `CITATION.cff` | done | GitHub renders "Cite this repository" |
| 1.5 | Badges | done | CI, Python, licence, methodology — all verifiable |

**Why 3.10 and not 3.11.** JetPack 6 ships Python 3.10, and edge devices are a
supported target: with a 3.11 floor, `pip install opaas` failed on a Jetson.
Lowering it broke twice, both caught by running a real 3.10 rather than
assuming — `datetime.UTC` does not exist before 3.11, and `fromisoformat`
refuses the `Z` suffix that every date we emit ends in. CI now pins both.

---

## Phase 2 — The seal layer (does not exist yet)

**This is the largest gap in the project.** `credential/anchors/` contains only
a README, so onpremIALabs cannot today issue a credential that a third party
verifies without being handed a key. The tool produces self-verifiable evidence;
the seal that means something to a stranger is not operational.

| # | Task | Owner | Depends on |
|---|---|---|---|
| 2.1 | Generate the production root key under real custody | onpremIALabs | — |
| 2.2 | Publish the DID at `onpremialabs.com/.well-known/did.json` | onpremIALabs | 2.1 |
| 2.3 | Bundle the anchor in `credential/anchors/` | — | 2.1 |

**Acceptance:** on a clean machine, `opaas verify --credential cert.json
--report report.json` succeeds with no flags.

The private key never travels through a code change. See
[key-custody.md](key-custody.md): it is the system's only secret, and if it
leaks anyone can issue certificates that look legitimate.

---

## Phase 3 — Complete report coverage

Owned by whoever runs the evaluation, because these are their documents and
their claims.

| # | Task | Effect |
|---|---|---|
| 3.1 | Fill the block E declaration | Declared requirements 0/7 → 7/7 |
| 3.2 | A `grounded_qa` dataset of your own documents | Unlocks hallucination **and** faithfulness |
| 3.3 | The definitive run, with both | Unmeasured dimensions 3 → 1 (bias only) |
| 3.4 | Verify live that 3.2 unlocks what it should | Never yet executed |

3.4 is open: the composition has been validated statically — it loads, composes
to 970 items and carries `grounded_qa` items — but no run has confirmed it,
because the engine was down on both attempts.

---

## Phase 4 — The observatory

The public side aggregates what the tool emits. That only produces trustworthy
rankings if the admission rules are written down.

| # | Task | Note |
|---|---|---|
| 4.1 | An admission policy, published like the methodology | What the site accepts, flags, rejects. **This is the product**: the registry is worth exactly what its criteria are worth |
| 4.2 | Document the output contract | Group by `environment.gpus[]`, **not** `configuration.hardware` — the latter is a free string that will not group |
| 4.3 | A submission flow for hardware vendors | Signed credential plus transparency log, so submissions are attributable |

Every signal 4.1 needs is already in the report: `syntheticSmoke`, `promptMode`,
`datasetHash`, the interval width behind `n`, `evaluated-partial`,
`cloud-baseline`, and a non-empty `limitations`. What is missing is the rule,
not the data.

---

## Phase 5 — Edge and modularity

| # | Task | Note |
|---|---|---|
| 5.1 | Jetson telemetry backend (`tegrastats`/sysfs) | Additive: the ABC already answers *unavailable, with a reason* |
| 5.2 | ONNX Runtime GenAI adapter | Fits the current ABC. Detection models do not — different modality, not a different engine |
| 5.3 | Open `SUPPORTED_ITEM_TYPES` | A closed `frozenset` today. Needed before a security or XAI layer can bring its own data |

Depends on 1.1: without a 3.10 floor there is no Jetson.

---

## Phase 6 — PyPI

Deliberately last: publishing fixes the name and `requires-python` in front of
third parties, so it happens once the floor and the seal are settled.

| # | Task | Note |
|---|---|---|
| 6.1 | Trusted Publishing from GitHub Actions | No tokens in repository secrets |
| 6.2 | Publish `opaas` | Name verified available; the package builds and passes `twine check` |
| 6.3 | Switch the README install lines to `pip install opaas` | Until then they point at the repository, because shipping an install command that fails is a bug we have already had |

---

## Independent

- A cropped mascot image, and regenerate the sprite with
  `python scripts/sprite_from_png.py docs/img/mascot.png`. A head reads at
  favicon size; a full body does not. It also moves further from the meerkat
  depiction registered as part of the SURICATA trademark.
- A screenshot of a published certificate page, once one exists — the only part
  of the tool that text cannot convey.
