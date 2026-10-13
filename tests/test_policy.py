import pytest

from opaas.core.policy import (
    COMPLY,
    UNDECIDABLE,
    VIOLATE,
    PolicyError,
    decide,
    evaluate,
    load_policies,
    policies_hash,
    policy_fields,
)


def _policy(**spec):
    return load_policies([{"id": "p", **spec}])[0]


def _call(target, **extra):
    return {"id": target + str(len(extra)), "action": "tool_call", "target": target, **extra}


NO_SHELL = {"kind": "forbid", "scope": {"field": "action", "eq": "tool_call"},
            "match": {"field": "target", "in": ["shell", "bash"]}}


def test_forbid_is_three_valued():
    policy = _policy(**NO_SHELL)
    assert decide(policy, [_call("search")]).verdict == COMPLY
    violated = decide(policy, [_call("search"), _call("shell")])
    assert violated.verdict == VIOLATE and violated.events == ["shell0"]
    unknown = decide(policy, [{"id": "e1", "action": "tool_call"}])
    assert unknown.verdict == UNDECIDABLE
    assert unknown.missing == ["target"]


def test_a_violation_outweighs_an_undecidable_event():
    """One proven violation settles the policy, whatever else could not be read."""
    verdict = decide(_policy(**NO_SHELL), [{"id": "e1", "action": "tool_call"}, _call("bash")])
    assert verdict.verdict == VIOLATE


def test_an_event_outside_the_scope_needs_no_field_at_all():
    """Kleene AND: a scope that is false decides the event, whatever is missing."""
    verdict = decide(_policy(**NO_SHELL), [{"id": "m", "action": "model_call"}])
    assert verdict.verdict == COMPLY


def test_precedes_requires_an_earlier_matching_event_sharing_the_field():
    # Without the scope, the approval itself (which has no target) could be a
    # delete_file call as far as three-valued logic knows.
    policy = _policy(
        kind="precedes",
        scope={"field": "action", "eq": "tool_call"},
        match={"field": "target", "eq": "delete_file"},
        before={"field": "action", "eq": "approval"},
        sameField="trace.traceId",
    )
    approval = {"id": "a", "action": "approval", "trace": {"traceId": "t1"}}
    delete_t1 = {"id": "d1", "action": "tool_call", "target": "delete_file",
                 "trace": {"traceId": "t1"}}
    delete_t2 = {"id": "d2", "action": "tool_call", "target": "delete_file",
                 "trace": {"traceId": "t2"}}
    assert decide(policy, [approval, delete_t1]).verdict == COMPLY
    assert decide(policy, [delete_t1, approval]).verdict == VIOLATE
    assert decide(policy, [approval, delete_t2]).verdict == VIOLATE
    untraced = {"id": "d3", "action": "tool_call", "target": "delete_file"}
    verdict = decide(policy, [approval, untraced])
    assert verdict.verdict == UNDECIDABLE and verdict.missing == ["trace.traceId"]


def test_require_checks_then_only_where_when_holds():
    policy = _policy(kind="require", when={"field": "action", "eq": "model_call"},
                     then={"field": "args.value.temperature", "lte": 0.5})
    ok = {"id": "1", "action": "model_call", "args": {"value": {"temperature": 0.2}}}
    hot = {"id": "2", "action": "model_call", "args": {"value": {"temperature": 0.9}}}
    digest_only = {"id": "3", "action": "model_call", "args": {"digest": "sha256:x"}}
    assert decide(policy, [ok]).verdict == COMPLY
    assert decide(policy, [ok, hot]).verdict == VIOLATE
    assert decide(policy, [digest_only]).missing == ["args.value.temperature"]


def test_max_count_counts_per_group_and_doubts_only_when_the_limit_is_in_reach():
    policy = _policy(kind="max_count", match={"field": "action", "eq": "model_call"}, max=2,
                     groupBy="trace.traceId")
    def call(i, trace):
        return {"id": str(i), "action": "model_call", "trace": {"traceId": trace}}
    assert decide(policy, [call(1, "a"), call(2, "a"), call(3, "b")]).verdict == COMPLY
    assert decide(policy, [call(i, "a") for i in range(3)]).verdict == VIOLATE
    untraced = {"id": "u", "action": "model_call"}
    assert decide(policy, [call(1, "a"), untraced]).verdict == COMPLY
    verdict = decide(policy, [call(1, "a"), call(2, "a"), untraced])
    assert verdict.verdict == UNDECIDABLE and verdict.missing == ["trace.traceId"]


def test_not_and_any_propagate_unknown():
    record = {"action": "tool_call"}
    assert evaluate({"not": {"field": "target", "eq": "x"}}, record)[0] is None
    assert evaluate({"any": [{"field": "target", "eq": "x"},
                             {"field": "action", "eq": "tool_call"}]}, record)[0] is True
    assert evaluate({"field": "target", "exists": False}, record) == (True, set())


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x", "kind": "judge", "match": {"field": "a", "eq": 1}},
        {"id": "x", "kind": "forbid", "match": {"field": "a", "means": "harmful"}},
        {"id": "x", "kind": "forbid", "match": {"field": "a", "eq": 1}, "matsh": {}},
        {"id": "x", "kind": "forbid"},
        {"id": "x", "kind": "forbid", "match": {"field": "a", "regex": "("}},
    ],
)
def test_anything_that_is_not_a_structural_predicate_is_refused(bad):
    with pytest.raises(PolicyError):
        load_policies([bad])


def test_the_policy_set_hash_and_fields_are_stable():
    policies = load_policies([{"id": "p", **NO_SHELL}])
    assert policies_hash(policies) == policies_hash(load_policies([{"id": "p", **NO_SHELL}]))
    assert policy_fields(policies[0]) == {"action", "target"}
