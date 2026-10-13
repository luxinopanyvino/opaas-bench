"""Every metric against a case small enough to compute by hand."""

from opaas.core.metrics import (
    action_completeness_rate,
    attribution_completeness,
    compare_thresholds,
    detection_latency,
    field_recoverability,
    gap_burden,
    integrity_level,
    lifecycle_coverage,
    policy_decidability,
)
from opaas.core.policy import Verdict

CHAIN = [{"role": "user", "id": "u"}, {"role": "agent", "id": "a"}]


def _rec(i, target="t", phase="attempt", digest="sha256:1", chain=CHAIN, **extra):
    record = {"id": f"e{i}", "ts": "2026-01-01T00:00:00Z", "action": "tool_call",
              "target": target, "phase": phase, "actorChain": chain,
              "args": {"digest": digest}, "output": {"digest": "sha256:o"}, **extra}
    return {k: v for k, v in record.items() if v is not None}


def test_acr_counts_recorded_actions_against_ground_truth():
    truth = [_rec(1, "a"), _rec(2, "b"), _rec(3, "c"), _rec(4, "a", digest="sha256:2")]
    # Records a, b and a second `a` without arguments: three of four recovered.
    recorded = [_rec(1, "a"), _rec(2, "b"), _rec(9, "a", digest=None)]
    recorded[2].pop("args", None)
    acr = action_completeness_rate(truth, recorded)
    assert acr["recovered"] == 3 and acr["point"] == 0.75
    assert acr["unrecorded"] == ["e3"]


def test_acr_without_ground_truth_is_not_measured_rather_than_perfect():
    acr = action_completeness_rate(None, [_rec(1)])
    assert acr["measured"] is False and "ground truth" in acr["reason"]


def test_rf_is_the_mean_share_of_required_fields_per_action():
    full = _rec(1)
    half = {"id": "e2", "ts": "t", "action": "tool_call", "target": "x"}  # 4 of 8 present
    rf = field_recoverability([full, half])
    assert rf["point"] == 0.75
    assert rf["completeActions"]["point"] == 0.5
    assert rf["perField"]["actorChain"] == 0.5 and rf["perField"]["id"] == 1.0


def test_lpc_and_gap_burden_against_ground_truth():
    truth = [_rec(1), _rec(2, phase="retry"), _rec(3, "b", phase="block")]
    recorded = [_rec(1), _rec(2, phase="attempt")]  # the retry logged as an attempt
    lpc = lifecycle_coverage(recorded, ground_truth=truth)
    assert lpc["point"] == round(1 / 3, 4) and lpc["unobserved"] == ["block", "retry"]
    gb = gap_burden(truth, recorded)
    assert gb["transitions"] == 2 and gb["gaps"] == 2 and gb["point"] == round(2 / 3, 4)


def test_lpc_against_a_declaration_names_its_basis():
    lpc = lifecycle_coverage([_rec(1)], expected_phases=["attempt", "approval"])
    assert lpc["basis"] == "operator declaration" and lpc["point"] == 0.5
    assert lifecycle_coverage([_rec(1)])["measured"] is False


def test_spdr_names_the_fields_blocking_undecidable_policies():
    verdicts = [
        Verdict("a", "comply"),
        Verdict("b", "violate"),
        Verdict("c", "undecidable", missing=["actorChain"]),
        Verdict("d", "undecidable", missing=["actorChain", "trace.traceId"]),
    ]
    spdr = policy_decidability(verdicts)
    assert spdr["point"] == 0.5 and spdr["violations"] == ["b"]
    assert spdr["missingFieldsBlocking"] == {"actorChain": 2, "trace.traceId": 1}
    assert policy_decidability([])["measured"] is False


def test_adl_is_measured_from_decision_times_only():
    record = _rec(1, policyDecisions=[{"policy": "p", "decidedAt": "2026-01-01T00:00:00.250Z"}])
    assert detection_latency([record])["p50"] == 250.0
    assert detection_latency([_rec(2)])["measured"] is False


def test_ac_requires_a_principal_origin_and_named_hops():
    records = [
        _rec(1),
        _rec(2, chain=[{"role": "agent", "id": "a"}]),
        _rec(3, chain=[{"role": "user", "id": ""}]),
        _rec(4, chain=None),
    ]
    ac = attribution_completeness(records)
    assert ac["AC"]["point"] == 0.25 and ac["ACD"]["p50"] == 2
    assert ac["incompleteBecause"] == {
        "no principal at the origin": 1,
        "a hop without role or identity": 1,
        "no actor chain recorded": 1,
    }


def test_integrity_levels_are_ordered_and_append_only_is_only_declared():
    assert integrity_level(chained=True, signed=True) == "signed"
    assert integrity_level(chained=True, signed=False) == "hash-chained"
    assert integrity_level(chained=False, signed=True) == "none"
    assert integrity_level(chained=False, signed=False, declared_append_only=True) == "append-only"


def test_thresholds_are_compared_never_merged_and_a_straddle_is_none():
    measured = {"AC": {"point": 0.9, "low": 0.8, "high": 0.96},
                "SPDR": {"point": 0.5, "low": 0.2, "high": 0.8},
                "ACR": {"measured": False}}
    rows = {r["metric"]: r for r in compare_thresholds(
        measured, {"AC": {"min": 0.75}, "SPDR": {"min": 0.6}, "ACR": {"min": 0.9}})}
    assert rows["AC"]["meetsWithConfidence"] is True
    assert rows["SPDR"]["meetsAtPoint"] is False and rows["SPDR"]["meetsWithConfidence"] is None
    assert rows["ACR"]["measured"] is None
    assert all(r["declaredBy"] == "operator" for r in rows.values())
