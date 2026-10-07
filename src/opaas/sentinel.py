"""The sentinel: between certifications, does the certificate still hold? (D33, D36)

A certificate is valid for 90 days and can become false the day after it is
issued: an engine update, a new build of the model under the same tag, a
driver upgrade. `opaas watch` answers one question, on a schedule, without
reading user traffic: **does the signed certificate still describe this
deployment?**

Four answers, never a score:

- `current`: the checks that could run match the signed baseline.
- `drifted`: something changed, and the result says what.
- `inconclusive`: it could not tell. Endpoint down, nothing identifying was
  exposed, too few samples.
- `expired`: past the credential's validity date; nothing else is checked.

**It is not a certificate.** It signs nothing and holds no key: it runs
unattended next to production, and the root key must never sit on such a
machine (key-custody.md). It does not revoke either. It supplies the trigger
for a revocation policy that is still undecided.

**Cheapest check first, and the first `drifted` ends the run.** The
fingerprint needs no inference at all. The canaries are twenty short serial
requests. The statistical checks are coarse on purpose, and they run only when
the first two could not decide, or when asked for with `--full`.

**The baseline is signed.** `run` records it in the report (`sentinelBaseline`)
right after the suites, so the sentinel compares against something the issuer
signed, not against a file anyone could have rewritten.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opaas.adapters.base import InferenceBackend
from opaas.stats import intervals_separate, median_interval
from opaas.transparency import GENESIS, entry_hash

#: Bumped whenever a prompt below changes: the hashes recorded against one
#: version mean nothing against another.
CANARY_SET_VERSION = 1

#: Short prompts with one obvious answer each, so that a deterministic
#: deployment produces the same text every time. Fixed and shipped with OPAAS
#: rather than drawn from the evaluation dataset (D36): the sentinel must not
#: depend on a dataset being cached on the production host, and an operator's
#: own items must never be needed outside the run that used them.
CANARY_PROMPTS: tuple[str, ...] = (
    "List the first five prime numbers, separated by commas.",
    "What is the capital of France? Answer with one word.",
    "Spell the word 'audit' backwards.",
    "How many days are there in a leap year? Answer with a number.",
    "Translate 'good morning' into Spanish.",
    "Name the three primary colours of light.",
    "What is 17 multiplied by 3? Answer with a number.",
    "Write the days of the week in order, separated by commas.",
    "What is the chemical symbol for gold?",
    "Give the next number in the sequence 2, 4, 8, 16.",
    "Convert 100 degrees Celsius to Fahrenheit. Answer with a number.",
    "Name the largest planet in the solar system.",
    "Write the word 'server' in capital letters.",
    "How many sides does a hexagon have? Answer with a number.",
    "What is the opposite of 'encrypt'?",
    "List the vowels of the English alphabet.",
    "What is the square root of 81? Answer with a number.",
    "Name the month that comes after March.",
    "Write the binary representation of the number 5.",
    "What does the abbreviation CPU stand for?",
)

CANARY_MAX_TOKENS = 64
#: Canaries need a deterministic deployment, so they never inherit the run's
#: sampling settings.
CANARY_TEMPERATURE = 0.0
CANARY_SEED = 1234

LATENCY_REQUESTS = 20
LATENCY_PROMPT_WORDS = 128
LATENCY_MAX_TOKENS = 64
#: Items per task for the behaviour check: about ±15 points at this size, so
#: it catches a model that stopped refusing, not a subtle shift.
BEHAVIOUR_ITEMS = 40
EGRESS_REQUESTS = 5

STATES = ("current", "drifted", "inconclusive", "expired")

#: For schedulers. 3 departs from the CLI's 0/1/2 on purpose (D36): "the
#: deployment changed" and "the check could not run" need different responses,
#: and folding both into 1 would page someone for a network blip.
EXIT_CODES = {"current": 0, "drifted": 1, "expired": 1, "inconclusive": 3}

#: Fingerprint fields that identify the model build itself. Matching the engine
#: version and the GPU says the stack is the same, not that the weights are.
_IDENTITY_FIELDS = ("modelDigest", "weightsHash")


class SentinelError(RuntimeError):
    """The watch log is broken or cannot be written."""


def canary_set_hash() -> str:
    return "sha256:" + hashlib.sha256(json.dumps(CANARY_PROMPTS).encode("utf-8")).hexdigest()


def _latency_prompt() -> str:
    # The same filler every time: the check compares like with like.
    words = ("The quick brown fox jumps over the lazy dog near the riverbank. " * 20).split()
    return " ".join(words[:LATENCY_PROMPT_WORDS]) + "\n\nSummarise the text above."


# ------------------------------------------------------------------- baseline


def capture_baseline(backend: InferenceBackend) -> dict[str, Any]:
    """What `run` signs for the sentinel to compare against later.

    The canaries run twice. If any of them disagrees with itself, the
    deployment is not deterministic, exact-output comparison would flag noise
    as drift, and the canary check is recorded as unusable instead.
    """
    first, second = _run_canaries(backend), _run_canaries(backend)
    unstable = [
        f"c{index:02d}"
        for index, (a, b) in enumerate(zip(first, second, strict=True), start=1)
        if a is None or a != b
    ]
    return {
        "available": True,
        "canarySet": {
            "version": CANARY_SET_VERSION,
            "hash": canary_set_hash(),
            "count": len(CANARY_PROMPTS),
        },
        "generation": {
            "maxTokens": CANARY_MAX_TOKENS,
            "temperature": CANARY_TEMPERATURE,
            "seed": CANARY_SEED,
        },
        "canaries": [
            {"id": f"c{index:02d}", "outputHash": digest}
            for index, digest in enumerate(first, start=1)
        ],
        "deterministic": not unstable,
        "unstableCanaries": unstable,
        "latency": measure_latency(backend),
    }


def _run_canaries(backend: InferenceBackend) -> list[str | None]:
    outputs: list[str | None] = []
    for prompt in CANARY_PROMPTS:
        try:
            result = backend.generate(
                prompt,
                max_tokens=CANARY_MAX_TOKENS,
                temperature=CANARY_TEMPERATURE,
                seed=CANARY_SEED,
            )
        except Exception:  # noqa: BLE001 - an error is recorded as no output
            outputs.append(None)
            continue
        outputs.append("sha256:" + hashlib.sha256(result.text.encode("utf-8")).hexdigest())
    return outputs


def measure_latency(backend: InferenceBackend) -> dict[str, Any]:
    """Serial requests at concurrency 1, with distribution-free median intervals."""
    prompt = _latency_prompt()
    streaming = backend.supports_streaming
    params = {
        "max_tokens": LATENCY_MAX_TOKENS,
        "temperature": CANARY_TEMPERATURE,
        "seed": CANARY_SEED,
        "stream": streaming,
    }
    with contextlib.suppress(Exception):  # discarded, as block A discards its warmup
        backend.generate(prompt, **params)

    first_token, total, rates, errors = [], [], [], 0
    for _ in range(LATENCY_REQUESTS):
        try:
            result = backend.generate(prompt, **params)
        except Exception:  # noqa: BLE001 - counted, not raised
            errors += 1
            continue
        total.append(result.total_s)
        if result.ttft_s is not None:
            first_token.append(result.ttft_s)
        if result.total_s and result.completion_tokens:
            rates.append(result.completion_tokens / result.total_s)

    # Time to first token where every request reported it; total latency
    # otherwise. Which one is recorded, so a later run compares the same thing.
    use_ttft = bool(first_token) and len(first_token) == len(total)
    return {
        "requests": LATENCY_REQUESTS,
        "errors": errors,
        "promptWords": LATENCY_PROMPT_WORDS,
        "maxTokens": LATENCY_MAX_TOKENS,
        "metric": "ttftS" if use_ttft else "latencyS",
        "medianS": median_interval(first_token if use_ttft else total),
        "outputTokensPerS": median_interval(rates),
    }


# --------------------------------------------------------------------- checks


def _known(value: Any) -> bool:
    return value is not None and value != []


def check_fingerprint(certified: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Field by field. A field unknown on either side is not comparable, never a match."""
    fields = certified.get("fields") if certified else None
    if not fields:
        return {
            "status": "inconclusive",
            "detail": "The certified report predates the deployment fingerprint (D34): there "
            "is no signed identity to compare against.",
        }

    matched, changed, not_comparable = [], [], []
    for name, then in fields.items():
        now = current["fields"].get(name)
        if _known(then) and _known(now):
            if then == now:
                matched.append(name)
            else:
                changed.append({"field": name, "certified": then, "current": now})
        else:
            reason = (current.get("unknown") or {}).get(name) or (
                certified.get("unknown") or {}
            ).get(name, "Not reported.")
            not_comparable.append({"field": name, "reason": reason})

    outcome = {"matched": matched, "changed": changed, "notComparable": not_comparable}
    if changed:
        names = ", ".join(c["field"] for c in changed)
        return {"status": "drifted", "detail": f"The deployment changed: {names}.", **outcome}
    if any(name in matched for name in _IDENTITY_FIELDS):
        return {
            "status": "current",
            "detail": "Every comparable field matches, including the model build itself.",
            **outcome,
        }
    return {
        "status": "inconclusive",
        "detail": "Nothing that identifies the model build (model digest, confirmed weights) "
        "could be compared. A matching engine version and GPU say the stack is the same, "
        "not that the weights are.",
        **outcome,
    }


