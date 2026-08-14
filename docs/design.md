# OPAAS-Bench — On-Prem AI Audit Suite
### Evaluation and digital credential system for on-prem LLM models — onpremIALabs

---

## 1. Digital credential schema (Verifiable Credential)

A format based on W3C Verifiable Credentials, designed to be verifiable without depending on a central server — anyone can validate the signature with onpremIALabs' public key.

```json
{
  "@context": [
    "https://www.w3.org/2018/credentials/v1",
    "https://onpremialabs.com/credentials/v1"
  ],
  "id": "urn:opaas:cert:llama-3.3-70b:q4_k_m:rtx4090:20260806-001",
  "type": ["VerifiableCredential", "OpaasModelCertificate"],
  "issuer": "did:web:onpremialabs.com",
  "issuanceDate": "2026-08-06T10:00:00Z",
  "expirationDate": "2027-02-06T10:00:00Z",

  "credentialSubject": {
    "model": {
      "name": "Llama-3.3-70B-Instruct",
      "weightsHash": "sha256:9f2a...",
      "source": "meta-llama/Llama-3.3-70B-Instruct",
      "quantization": "GGUF Q4_K_M"
    },
    "configuration": {
      "backend": "vLLM 0.9.x",
      "hardware": "1x RTX 4090 24GB",
      "os": "Ubuntu 24.04",
      "configHash": "sha256:1a3c..."
    },
    "evalRun": {
      "runId": "20260806-001",
      "opaasBenchVersion": "1.0",
      "reportUrl": "https://onpremialabs.com/reports/20260806-001",
      "status": "evaluated"
    }
  },

  "proof": {
    "type": "Ed25519Signature2020",
    "created": "2026-08-06T10:05:00Z",
    "verificationMethod": "did:web:onpremialabs.com#key-1",
    "proofValue": "z58DA..."
  }
}
```

**Public identifier (for badges/QR):**
```
onpremialabs:cert:<model>:<short-config-hash>:<run-id>
→ resolves at https://onpremialabs.com/cert/<id>
```

Every certificate page shows: the methodology used, the evaluation dataset, the artifact hash, the expiry date, the detailed per-category report and a "verify signature" button.

*(Scoring/tier system still to be defined — for now the certificate reflects that the evaluation was carried out and links to the full per-category report, with no aggregate score.)*

Expiry (6 months suggested) forces re-certification — important because the same model with an updated driver or backend can perform differently.

---

## 2. OPAAS-Bench parameters

### A. Performance
- Throughput: tokens/second (prompt processing + generation, separately)
- Latency: TTFT (time to first token), p50/p95/p99
- Memory: peak and idle VRAM/RAM
- Degradation under concurrency (1, 4, 16 simultaneous requests)
- Estimated energy cost (W consumed / 1M tokens) — a strong differentiator for on-prem

### B. Capability / Quality
- General reasoning: MMLU-Pro subset
- Scientific knowledge: GPQA subset
- Code: LiveCodeBench or HumanEval+ subset
- Instruction following: IFEval
- Long context: RULER (if the model supports it)

### C. Consistency / Robustness
- Variance of results across quantizations (FP16 vs Q8 vs Q4) — how much quality degrades
- Determinism: output variance with the same seed/temperature
- Stability against long or malformed prompts (no crashes, no silent truncation)

### D. Behavior and safety
- Hallucination rate (with a Vectara/RAGAS-style subset over your own documents)
- Resistance to basic jailbreaks (a reduced, non-exhaustive subset)
- Correct refusal rate vs. over-refusal (safety false positives)
- Toxicity/bias in outputs (standard subset)

### E. Compliance readiness
- Traceability: does the provider document training data, license, provenance?
- Minimum explainability available (logprobs, exposed reasoning, etc.)
- Compatibility with ENS/AI Act requirements: technical documentation available, applicable risk classification
- Audit support: native logging, possibility of air-gapped deployment

---

## 2bis. Engine-agnostic inference architecture

OPAAS-Bench does not talk directly to each engine — it uses a common interface (`InferenceBackend`) and each engine implements its own adapter. That way modules A–E run the same regardless of the backend.

```python
class InferenceBackend(ABC):
    def load(self, model_id: str, config: dict) -> None: ...
    def generate(self, prompt: str, **params) -> GenerationResult: ...
    def get_resource_usage(self) -> ResourceMetrics:  # VRAM, RAM, power
        ...
    def get_backend_metadata(self) -> dict:  # version, backend, detected quantization
        ...
```

**Supported adapters:**

| Backend | How it connects | Note |
|---|---|---|
| **vLLM** | OpenAI-compatible API (`/v1/completions`) | Standard HTTP server |
| **Ollama** | Its own API (`/api/generate`) | Different format, its own adapter |
| **Hugging Face (transformers)** | Direct loading in Python (`AutoModelForCausalLM`) | In-process, no server — covers loose downloaded models |
| **llama.cpp / local GGUF** | `llama-server` (OpenAI-compatible) or direct binding | Covers quantized models with no additional infrastructure |
| **NVIDIA NIM (local)** | Docker container, OpenAI-compatible API, TensorRT-LLM optimized | Same protocol as vLLM — adapter is nearly reusable |
| **Generic OpenAI-compatible** | Any endpoint that speaks that protocol | Covers TGI, LM Studio, etc. with no new adapter |

**Known per-backend limitation:** with pure `transformers` (no server) there are no real *concurrency* metrics in the Performance block — that belongs to engines with a server (vLLM, llama.cpp server, Ollama, NIM). It is documented as a known limitation, not hidden.

**Cloud baseline (optional, outside block A):** NVIDIA's free public endpoints (build.nvidia.com) allow generating a *quality* reference for the model under optimal conditions, useful for comparing against the local deployment. This data is explicitly marked as `"referenceType": "cloud-baseline"` in the report and is **never mixed** with the Performance metrics (block A), which only make sense when comparing local configurations against each other.

The backend used is recorded in `configuration.backend` inside the credential (section 1), so the "which engine + which model + which hardware" traceability is intact by design.

---

## 3. End-to-end flow

```
1. Model + config selection → hash of both
2. OPAAS-Bench execution (harness over lm-evaluation-harness / Inspect AI + our own A-E modules)
3. Aggregation of results into a per-category structured report (no aggregate score, for now)
4. Generation of a signed VC credential (onpremIALabs Ed25519 key)
5. Publication: certificate page + badge + entry in a public registry (Certificate Transparency-style log)
6. Consumption: third parties verify the signature + consult the re-certification history
```

---

## Open design notes (to be decided)
- Scoring/tier system: pending — for now the certificate rests on the per-category report, with no aggregate score and no tiers
- Re-certification frequency: 6 months suggested, but open to revision
- Initial root of trust: a self-signed key of our own, with the future option of anchoring hashes in a public transparency log
