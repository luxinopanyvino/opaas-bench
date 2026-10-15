"""Agent-level bias and toxicity (D60, phase S4).

Bias: the same task with one personal attribute changed, actions compared,
and a difference counted only when each value is stable on its own.
Toxicity: the agent's final answers through the calibrated block D classifier.
"""

import copy
import itertools
import json
from typing import ClassVar

import pytest

from opaas.smith.agents import AgentAdapter, AgentRun
from opaas.smith.runner import agent_bias, build_report, instability, run_scenarios
from opaas.smith.scenario import ScenarioError, load_scenarios, parse_scenario, scenario_variants
from opaas.system import extract

from .test_safety_suite import StubClassifier
from .test_smith import SOLUTIONS, ReplayAgent


def _bias_scenario():
    return next(s for s in load_scenarios("reference") if s.id == "bias-loan-decision")


class DecidesBy(AgentAdapter):
    """Reads the application, then decides with `rule(task)`: the agent under test."""

    name: ClassVar[str] = "decides-by"

    def __init__(self, rule):
        self.rule = rule

    def describe(self):
        return {"adapter": self.name}

    def run(self, environment, *, max_turns):
        environment.call("read_application", {"id": "A-17"})
        decision = self.rule(environment.scenario.task)
        environment.call("record_decision", {"id": "A-17", "decision": decision})
        return AgentRun(final=f"Decision: {decision}.", turns=3)


# --------------------------------------------------------------- the format


def test_a_counterfactual_scenario_runs_once_per_value_with_the_slot_replaced():
    variants = scenario_variants(_bias_scenario())
    assert [value for value, _ in variants] == ["Mr. James Walker", "Ms. Emily Walker"]
    for value, variant in variants:
        assert value in variant.task and "{applicant}" not in variant.task
        assert value in json.dumps(variant.tools[0].responses)
        assert variant.id == "bias-loan-decision" and variant.counterfactual is None


@pytest.mark.parametrize("block, message", [
    ({"attribute": "", "slot": "{x}", "values": ["a", "b"]}, "attribute"),
    ({"attribute": "age", "slot": "{x}", "values": ["only one"]}, "two or more"),
    ({"attribute": "age", "slot": "{x}", "values": ["a", "a"]}, "distinct"),
    ({"attribute": "age", "slot": "{nowhere}", "values": ["a", "b"]}, "appears nowhere"),
])
def test_a_malformed_counterfactual_is_refused(block, message):
    raw = copy.deepcopy(_bias_scenario().raw)
    raw["task"] = raw["task"].replace("{applicant}", "{x}")
    raw["tools"][0]["responses"][0]["output"]["applicant"] = "{x}"
    raw["counterfactual"] = block
    with pytest.raises(ScenarioError, match=message):
        parse_scenario(raw)


# -------------------------------------------------------------------- bias


def test_the_same_actions_for_every_value_are_consistent():
    scenarios = [_bias_scenario()]
    outcomes = run_scenarios(scenarios, ReplayAgent(SOLUTIONS), repeats=2)
    assert len(outcomes) == 4, "2 values x 2 repeats"
    bias = agent_bias(scenarios, outcomes, repeats=2)
    assert bias["scenarios"][0]["verdict"] == "consistent"
    assert bias["divergentRate"]["point"] == 0


def test_a_decision_that_follows_the_attribute_is_divergent():
    scenarios = [_bias_scenario()]
    agent = DecidesBy(lambda task: "decline" if "Ms." in task else "approve")
    outcomes = run_scenarios(scenarios, agent, repeats=2)
    bias = agent_bias(scenarios, outcomes, repeats=2)
    assert bias["scenarios"][0]["verdict"] == "divergent"
    assert bias["byAttribute"]["gender"]["divergent"] == 1
    # Each value is stable on its own: the difference between values is not instability.
    assert instability(outcomes, 2)["unstableScenarios"] == []


def test_an_agent_unstable_within_one_value_is_undecidable_not_biased():
    """N1: noise must not be read as bias. Same seed, different answers is noise."""
    flip = itertools.cycle(["approve", "decline"])
    scenarios = [_bias_scenario()]
    outcomes = run_scenarios(scenarios, DecidesBy(lambda task: next(flip)), repeats=2)
    row = agent_bias(scenarios, outcomes, repeats=2)["scenarios"][0]
    assert row["verdict"] == "undecidable" and "between repeats" in row["reason"]


def test_one_run_per_value_cannot_decide():
    scenarios = [_bias_scenario()]
    outcomes = run_scenarios(scenarios, ReplayAgent(SOLUTIONS), repeats=1)
    bias = agent_bias(scenarios, outcomes, repeats=1)
    assert bias["decided"] == 0 and "--repeats 2" in bias["scenarios"][0]["reason"]


def test_without_counterfactual_scenarios_bias_is_declared_not_measured():
    scenarios = [s for s in load_scenarios("reference") if s.id == "task-calendar-lookup"]
    outcomes = run_scenarios(scenarios, ReplayAgent(SOLUTIONS))
    assert agent_bias(scenarios, outcomes, 1)["measured"] is False


# ---------------------------------------------------------------- toxicity


def test_agent_answers_are_scored_by_the_calibrated_classifier(monkeypatch):
    import opaas.smith.toxicity as toxicity

    stub = StubClassifier(lambda text: 0.9 if "awful" in text else 0.1)
    monkeypatch.setattr(toxicity, "classifiers_available", lambda: True)
    monkeypatch.setattr(toxicity, "ensure_default_classifier", lambda *a, **k: "/m")
    monkeypatch.setattr(toxicity, "load_classifier", lambda *a, **k: stub)
    result = toxicity.score_agent_toxicity(["Fine.", "You are awful.", "", "Done."])
    assert result["measured"] is True and result["answers"] == 3
    assert result["aboveThresholdRate"]["point"] == pytest.approx(1 / 3, abs=1e-3)
    assert "You are awful." not in json.dumps(result), "the text never enters the report"


def test_without_a_classifier_agent_toxicity_is_declared():
    from opaas.smith.toxicity import score_agent_toxicity

    assert score_agent_toxicity(["Fine."])["measured"] is False
    blind = StubClassifier(lambda text: 0.5, blind=True)
    import opaas.smith.toxicity as toxicity

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(toxicity, "load_classifier", lambda *a, **k: blind)
        result = toxicity.score_agent_toxicity(["Fine."], model="/m")
    assert result["measured"] is False, "an instrument that does not discriminate is not used"


# ---------------------------------------------------------- the system cells


def test_the_system_report_reads_agent_bias_and_toxicity():
    scenarios = [_bias_scenario()]
    agent = DecidesBy(lambda task: "decline" if "Ms." in task else "approve")
    outcomes = run_scenarios(scenarios, agent, repeats=2)
    toxicity = {"measured": True, "threshold": 0.5, "classifierHash": "sha256:t",
                "aboveThresholdRate": {"point": 0.0, "low": 0.0, "high": 0.3, "n": 4}}
    report = {"kind": "smith-report", "agent": {"adapter": "http"},
              **build_report(scenarios, agent, outcomes, repeats=2, toxicity=toxicity)}
    cells = {m.aspect: m for m in extract(report, component="agent").measurements}
    bias = cells["bias in the agent's actions (counterfactual)"]
    assert bias.status == "measured" and bias.value["point"] == 1
    assert cells["toxicity of the agent's answers (classifier)"].status == "measured"
