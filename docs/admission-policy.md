# Admission policy

This document exists for the same reason as the [methodology](methodology.md):
so that someone who does not trust onpremIALabs can decide whether a public
comparison means anything. It states what a registry of reports accepts, what
it accepts with a caveat, and what it refuses.

**It is not prose.** Every rule here is implemented in
[`registry.py`](../src/opaas/registry.py) and pinned by tests, and anyone can
run it on their own report before submitting:

```bash
opaas admit report.json
```

A rule written in the documentation gets broken; one the code rejects does not.

---

## Why a policy at all

A leaderboard is worth exactly what its admission criteria are worth. Fed
submissions from whoever wants to look good — hardware vendors benchmarking
their own configurations, most of all — a registry that accepts everything
ranks nothing: the top row goes to whoever tuned hardest against the
measurement rather than to the best deployment.

The defence was built into the report before there was a registry. Every
setting that changes what a figure means is recorded in it, so the decision can
be **mechanical**: no trusting the submitter, no reading by hand, no private
criteria.

## The three verdicts

| verdict | meaning | exit code |
|---|---|---|
| **accepted** | Nothing found. The row can sit next to others | 0 |
| **flagged** | Comparable, with a caveat that travels with the row | 0 |
| **rejected** | Does not describe something that can be ranked | 1 |

**Rejected is not a verdict on the deployment.** A smoke run and a cloud
baseline are valid, useful runs that simply say nothing about how one
deployment compares with another. And **accepted is never "good"**: it says the
row is comparable, not that the figures are any use. A 3% accuracy with a tight
interval is perfectly admissible.

## What is rejected

| rule | why |
|---|---|
| `smoke-dataset` | The report already declares its own results are not a measurement and must not be certified |
| `cloud-baseline` | Measures somebody else's endpoint under their conditions. It describes no deployment — which is why it cannot be signed either |
| `suite-failed` | The run is incomplete for reasons that have nothing to do with the deployment's quality |
| `raw-prompt-mode` | Without the chat template the model completes text instead of following instructions. On the same model: **52% against 69%** on IFEval, and **15.6% against 75%** on refusal of harmful requests |

That last one is the rule most worth having. Nothing in a report headline shows
it, the numbers look plausible either way, and the difference is larger than
any difference between two models a leaderboard would be trying to show.

## What is flagged

| rule | why the caveat has to travel |
|---|---|
| `partial-coverage` | Not all five blocks ran. Comparable only on the blocks it covers |
| `trimmed-sample` | `--limit` was used: the intervals describe that sample, not the dataset |
| `interval-too-wide` | A 95% interval wider than **10 points** cannot order two submissions. Ranking on it is theatre |
| `unrecognised-dataset` | A dataset the registry does not know. Valid, and not comparable with rows that used a different one |
| `unmeasured-dimensions` | Absent, **not zero**. A row that hides them reads as if they had been measured and come out badly |

## The one parameter to tune

`MAX_RANKABLE_INTERVAL` is set to **0.10**. At 69% accuracy that corresponds to
roughly 300 items; below that the interval swallows the differences a
leaderboard exists to show.

It is a policy decision, not a fact, and it is the one number in this document
worth arguing about. It follows from the rule the tool already applies
everywhere else: [a difference whose confidence intervals overlap is not a
difference](methodology.md).

## What the policy does not do

- **It does not check the signature.** Admission is about comparability;
  authenticity is `opaas verify`. A registry that accepts unsigned submissions
  should say so, and one that requires signing should reject unsigned ones
  before it gets here.
- **It does not rank.** It decides whether a row belongs in the table, never
  where in it.
- **It does not detect a tuned deployment**, only a run whose settings make it
  incomparable. Someone who legitimately configures their engine well and
  measures it honestly should rank highly — that is the point.

## Grouping rows

For comparison, group by `environment.gpus[]` (`{name, vramTotalMb}`), **not**
by `configuration.hardware`. The latter is a free string built for humans:
`"1x NVIDIA GeForce RTX 5060 Ti 16GB"` will not group with a differently
spelled equivalent. The structured field is there for exactly this.

`configHash` identifies the full tuple — model, quantization, engine,
configuration — and is what tells two rows apart when everything else looks
the same.
