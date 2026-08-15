# Mitigation plan — report integrity

Findings from run `20260815-001` (qwen2.5:latest on Ollama 0.24.0, RTX 5060 Ti,
`--suite all`), and what was done about each one. The run itself was healthy:
every suite completed, the datasets were the pinned artifacts, and the timings
add up. What failed was the **reporting** — in one case the report stated the
opposite of what its own data showed.

Severity is about the operator reading the report, not about the code:
a wrong verdict on a compliance-facing tool outranks a mislabelled column.

| ID | Severity | Finding | Status |
|---|---|---|---|
| M1 | Critical | Engine truncation reported as *ruled out* when it had happened | Fixed |
| M2 | Critical | The served context window was never known, pinned or reported | Fixed |
| M3 | High | Determinism anchored to the first sample: 20% reported where 80% was true | Fixed |
| M4 | High | `prompt tok/s` read as prefill speed; understates the engine ~34x | Fixed |
| M5 | High | 31 of 541 IFEval items silently excluded from a score presented as IFEval | Fixed |
| M6 | Medium | `constraints failed:` printed the passes | Fixed |
| M7 | Medium | `0/7 met` printed for seven *undeclared* requirements | Fixed |
| M8 | Medium | A silently truncated adverse case counted as *handled* | Fixed |
| M9 | Low | `coverage: complete` above four "not measured" dimensions; plurals | Fixed |

---

## M1 — Engine truncation reported as ruled out

**What the report said.** *"the token count indicates the engine DID process it
whole: the loss is the model's attention, not an engine truncation."*

**What the data said.** The canary probe recorded, per level:

| prompt words | tokens the engine reports | tokens per word |
|---|---|---|
| 24 | 86 | 3.58 |
| 256 | 338 | 1.32 |
| 1024 | 1176 | 1.15 |
| 4096 | **4096** | 1.00 |

4096 words of the probe's filler cost about 4700 tokens. The engine reported
exactly 4096 — the context window — and the canary sits at the *start* of the
prompt, which is the end that gets discarded on overflow. The prompt was cut by
the engine.

**Root cause.** `_token_growth_evidence` compared the largest level against the
**smallest**. The 24-word control carries the fixed cost of the canary preamble,
so it costs 3.58 tokens per word against 1.15 for a long prompt. That dilution
inflated the observed growth ratio to 47.6 against a threshold of 43.4, and the
truncation passed as healthy by 10%.

**Fix.** `_truncation_evidence` in [`suites/consistency.py`](../src/opaas_bench/suites/consistency.py)
replaces it, and reasons over the *marginal* token cost:

- The reference rate is the slope between the two longest levels that came back
  whole, so the preamble cancels out.
- A level is truncated when the count falls more than 5% (and at least 32
  tokens) below what that slope predicts.
- Two earlier signals short-circuit it: the count not growing at all between two
  different prompt lengths, and the count reaching a known served window (M2).
- With only one reference level the slope cannot be separated from the
  intercept, so the weaker per-word test is used and the report says the
  evidence is weaker.

The report now carries `truncationEvidence` with the verdict, the observed
count, the predicted one and the sentence explaining the call, and the
`limitations` text is generated from the verdict instead of being hardcoded to
two branches. A third branch was added for "cause could not be established" —
previously an inconclusive probe was worded as if truncation had been ruled out.

Verified against the real numbers, both directions, in
`test_a_count_that_stops_short_of_the_predicted_growth_is_truncation` and
`test_the_reference_rate_ignores_the_fixed_cost_of_the_preamble`.

**Confirmed on the engine.** Same model and machine, block C only, changing
nothing but the window:

| prompt words | `--num-ctx 4096` | `--num-ctx 8192` |
|---|---|---|
| 24 | 86 tokens, canary back | 86 tokens, canary back |
| 256 | 338 | 338 |
| 1024 | 1176 | 1176 |
| 4096 | **4096, canary lost** | **4528, canary back** |
| verdict | truncation confirmed | no truncation |
| verified window | 1024 words | 4096 words |

The same prompt costs 4528 tokens once it fits — which is what the new
estimator predicted from the shorter levels before ever seeing it. There was no
attention problem: the window was the whole story.

## M2 — The served context window was invisible

**Root cause.** The Ollama adapter never sent `num_ctx`, so every run inherited
whatever the server defaulted to. The benchmark was measuring the server's
configuration while reporting it as the model's behaviour, and nothing in the
report recorded which window was in force — the same command on another machine
measures a different thing.

**Fix.**

- `InferenceBackend.context_window()` — the tokens the engine really processes
  per request, `None` when it cannot be established. `None` means unknown, never
  unlimited.
- Ollama: `--num-ctx` pins it and it is sent on every request; `/api/show` also
  yields the model's trained window, reported separately as an upper bound and
  never mistaken for the served one.
