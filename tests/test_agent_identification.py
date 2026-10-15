"""Identifying a system's agents and the models they use (D63).

No test probes the host: identification runs on an injected transport, and
the well-known configuration lookup on a temporary home.
"""

import json

import httpx
import pytest
from typer.testing import CliRunner

from opaas.cli.main import app
from opaas.core.events import AuditEvent, EventLog, evidence, parse_actor_chain
from opaas.discover import discover
from opaas.discover import identify as identify_module
from opaas.discover.agents import agent_stack_hash
from opaas.discover.identify import scan_agents, well_known_mcp_configs
from opaas.discover.inventory import Listener
from opaas.system import load_manifest
from opaas.system.draft import draft_manifest
from opaas.system.report import Member, build_report

runner = CliRunner()

CARD = {"name": "Support Agent", "url": "http://127.0.0.1:41000", "version": "1.2",
        "capabilities": {"streaming": True}, "skills": [{"id": "t", "name": "triage"}]}


def _transport(routes):
    """`routes`: {port: {path: (status, body)}}; a port absent from it refuses connections."""
    def handler(request: httpx.Request) -> httpx.Response:
        paths = routes.get(request.url.port)
        if paths is None:
            raise httpx.ConnectError("refused", request=request)
        status, body = paths.get(request.url.path, (404, {"detail": "Not Found"}))
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def _listeners(*ports, process="python"):
    return {port: Listener(port=port, address="127.0.0.1", process=process) for port in ports}


# --------------------------------------------------------------- the probes


def test_agents_that_identify_themselves_are_found_and_say_how():
    routes = {
        41000: {"/.well-known/agent-card.json": (200, CARD)},
        41001: {"/.well-known/agent.json": (200, {**CARD, "name": "Legacy Agent"})},
        41002: {"/api/config": (200, {"name": "Open WebUI", "version": "0.6"})},
        41003: {"/openapi.json": (200, {"info": {"title": "LangGraph Platform",
                                                 "version": "0.2"}})},
        41004: {"/openapi.json": (200, {"info": {"title": "Inventory API"}})},
    }
    found = scan_agents(_listeners(41000, 41001, 41002, 41003, 41004, 41005),
                        transport=_transport(routes))
    by_port = {a["port"]: a for a in found}
    assert by_port[41000]["kind"] == "a2a" and by_port[41000]["skills"] == ["triage"]
    assert by_port[41000]["basis"] == "A2A agent card at /.well-known/agent-card.json"
    assert by_port[41001]["name"] == "Legacy Agent"
    assert by_port[41002]["adapter"] == "open-webui"
    assert by_port[41003]["adapter"] == "langgraph" and by_port[41003]["basis"] == "OpenAPI title"
    assert 41004 not in by_port, "an API that is not an agent is not promoted to one"
    assert 41005 not in by_port, "a port that does not speak HTTP is skipped"


def test_engines_system_ports_and_the_rest_are_not_probed():
    seen = []

    def handler(request):
        seen.append(request.url.port)
        return httpx.Response(404)

    scan_agents(_listeners(11434, 135, 41000), skip_ports={11434},
                endpoints=["http://agents.internal:9000"],
                transport=httpx.MockTransport(handler))
    assert set(seen) == {41000, 9000}, "engines and ports under 1024 are left alone"


def test_well_known_mcp_configs_are_looked_for_where_each_client_keeps_them(tmp_path):
    home, appdata = tmp_path / "home", tmp_path / "roaming"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}", encoding="utf-8")
    found = well_known_mcp_configs({"USERPROFILE": str(home), "APPDATA": str(appdata)},
                                   platform="win32")
    assert {p.name for p in found} == {"mcp.json", "claude_desktop_config.json"}
    linux = well_known_mcp_configs({"HOME": str(home)}, platform="linux")
    assert [p.name for p in linux] == ["mcp.json"], "only files that exist, never a crawl"


def test_discover_reads_the_well_known_configs_and_records_identified_agents(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"command": "npx", "args": ["docs-mcp@1.0.0"],
                 "env": {"TOKEN": "secret-value"}}}}), encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "none"))
    engines = {"engines": [], "hash": "sha256:eng"}
    plain = discover(engines=engines, listeners={}, containers=False)
    agent = {"kind": "a2a", "adapter": "a2a", "name": "Support Agent", "port": 41000,
             "endpoint": "127.0.0.1:41000", "basis": "A2A agent card"}
    monkeypatch.setattr(identify_module, "probe_agents", lambda *a, **k: [agent])
    found = discover(engines=engines, listeners={}, containers=False, well_known=True)
    stack = found["agentStack"]
    assert stack["wellKnownConfigs"] == ["mcp.json"]
    assert [s["name"] for s in stack["mcpConfigs"][0]["servers"]] == ["docs"]
    assert "secret-value" not in json.dumps(found), "names only, never a value (N4)"
    assert stack["agentEndpoints"] == [agent] and found["hash"] != plain["hash"]