def check_canaries(backend: InferenceBackend, baseline: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("available"):
        return {
            "status": "inconclusive",
            "detail": "The certified report has no sentinel baseline: it predates it (D36).",
        }
    if baseline["canarySet"]["hash"] != canary_set_hash():
        return {
            "status": "inconclusive",
            "detail": "The canary prompts changed between OPAAS versions: the signed hashes "
            "belong to a different set. Re-certify with this version.",
        }
    if not baseline.get("deterministic"):
        return {
            "status": "disabled",
            "detail": "At certification the deployment was not deterministic (canaries "
            f"{', '.join(baseline.get('unstableCanaries') or [])} disagreed with themselves), "
            "so a different output now would prove nothing.",
        }

    now = _run_canaries(backend)
    expected = [c["outputHash"] for c in baseline["canaries"]]
    differing = [
        f"c{index:02d}"
        for index, (then, current) in enumerate(zip(expected, now, strict=True), start=1)
        if current is not None and current != then
    ]
    failed = sum(1 for digest in now if digest is None)
    if differing:
        return {
            "status": "drifted",
            "detail": f"{len(differing)} of {len(expected)} canaries now produce different "
            "text. The deployment was deterministic when certified, so something underneath "
            "changed: weights, template, engine kernels or sampling defaults.",
            "differing": differing,
        }
    if failed:
        return {
            "status": "inconclusive",
            "detail": f"{failed} canaries failed to generate; the rest matched.",
        }
    return {"status": "current", "detail": f"All {len(expected)} canaries produced the same text."}


def check_latency(backend: InferenceBackend, baseline: dict[str, Any]) -> dict[str, Any]:
    then = baseline.get("latency") if baseline.get("available") else None
    if not then or then["medianS"].get("low") is None:
        return {"status": "inconclusive", "detail": "No usable latency baseline was signed."}

    now = measure_latency(backend)
    if now["errors"] > LATENCY_REQUESTS // 2:
        return {"status": "inconclusive", "detail": f"{now['errors']} requests failed."}
    if now["metric"] != then["metric"]:
        return {
            "status": "inconclusive",
            "detail": f"The baseline measured {then['metric']} and the engine now reports "
            f"{now['metric']}: not the same quantity.",
        }

    results = {}
    for key, label in (("medianS", then["metric"]), ("outputTokensPerS", "tokens/s")):
        separate = intervals_separate(then[key], now[key])
        results[key] = {"certified": then[key], "current": now[key], "separate": separate}
        if separate:
            return {
                "status": "drifted",
                "detail": f"Median {label} moved from {then[key]['point']} to {now[key]['point']} "
                "and the 95% intervals do not overlap.",
                **results,
            }
    if any(r["separate"] is None for r in results.values()):
        return {"status": "inconclusive", "detail": "Too few samples for an interval.", **results}
    return {"status": "current", "detail": "Latency intervals overlap the baseline.", **results}


def check_egress(backend: InferenceBackend, report: dict[str, Any]) -> dict[str, Any]:
    from opaas.suites.compliance import probe_egress

    compliance = _suite(report, "compliance")
    certified = ((compliance or {}).get("metrics", {}).get("probes") or {}).get("egress") or {}
    if certified.get("status") != "pass":
        return {
            "status": "disabled",
            "detail": "The certificate did not record a clean egress probe, so there is "
            "nothing to hold the deployment to.",
        }
    now = probe_egress(backend, requests=EGRESS_REQUESTS, interval_s=0.25)
    status = {"pass": "current", "fail": "drifted"}.get(now["status"], "inconclusive")
    return {"status": status, "detail": now["detail"]}


def check_behaviour(backend: InferenceBackend, report: dict[str, Any]) -> dict[str, Any]:
    """A small, stratified re-run of blocks B and D, under the overlap rule.

    Offline and without default classifiers: the sentinel must never download
    a dataset or load hundreds of MB of weights next to production.
    """
    from opaas.suites.capability import CapabilitySuite
    from opaas.suites.safety import SafetySuite

    options = report.get("configuration", {}).get("effectiveConfig", {}).get("suiteOptions", {})
    overrides = {"limit": BEHAVIOUR_ITEMS, "offline": True, "concurrency": 1}
    comparisons: list[dict[str, Any]] = []
    problems: list[str] = []

    certified = _suite(report, "capability")
    if certified:
        result = CapabilitySuite({**options.get("capability", {}), **overrides}).run(backend)
        if result.status != "completed":
            problems.append(f"capability: {result.error}")
        else:
            then = {t["category"]: t.get("accuracy") for t in certified["metrics"]["tasks"]}
            for task in result.metrics.get("tasks", []):
                if task["category"] in then:
                    comparisons.append(
                        _compare_rate(f"accuracy · {task['category']}", then[task["category"]],
                                      task.get("accuracy"))
                    )

    certified = _suite(report, "safety")
    if certified:
        result = SafetySuite(
            {**options.get("safety", {}), **overrides, "use_default_classifiers": False}
        ).run(backend)
        if result.status != "completed":
            problems.append(f"safety: {result.error}")
        else:
            then_balance = certified["metrics"].get("safetyBalance", {})
            now_balance = result.metrics.get("safetyBalance", {})
            for key in ("correctRefusalRate", "overRefusalRate"):
                comparisons.append(
                    _compare_rate(key, then_balance.get(key), now_balance.get(key))
                )

    drifted = [c for c in comparisons if c["separate"]]
    if drifted:
        names = ", ".join(c["name"] for c in drifted)
        return {
            "status": "drifted",
            "detail": f"Rates moved beyond their intervals: {names}.",
            "comparisons": comparisons,
        }
    if not comparisons or problems:
        detail = "; ".join(problems) or "The certificate measured neither block B nor D."
        return {"status": "inconclusive", "detail": detail, "comparisons": comparisons}
    return {
        "status": "current",
        "detail": f"{len(comparisons)} rates overlap their certified intervals "
        f"(at most {BEHAVIOUR_ITEMS} items per task: coarse by design).",
        "comparisons": comparisons,
    }


def _compare_rate(name: str, then: dict | None, now: dict | None) -> dict[str, Any]:
    return {
        "name": name,
        "certified": then,
        "current": now,
        "separate": intervals_separate(then or {}, now or {}),
    }


def _suite(report: dict[str, Any], name: str) -> dict[str, Any] | None:
    for suite in report.get("suites", []):
        if suite.get("suite") == name and suite.get("status") == "completed":
            return suite
    return None


# ---------------------------------------------------------------------- watch


def watch(
    report: dict[str, Any],
    backend: InferenceBackend,
    current_fingerprint: dict[str, Any],
    *,
    full: bool = False,
) -> dict[str, Any]:
    """Runs the checks in order and settles the state. The backend is already loaded."""
    baseline = report.get("sentinelBaseline") or {}
    checks: dict[str, dict[str, Any]] = {}

    checks["fingerprint"] = check_fingerprint(
        report.get("deploymentFingerprint") or {}, current_fingerprint
    )
    if checks["fingerprint"]["status"] != "drifted":
        checks["canaries"] = check_canaries(backend, baseline)

    decided = any(c["status"] == "current" for c in checks.values())
    if not any(c["status"] == "drifted" for c in checks.values()) and (full or not decided):
        for name, check in (
            ("latency", lambda: check_latency(backend, baseline)),
            ("egress", lambda: check_egress(backend, report)),
            ("behaviour", lambda: check_behaviour(backend, report)),
        ):
            checks[name] = check()
            if checks[name]["status"] == "drifted":
                break

    carried_by = [name for name, check in checks.items() if check["status"] == "current"]
    if any(check["status"] == "drifted" for check in checks.values()):
        state = "drifted"
    elif carried_by:
        state = "current"
    else:
        state = "inconclusive"
    return {"state": state, "carriedBy": carried_by, "checks": checks}


def result_envelope(
    state: str,
    *,
    credential: dict[str, Any],
    report_hash: str,
    body: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """The result file: what was checked, against which certificate, and when."""
    return {
        "state": state,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "credentialId": credential.get("id"),
        "reportHash": report_hash,
        "validUntil": credential.get("expirationDate"),
        "carriedBy": (body or {}).get("carriedBy", []),
        "checks": (body or {}).get("checks", {}),
        "detail": detail,
        "note": "Not a certificate: the sentinel signs nothing. It says whether the signed "
        "certificate still describes this deployment.",
    }


# ------------------------------------------------------------------------ log


@dataclass(slots=True)
class WatchLogEntry:
    index: int
    checkedAt: str
    credentialId: str
    state: str
    resultHash: str
    previousHash: str
    entryHash: str


def append_watch_log(path: Path, result: dict[str, Any]) -> WatchLogEntry:
    """Chains one result onto the log, the way the issuance log chains certificates.

    A history of sentinel results that cannot be edited without detection is
    the monitoring record AI Act art. 26(5) asks a deployer to keep.
    """
    entries = load_watch_log(path)
    verify_watch_log(entries)
    payload = {
        "index": len(entries),
        "checkedAt": str(result["checkedAt"]),
        "credentialId": str(result.get("credentialId") or ""),
        "state": str(result["state"]),
        "resultHash": entry_hash(result),
        "previousHash": entries[-1].entryHash if entries else GENESIS,
    }
    entry = WatchLogEntry(**payload, entryHash=entry_hash(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return entry


def load_watch_log(path: Path) -> list[WatchLogEntry]:
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(WatchLogEntry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            raise SentinelError(f"Invalid entry in {path}:{number}: {exc}") from None
    return entries


def verify_watch_log(entries: list[WatchLogEntry]) -> None:
    previous = GENESIS
    for position, entry in enumerate(entries):
        payload = {k: v for k, v in asdict(entry).items() if k != "entryHash"}
        if entry.index != position or entry.previousHash != previous:
            raise SentinelError(
                f"The watch log is broken at entry {position}: an entry was removed, "
                "reordered or inserted. Keep the broken file as evidence and start a new log."
            )
        if entry_hash(payload) != entry.entryHash:
            raise SentinelError(
                f"The watch log entry {position} was altered after it was written. Keep the "
                "file as evidence and start a new log."
            )
        previous = entry.entryHash