- vLLM and OpenAI-compatible servers: `max_model_len` is read from `/v1/models`
  at load time.
- Not pinning it is now a stated limitation, not a silent default.

**Operator impact.** A run whose window is unpinned is still valid, but it
measures the deployment as configured. To measure the model, pin it:

```bash
opaas-bench run --model qwen2.5:latest --backend ollama --num-ctx 32768 --suite consistency
```

## M3 — Determinism anchored to the first sample

The run reported 20% determinism from 5 repetitions with 2 distinct outputs.
Those numbers only fit one shape: **the first answer was the odd one and the
other four were identical**. The rate was computed against `outputs[0]`, so four
identical answers scored 20% instead of 80% — an order of magnitude off, in the
alarming direction, and anchored to the sample most likely to be an artifact
(cold engine, empty prompt cache).

**Fix.** A warmup generation is discarded before measuring (`determinism_warmup`,
default 1), and the rate is the share of the *most repeated* output. The metric
records `anchor: "modal"` and `warmupDiscarded` so the change is visible in the
JSON rather than silently shifting the numbers.

## M4 — `prompt tok/s` is not prefill speed

`promptTokensPerS` is prompt tokens over the whole measurement window, decoding
included. It is a defensible aggregate, but next to a TTFT column it reads as
prefill speed — and 52 tok/s is an absurd prefill for this GPU. The real rate,
prompt tokens over TTFT, is about 1780 tok/s. The column also grew from 52 to
362 with prompt length purely by amortising fixed cost, inviting the reading
that long prompts are 7x faster.

**Fix.** `prefillTokensPerS` (p50/p95/p99 of prompt tokens ÷ TTFT per request)
is measured and is what the table now shows, under the name `prefill tok/s p50`.
The aggregate stays in the JSON with a note stating what it is not.

## M5 — IFEval items excluded without saying so

The importer drops any item carrying an instruction it cannot check in full —
correctly, since scoring half an item inflates the result. For IFEval that is 31
of 541 items, all `language:response_language`. But `ImportResult.notes` was
discarded by `ensure_composite`, the manifest called the import *"complete (510
items)"*, and the report presented the score as IFEval with no mention that 5.7%
of it never ran.

**Fix.** The manifest records `itemsAvailable`, `itemsImported`, `itemsSelected`
and `excluded {count, byReason}`; the selection label reads `510 of 541 items;
31 excluded (…)` instead of "complete"; the loader carries it into
`dataset.provenance()`; blocks B and D emit it as a limitation; the CLI prints
it under the capability table.

**Cache note.** Manifests are cached, so the fields are added by a
`manifestVersion` bump that re-imports stale caches on first use. The dataset
hash covers the manifest, so `datasetHash` changes: reports from before and
after this change are not hash-comparable on that field. That is the correct
outcome — the recorded provenance is genuinely different — but it is a break and
it is stated here.

## M6 — `constraints failed` printed the passes

`constraints failed: must_not_contain 99/117` was 99 **passes** out of 117
checks. Summed over all kinds it was 662/850 passing, consistent with the 69.4%
headline; read as failures it implied a catastrophe. Now printed as
failures over checks, under `constraints failed (failed/checked):`.

## M7 — `0/7 met` for undeclared requirements

All seven declared requirements were `unknown` — no declaration was supplied —
and the header rendered `0/7 met`, which reads as seven breaches. Unknown is not
failure. The line now distinguishes them, and the verified side reports its
fails and inconclusives instead of only its passes.

## M8 — A truncated adverse case counted as handled

`extreme repetition` returned with `silentTruncation: true` and still scored
`outcome: "ok"`, contributing to a 89% robustness rate. Cutting the answer with
no reason given is the failure mode this block exists to catch, so it cannot
also be evidence of coping. Its outcome is now `truncated`, it is excluded from
`handled`, and it surfaces as a limitation naming the cases.

## M9 — Coverage wording and plurals

`coverage: blocks A–E (complete)` sat above four "not measured" dimensions.
Every block ran, which is not the same as everything having been measured. The
line now reads `all five blocks ran` and is followed by a count of the
dimensions that were not, with their names. `1 silent truncations` and `1
distinct outputs` are pluralised.

---

## Not addressed here

- **Concurrency.** The `no-concurrency-scaling` alerts were correct: TTFT p50 of
  9.29s at concurrency 4 and 46.4s at 16 are exactly 3 and 15 requests of queue
  at ~3.1s each. That is the server's `OLLAMA_NUM_PARALLEL`, a deployment
  setting, and the alert already says so.
- **Determinism under continuous batching.** Still measured serially, still
  declared as a limitation. Measuring it under load needs a different design.
- **Log-probability scoring.** Block B still scores generated text, and still
  says so. Changing it would make the figures comparable with likelihood-scoring
  harnesses, and is a separate piece of work.
