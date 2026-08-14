# Implementation plan — OPAAS-Bench
### onpremIALabs

This plan assumes solo development (or a small team) alongside a full-time job, so it is phased to deliver usable value as early as possible instead of building the whole system before shipping anything.

---

## Phase 0 — Foundations (before writing product code)
- Define the repo structure (monorepo: `cli/`, `adapters/`, `suites/`, `credential/`, `web/`)
- Choose the stack: Python for the CLI and the harness (the evals ecosystem is already there), language for the publication website to be decided
- Generate the onpremIALabs root Ed25519 key pair (root of trust) and document how it is kept
- Private repo until the minimum flow is proven; public once the methodology is ready for scrutiny

---

## Phase 1 — Agnostic backend interface + one adapter
**Goal: validate the architecture before investing in coverage**
- Implement the abstract `InferenceBackend` class
- Adapter 1: **vLLM** (OpenAI-compatible API, the most direct)
- Minimal CLI: `opaas-bench run --model <id> --backend vllm --suite performance`
- Output: structured JSON with raw metrics, publishing nothing yet

**Phase exit criterion:** one complete end-to-end run, against a real model, with reproducible results.

---

## Phase 2 — Expand the adapters
- Adapter 2: **Ollama**
- Adapter 3: **llama.cpp / local GGUF**
- Adapter 4: **Hugging Face transformers** (document the concurrency limitation)
- Adapter 5: **local NVIDIA NIM** (reuses much of the vLLM logic through OpenAI compatibility)
- Smoke test: the same small model running on all 5 backends, checking that the metrics make sense against each other

---

## Phase 3 — Complete evaluation suite (blocks A–E)
- **A (Performance):** integrate `genai-perf` or a script of our own over `benchmark_serving.py`, sweeping concurrency and prompt lengths
- **B (Capability):** integrate `lm-evaluation-harness` with reduced subsets of MMLU-Pro, GPQA, HumanEval/LiveCodeBench, IFEval
- **C (Consistency):** our own scripts — comparison across quantizations, determinism with a fixed seed, stress test with long/malformed prompts
- **D (Behavior and safety):** integrate Inspect AI for RAGAS (hallucination), XSTest (over-refusal), a reduced jailbreak subset, RealToxicityPrompts
- **E (Compliance readiness):** audited checklist (not executable) + detection of external network calls during the run (`mitmproxy`/`tcpdump`) to verify air-gapped operation

**Phase exit criterion:** `opaas-bench run --suite all` generates a complete per-category report for at least one model on at least two backends.

---

## Phase 4 — Digital credential
- Implement credential generation following the already-designed VC schema (section 1 of the design document)
- Signing with the root Ed25519 key
- `opaas-bench sign --run-id <id>` command
- Standalone verifier (script or library) that anyone can use to validate a credential without depending on your infrastructure

---

## Phase 5 — Web publication (onpremialabs.com)
- Individual certificate page (methodology, per-category report, hash, "verify signature" button)
- Public listing/registry of issued certificates
- `opaas-bench publish --run-id <id>` command that uploads the result
- Embeddable badge (SVG or image) with a verification link

---

## Phase 6 — Hardening and opening up
- Publish the full methodology and the harness code (transparency = credibility)
- Optional cloud baseline via NVIDIA build.nvidia.com, clearly separated from the Performance block
- Evaluate anchoring hashes in a public Certificate Transparency-style log
- Documentation so third parties can run their own evaluation and request certification without sending you the model

---

## External dependencies to resolve in parallel
- Legal/tax structure of the project (already under way, still to be defined)
- Reference hardware available for testing (which configurations you cover yourself at launch)
- Policy for what happens when an evaluated model changes version (revocation of the previous credential)

---

## Recommended immediate starting order
1. Phase 0 complete
2. Phase 1 (CLI + vLLM) — this is already demonstrable and shareable as an "early preview"
3. Phase 3, block B only (Capability) — the easiest to show with credible results quickly
4. The rest, iterating on feedback
