"""The five auditability dimensions, measured (D44).

One function per metric of *Auditable Agents*, each over plain records
(`core.events`) and each returning its counts next to its value, so a third
party can recompute it by hand. Proportions carry a Wilson interval, as every
proportion in OPAAS does.

| Dimension | Existence metric | Diagnostic |
|---|---|---|
| Action recoverability | ACR (actions recorded), RF (required fields present) | — |
| Lifecycle coverage | LPC (phases observed), GB (gap burden) | — |
| Policy checkability | SPDR (policies decidable) | ADL (detection latency) |
| Responsibility attribution | AC (complete actor chains) | ACD (chain depth) |
| Evidence integrity | IS (none < append-only < hash-chained < signed) | VC (verification cost) |

**Some metrics need ground truth, and without it they are not computed.** ACR
asks what share of the actions that *happened* were recorded; a record cannot
count what it omitted. `smith` has the ground truth (its mock tools see every
call), so it computes ACR exactly. `inquest`, auditing records that exist,
reports ACR as not measured and says why (invariant 2), never as 100%.

**No aggregate across dimensions, and no thresholds of OPAAS's own** (invariant
1, N6). `compare_thresholds` sets the operator's declared thresholds next to
the measurement; the two are compared, never merged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from opaas.core.events import (
    ACTOR_ROLES,
    INTEGRITY_LEVELS,
    PHASES,
    PRINCIPAL_ROLES,
    REQUIRED_FIELDS,
    missing_fields,
    parse_ts,
)
from opaas.core.policy import UNDECIDABLE, VIOLATE, Verdict
from opaas.core.stats import summarize, wilson_interval

#: The dimensions, in the paper's order, with the Auditability Card question
#: each one answers.
DIMENSIONS: dict[str, dict[str, Any]] = {
    "actionRecoverability": {"question": "Q1", "metrics": ("ACR", "RF")},
    "lifecycleCoverage": {"question": "Q2", "metrics": ("LPC", "GB")},
    "policyCheckability": {"question": "Q3", "metrics": ("SPDR", "ADL")},
    "responsibilityAttribution": {"question": "Q4", "metrics": ("AC", "ACD")},
    "evidenceIntegrity": {"question": "Q5", "metrics": ("IS", "VC")},
}


def not_measured(reason: str) -> dict[str, Any]:
    """What a metric is when its inputs do not exist. Never a zero (invariant 2)."""
    return {"measured": False, "reason": reason}


def _key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("action")), str(record.get("target"))


def _args_digest(record: dict[str, Any]) -> str | None:
    args = record.get("args")
    return args.get("digest") if isinstance(args, dict) else None


def match_records(
    ground_truth: Sequence[dict[str, Any]], recorded: Sequence[dict[str, Any]]
) -> list[tuple[int, int | None]]:
    """Pairs each ground-truth action with the record of it, in order.

    A record matches an action of the same kind and target whose arguments it
    either digests identically or does not carry at all: a record without its
    arguments still records that the action *happened* (ACR); that it lost the
    arguments is what RF measures.
    """
    used: set[int] = set()
    pairs: list[tuple[int, int | None]] = []
    for gt_index, truth in enumerate(ground_truth):
        found = None
        for rec_index, record in enumerate(recorded):
            if rec_index in used or _key(record) != _key(truth):
                continue
            digest = _args_digest(record)
            if digest is None or digest == _args_digest(truth):
                found = rec_index
                break
        if found is not None:
            used.add(found)
        pairs.append((gt_index, found))
    return pairs


# ------------------------------------------------------- action recoverability


def action_completeness_rate(
    ground_truth: Sequence[dict[str, Any]] | None, recorded: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """ACR: the share of the actions that happened that the record carries."""
    if ground_truth is None:
        return not_measured(
            "No ground truth: a record cannot count the actions it omitted. ACR is "
            "measured by `opaas smith`, whose mock tools see every call."
        )
    if not ground_truth:
        return not_measured("The ground truth holds no action.")
    pairs = match_records(ground_truth, recorded)
    recovered = sum(1 for _, found in pairs if found is not None)
    return {
        "measured": True,
        **wilson_interval(recovered, len(pairs)),
        "recovered": recovered,
        "unrecorded": [str(ground_truth[i].get("id")) for i, f in pairs if f is None][:20],
    }


def field_recoverability(recorded: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """RF: of the fields each action must carry, the share it does."""
    if not recorded:
        return not_measured("The record holds no action.")
    shares, complete = [], 0
    absent: Counter[str] = Counter()
    required: Counter[str] = Counter()
    for record in recorded:
        needed = REQUIRED_FIELDS.get(str(record.get("action")), REQUIRED_FIELDS["message"])
        lost = missing_fields(record)
        required.update(needed)
        absent.update(lost)
        shares.append(1 - len(lost) / len(needed))
        complete += not lost
    return {
        "measured": True,
        "point": round(sum(shares) / len(shares), 4),
        "n": len(shares),
        "completeActions": wilson_interval(complete, len(shares)),
        "perField": {name: round(1 - absent[name] / count, 4)
                     for name, count in sorted(required.items())},
    }


# ---------------------------------------------------------- lifecycle coverage


def lifecycle_coverage(
    recorded: Sequence[dict[str, Any]],
    *,
    expected_phases: Iterable[str] | None = None,
    ground_truth: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LPC: the share of execution phases that occurred which the record shows.

    The phases that occurred come from the ground truth when there is one, and
    otherwise from the operator's declaration of which phases the system has.
    Without either, there is nothing to cover and LPC is not measured.
    """
    observed = {str(r.get("phase")) for r in recorded if r.get("phase") in PHASES}
    if ground_truth is not None:
        expected = {str(r.get("phase")) for r in ground_truth if r.get("phase") in PHASES}
        basis = "ground truth"
    elif expected_phases is not None:
        expected = {p for p in expected_phases if p in PHASES}
        basis = "operator declaration"
    else:
        return not_measured(
            "Neither ground truth nor a declaration of the system's phases: there is "
            "nothing to measure coverage against."
        )
    if not expected:
        return not_measured(f"The {basis} names no phase.")
    covered = sorted(observed & expected)
    return {
        "measured": True,
        "point": round(len(covered) / len(expected), 4),
        "n": len(expected),
        "basis": basis,
        "observed": covered,
        "unobserved": sorted(expected - observed),
    }