def test_an_inventory_without_agents_keeps_its_hash():
    stack = {"mcpConfigs": [], "toolManifests": [], "skills": [], "containers": {}}
    assert agent_stack_hash(stack) == agent_stack_hash({**stack, "agentEndpoints": []})


# ---------------------------------------------------------------- drafting


def _record(actor, action, target):
    return AuditEvent(action=action, target=target, actor_chain=parse_actor_chain(actor),
                      args=evidence({}), output=evidence("")).to_record()


INVENTORY = {
    "engines": [{"kind": "ollama", "endpoint": "127.0.0.1:11434",
                 "models": [{"name": "qwen"}, {"name": "unused"}]}],
    "agentStack": {"agentEndpoints": [{"kind": "a2a", "adapter": "a2a",
                                       "name": "Support Agent", "endpoint": "127.0.0.1:41000",
                                       "basis": "A2A agent card"}]},
}
RECORDS = [
    _record("user:ana>agent:rag-bot", "model_call", "qwen"),
    _record("user:ana>agent:rag-bot", "tool_call", "search_docs"),
    _record("user:ana>agent:rag-bot", "model_call", "remote-model"),
]


def test_the_records_say_which_agents_use_which_models_and_tools():
    manifest, notes = draft_manifest(INVENTORY, records=RECORDS,
                                     records_source={"source": "sentinel", "path": "e.jsonl"})
    kinds = {c["id"]: (c["kind"], c.get("adapter")) for c in manifest["components"]}
    assert kinds == {"qwen": ("model", None), "support-agent": ("agent", "a2a"),
                     "sentinel": ("proxy", None), "rag-bot": ("agent", "observed"),
                     "search_docs": ("tool", None)}
    assert {"from": "rag-bot", "to": "qwen", "via": "sentinel"} in manifest["flows"]
    assert {"from": "rag-bot", "to": "search_docs"} in manifest["flows"]
    assert manifest["records"] == [{"source": "sentinel", "path": "e.jsonl",
                                    "component": "sentinel"}]
    joined = " ".join(notes)
    assert "unused is served" in joined and "remote-model is called" in joined
    load_manifest(manifest, known_adapters=("bare-model", "http", "mcp"))


def test_an_identified_agent_is_observed_and_an_undeclared_one_is_a_finding(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"kind": "inventory", "inventory": INVENTORY}),
                         encoding="utf-8")
    manifest, _ = draft_manifest(INVENTORY, purpose="Answers support tickets.")
    report = build_report(load_manifest(manifest), manifest_hash="sha256:m",
                          members=[Member("system", "discover", inventory)], steps=[])
    agent = next(c for c in report["composition"]["components"] if c["kind"] == "agent")
    assert agent["observed"] is True and "a2a" in agent["basis"]
    assert not report["composition"]["findings"]

    bare = load_manifest({"format": 1, "id": "s", "purpose": "p",
                          "components": [{"id": "qwen", "kind": "model", "backend": "ollama",
                                          "model": "qwen"}]})
    missing = build_report(bare, manifest_hash="sha256:m",
                           members=[Member("system", "discover", inventory)], steps=[])
    assert any("a2a agent Support Agent" in f for f in missing["composition"]["findings"])


def test_an_agent_known_from_records_is_pending_with_its_reason(tmp_path):
    from opaas.system.orchestrate import run_audit

    manifest = load_manifest(draft_manifest(INVENTORY, records=RECORDS)[0])
    calls = []
    run = run_audit(manifest, base=tmp_path, out=tmp_path / "out",
                    invoke=lambda args: calls.append(args) or 1)
    assert calls[0][:3] == ["discover", "scan", "--well-known"]
    pending = {s.component: s.reason for s in run.steps if s.status == "pending"}
    assert "known only from the records" in pending["rag-bot"]
    assert "no a2a adapter yet" in pending["support-agent"]


def test_audit_draft_from_a_sentinel_log(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for record in RECORDS[:2]:
        log.append(record)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"kind": "inventory", "inventory": INVENTORY}),
                         encoding="utf-8")
    out = tmp_path / "system" / "system.json"
    result = runner.invoke(app, ["audit", "draft", "-i", str(inventory), "--records",
                                 str(log.path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["records"][0]["path"] == "../events.jsonl", "relative to the manifest"
    assert any(c.get("name") == "rag-bot" for c in manifest["components"])


@pytest.mark.parametrize("title, adapter", [("Flowise", "flowise"), ("My Agent Service",
                                                                     "agent-api")])
def test_framework_titles_map_to_adapters(title, adapter):
    routes = {42000: {"/openapi.json": (200, {"info": {"title": title}})}}
    found = scan_agents(_listeners(42000), transport=_transport(routes))
    assert found[0]["adapter"] == adapter
