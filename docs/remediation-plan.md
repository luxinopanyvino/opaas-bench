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

**The chain is already proven.** The whole ceremony was rehearsed with a
throwaway key — `keygen`, `sign`, `trust --add`, then `verify` with no key
flags passing all six checks including log inclusion. See
[key-custody.md](key-custody.md). What is missing is the real key, not the
mechanism, and `opaas trust` now makes step 2.3 a command rather than a
filesystem instruction.

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
| 3.4 | Verify that 3.2 unlocks what it should | ✅ done |

**3.4 is settled.** Block D was run against `ifeval,xstest,<your documents>` and
both dimensions came back measured:

| dimension | with `ifeval,xstest` | with documents composed in |
|---|---|---|
| hallucination with a passage | not measured — no `grounded_qa` items | **measured** |
| faithfulness | not measured — nothing with a passage to score | **measured** |
| unmeasured, total | bias, hallucination, faithfulness | **bias alone** |

It needs no inference server: the backend only has to produce text, and what
was in question was whether the composition reaches the two dimensions. It
does, with the pinned DeBERTa classifier loading and scoring for real. What
3.3 still needs is a served model and your own passages in place of the
example ones.

---

## Phase 4 — The observatory

The public side aggregates what the tool emits. That only produces trustworthy
rankings if the admission rules are written down.

| # | Task | Status |
|---|---|---|
| 4.1 | An admission policy, published like the methodology | ✅ done |
| 4.2 | Document the output contract | ✅ done |
| 4.3 | A submission flow for hardware vendors | open — needs the site |

**4.1 is code, not prose.** [`registry.py`](../src/opaas/registry.py) decides
accepted / flagged / rejected from the report alone, and `opaas admit` exposes
it so a submitter can check their own report before sending it. Published as
[admission-policy.md](admission-policy.md), for the same reason the methodology
is: someone who does not trust the registry can read what it enforces.

**4.2** is the *Grouping rows* section of that document: group by
`environment.gpus[]`, never by the free-text `configuration.hardware`.

**4.3** still needs the site — signed credential plus transparency log, so a
vendor's submission is attributable. The policy deliberately does **not** check
signatures: admission is about comparability, authenticity is `opaas verify`.

---

## Phase 5 — Edge and modularity

| # | Task | Status |
|---|---|---|
| 5.1 | Jetson telemetry backend (`tegrastats`/sysfs) | open — **needs the hardware** |
| 5.2 | ONNX Runtime GenAI adapter | open — **needs the runtime** |
| 5.3 | Open the item type seam | ✅ done |

**5.3** is `register_item_type(name, validator)`, following the same convention
as adapters, suites, sources and importers. A security or XAI layer now brings
its own kind of item without editing `datasets/loader.py`. The four built-in
types were extracted from an `if/elif` chain into registered validators, and
the whole suite passed unchanged: the refactor moved code, it changed no
behaviour.

**5.1 and 5.2 are deliberately not written blind.** Telemetry that claims to
read VRAM and power, and an adapter that claims to drive a runtime, are exactly
the code that must not ship unverified: if either is wrong it produces plausible
numbers rather than an error, which is the failure mode this project exists to
catch. Both need a machine to run on — a Jetson for one, an ONNX Runtime GenAI
install for the other. What is already in place for whoever has that machine:
the telemetry ABC answers *unavailable, with a reason* rather than zero, and
`InferenceBackend` takes a new engine without any change under `suites/`.

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
