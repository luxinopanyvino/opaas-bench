"""The system audit, phase S4: scenario templates from the manifest (D60, §10.4)."""

import json

import pytest
from typer.testing import CliRunner

from opaas.adapters import register_backend
from opaas.cli.main import app
from opaas.smith.scenario import TEMPLATE_MARKER, ScenarioError, parse_scenario
from opaas.system import load_manifest
from opaas.system.template import scenario_templates, tool_risks

from .conftest import ScriptedBackend

runner = CliRunner()


def _manifest(tools):
    return load_manifest({
        "format": 1, "id": "support", "purpose": "Answers tickets.",
        "components": [{"id": "agent", "kind": "agent", "adapter": "bare-model",
                        "backend": "fake-answering", "model": "m"},
                       *({"id": t, "kind": "tool", "name": t} for t in tools)],
    }, known_adapters=("bare-model", "http"))


def _complete(value):
    """What an operator does: replace every marker with this deployment's content."""
    if isinstance(value, dict):
        return {k: _complete(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_complete(v) for v in value]
    if isinstance(value, str) and TEMPLATE_MARKER in value:
        return value.replace(TEMPLATE_MARKER, "Written by the operator")
    return value


def test_a_tool_gets_the_templates_its_name_calls_for():
    assert tool_risks("delete_ticket") == ["destructive", "injection"]
    assert tool_risks("send_payment") == ["permission"]
    assert tool_risks("fetch_page") == ["injection"]
    assert tool_risks("convert_currency") == []
    templates, _ = scenario_templates(_manifest(["delete_ticket", "convert_currency"]))
    categories = [s["category"] for _, s in templates]
    assert categories == ["task", "destructive", "injection", "task"]


def test_a_template_never_writes_what_correct_means():
    """The request, the tool's real output and the expectation are the operator's."""
    templates, _ = scenario_templates(_manifest(["delete_ticket"]))
    for _, scenario in templates:
        assert TEMPLATE_MARKER in scenario["task"]
        assert all(TEMPLATE_MARKER in e["contains"] for e in scenario["expected"])
    destructive = next(s for _, s in templates if s["category"] == "destructive")
    assert destructive["policies"][0]["kind"] == "precedes"
    assert [t["name"] for t in destructive["tools"]] == ["ask_user", "delete_ticket"]


def test_smith_refuses_a_template_still_to_complete():
    """An unfinished template must never count as the operator's own scenario (D47)."""
    templates, _ = scenario_templates(_manifest(["fetch_page"]))
    for _, scenario in templates:
        with pytest.raises(ScenarioError, match="template still to complete"):
            parse_scenario(scenario)
        parse_scenario(_complete(scenario))  # completed, every template is a valid scenario


def test_an_injection_template_plants_a_canary_smith_accepts():
    templates, _ = scenario_templates(_manifest(["read_email"]))
    injection = next(s for _, s in templates if s["category"] == "injection")
    canary = injection["adversarial"][0]["canary"]
    assert len(canary) >= 8 and canary in injection["tools"][0]["responses"][0]["output"]
    assert parse_scenario(_complete(injection)).canaries == (canary,)


def test_a_manifest_without_tools_gets_one_generic_template_and_a_note():
    templates, notes = scenario_templates(_manifest([]))
    assert len(templates) == 1 and "declares no tools" in notes[0]


def test_tool_schemas_from_a_manifest_fill_the_mock(tmp_path):
    system = tmp_path / "system.json"
    system.write_text(json.dumps({
        "format": 1, "id": "support", "purpose": "Answers tickets.",
        "components": [{"id": "lookup", "kind": "tool", "name": "lookup_order"}]}),
        encoding="utf-8")
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps({"tools": [{"name": "lookup_order",
                                            "description": "Finds an order by id.",
                                            "inputSchema": {"type": "object", "properties": {
                                                "id": {"type": "string"}}}}]}),
                     encoding="utf-8")
    out = tmp_path / "scenarios"
    result = runner.invoke(app, ["audit", "template", str(system), "-o", str(out),
                                 "--manifest", str(tools)])
    assert result.exit_code == 0, result.output
    written = json.loads(next(out.glob("*-task.json")).read_text(encoding="utf-8"))
    assert written["tools"][0]["description"] == "Finds an order by id."
    assert "id" in written["tools"][0]["parameters"]["properties"]
    again = runner.invoke(app, ["audit", "template", str(system), "-o", str(out)])
    assert again.exit_code == 2, "a completed template is never overwritten silently"


def test_completed_templates_make_the_agent_certifiable(tmp_path):
    """The point of §10.4: with the operator's scenarios, smith reports certifiable."""
    class Answering(ScriptedBackend):
        name = "fake-answering"

        def __init__(self):
            super().__init__(lambda prompt: '{"final": "Written by the operator"}')

    register_backend(Answering)
    templates, _ = scenario_templates(_manifest(["delete_ticket"]))
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    for name, scenario in templates:
        (scenarios / name).write_text(json.dumps(_complete(scenario)), encoding="utf-8")
    out = tmp_path / "smith.json"
    result = runner.invoke(app, ["smith", "run", "--agent", "bare-model", "--backend",
                                 "fake-answering", "--model", "m", "--scenarios",
                                 str(scenarios), "-n", "1", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8"))["certifiable"] is True

    unfinished = tmp_path / "unfinished"
    unfinished.mkdir()
    name, scenario = templates[0]
    (unfinished / name).write_text(json.dumps(scenario), encoding="utf-8")
    refused = runner.invoke(app, ["smith", "run", "--agent", "bare-model", "--backend",
                                  "fake-answering", "--model", "m", "--scenarios",
                                  str(unfinished), "-o", str(tmp_path / "no.json")])
    assert refused.exit_code == 2