def gap_burden(
    ground_truth: Sequence[dict[str, Any]] | None, recorded: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """GB: lifecycle transitions that happened but are missing from the record, per action.

    A transition is any ground-truth event whose phase is not a plain
    `attempt`: a retry, a fallback, a block, a pending approval. It counts as a
    gap when no record matches it with the same phase. Needs ground truth.
    """
    if ground_truth is None:
        return not_measured("No ground truth: missing transitions cannot be counted.")
    if not ground_truth:
        return not_measured("The ground truth holds no action.")
    pairs = match_records(ground_truth, recorded)
    transitions = [(i, f) for i, f in pairs if ground_truth[i].get("phase") != "attempt"]
    gaps = [
        str(ground_truth[i].get("id"))
        for i, found in transitions
        if found is None or recorded[found].get("phase") != ground_truth[i].get("phase")
    ]
    return {
        "measured": True,
        "point": round(len(gaps) / len(ground_truth), 4),
        "transitions": len(transitions),
        "gaps": len(gaps),
        "actions": len(ground_truth),
        "missing": gaps[:20],
    }


# --------------------------------------------------------- policy checkability


def policy_decidability(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    """SPDR: the share of policies the record decides, and what blocks the rest."""
    if not verdicts:
        return not_measured("No policy was declared, so none could be checked.")
    undecidable = [v for v in verdicts if v.verdict == UNDECIDABLE]
    decided = len(verdicts) - len(undecidable)
    blocking: Counter[str] = Counter(name for v in undecidable for name in v.missing)
    return {
        "measured": True,
        **wilson_interval(decided, len(verdicts)),
        "verdicts": dict(sorted(Counter(v.verdict for v in verdicts).items())),
        "violations": [v.policy for v in verdicts if v.verdict == VIOLATE],
        "undecidable": [{"policy": v.policy, "missing": v.missing} for v in undecidable],
        # The single most useful line for the operator: log these and the
        # undecidable policies become decidable (Proposition 1).
        "missingFieldsBlocking": dict(blocking.most_common()),
    }


def detection_latency(recorded: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """ADL: from the action to the policy decision about it, in milliseconds.

    Only decisions that carry `decidedAt` count: a decision without a time
    cannot say how late it came.
    """
    latencies = []
    for record in recorded:
        acted = parse_ts(record.get("ts"))
        for decision in record.get("policyDecisions") or []:
            decided = parse_ts(decision.get("decidedAt")) if isinstance(decision, dict) else None
            if acted and decided:
                latencies.append((decided - acted).total_seconds() * 1000)
    if not latencies:
        return not_measured("No recorded policy decision carries a decision time.")
    return {"measured": True, **summarize(latencies, digits=3)}


# --------------------------------------------------- responsibility attribution


def chain_complete(chain: Any) -> bool:
    """A principal at the origin, and every hop named by role and identity."""
    if not isinstance(chain, list) or not chain:
        return False
    for hop in chain:
        if not isinstance(hop, dict) or hop.get("role") not in ACTOR_ROLES or not hop.get("id"):
            return False
    return chain[0]["role"] in PRINCIPAL_ROLES


def attribution_completeness(recorded: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """AC: actions whose responsibility chain is complete. ACD: how deep the chains are."""
    if not recorded:
        return not_measured("The record holds no action.")
    complete = [r for r in recorded if chain_complete(r.get("actorChain"))]
    reasons: Counter[str] = Counter()
    for record in recorded:
        chain = record.get("actorChain")
        if not isinstance(chain, list):
            reasons["no actor chain recorded"] += 1
        elif not chain:
            reasons["empty actor chain"] += 1
        elif not chain_complete(chain):
            first = chain[0].get("role") if isinstance(chain[0], dict) else None
            reasons["no principal at the origin" if first not in PRINCIPAL_ROLES
                    else "a hop without role or identity"] += 1
    return {
        "measured": True,
        "AC": wilson_interval(len(complete), len(recorded)),
        "ACD": summarize([len(r["actorChain"]) for r in complete], digits=2),
        "incompleteBecause": dict(reasons.most_common()),
    }


# ------------------------------------------------------------ evidence integrity


def integrity_rank(level: str) -> int:
    return INTEGRITY_LEVELS.index(level)


def integrity_level(*, chained: bool, signed: bool, declared_append_only: bool = False) -> str:
    """The IS level the evidence actually supports, strongest first.

    `append-only` is a storage property nobody can observe in the file: it is
    reached only when the operator declares it, and it is labelled as such by
    the caller (invariant 9).
    """
    if signed and chained:
        return "signed"
    if chained:
        return "hash-chained"
    return "append-only" if declared_append_only else "none"


# ------------------------------------------------------------------- thresholds


def compare_thresholds(
    measured: dict[str, dict[str, Any]], declared: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The operator's thresholds next to the measurement (N6).

    `declared` maps a metric to `{"min": x}` or `{"max": x}`. `meetsWithConfidence`
    is True or False only when the whole interval sits on one side of the
    threshold, and None when it straddles it: a measurement compatible with
    both sides has not shown either.
    """
    rows = []
    for name, threshold in sorted(declared.items()):
        value = measured.get(name)
        row: dict[str, Any] = {"metric": name, "threshold": threshold, "declaredBy": "operator"}
        if not value or not value.get("measured", True) or value.get("point") is None:
            rows.append({**row, "measured": None, "meetsAtPoint": None,
                         "meetsWithConfidence": None,
                         "detail": "Not measured: the threshold cannot be checked."})
            continue
        point, low, high = value.get("point"), value.get("low"), value.get("high")
        if "min" in threshold:
            bound = float(threshold["min"])
            at_point = point >= bound
            confident = (None if low is None or high is None
                         else True if low >= bound else False if high < bound else None)
        else:
            bound = float(threshold["max"])
            at_point = point <= bound
            confident = (None if low is None or high is None
                         else True if high <= bound else False if low > bound else None)
        rows.append({**row, "measured": {"point": point, "low": low, "high": high},
                     "meetsAtPoint": at_point, "meetsWithConfidence": confident})
    return rows


__all__ = [
    "DIMENSIONS",
    "action_completeness_rate",
    "attribution_completeness",
    "chain_complete",
    "compare_thresholds",
    "detection_latency",
    "field_recoverability",
    "gap_burden",
    "integrity_level",
    "integrity_rank",
    "lifecycle_coverage",
    "match_records",
    "not_measured",
    "policy_decidability",
]
