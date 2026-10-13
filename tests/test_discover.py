import json

import httpx

from opaas.core.events import AuditEvent, EventLog, evidence, parse_actor_chain
from opaas.discover import compare_discoveries, discover
from opaas.discover.agents import (
    classify_tool,
    list_containers,
    read_mcp_config,
    read_skills,
    read_tool_manifest,
)
from opaas.discover.gaps import GAP, NOT_OBSERVED, OK, UNSUPPORTED, ruleset_hash, scan_gaps
from opaas.discover.inventory import Listener, scan
from opaas.sentinel.inventory import check_environment


def _ollama(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.12.0"})
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [
            {"name": "qwen2.5:7b", "digest": "abc", "details": {"quantization_level": "Q4_K_M"}}
        ]})
    return httpx.Response(404)


def _engines(bind_address="127.0.0.1", models=None):
    def handler(request):
        if models is not None and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": models})
        return _ollama(request)
    return scan({11434: Listener(11434, bind_address, "ollama")}, environment={},
                transport=httpx.MockTransport(handler))


def test_the_engine_scan_sends_only_gets_and_records_no_hostname():
    methods = []

    def handler(request):
        methods.append(request.method)
        return _ollama(request)

    inventory = scan({11434: Listener(11434, "0.0.0.0", "ollama.exe")}, environment={},
                     transport=httpx.MockTransport(handler))
    assert set(methods) == {"GET"}
    engine = inventory["engines"][0]
    assert engine["kind"] == "ollama" and engine["bind"] == "all-interfaces"
    assert engine["models"][0]["digest"] == "sha256:abc"
    assert "hostname" not in json.dumps(inventory)


def test_mcp_configs_keep_names_and_drop_secrets(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "files": {"command": "C:/bin/npx.cmd",
                  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/secret"],
                  "env": {"API_TOKEN": "s3cr3t"}},
        "remote": {"type": "http", "url": "https://tools.example:8443/mcp?key=s3cr3t"},
    }}), encoding="utf-8")
    read = read_mcp_config(config)
    dumped = json.dumps(read)
    assert "s3cr3t" not in dumped and "/home/secret" not in dumped
    files, remote = read["servers"]
    assert files["launcher"] == "npx" and files["versionPinned"] is False
    assert files["envNames"] == ["API_TOKEN"]
    assert remote["endpoint"] == "https://tools.example:8443"


def test_free_form_tools_are_classified_from_names_and_parameters():
    assert classify_tool("run_command", "", {"properties": {"command": {"type": "string"}}}) \
        == "shell"
    assert classify_tool("fetch", "Fetch a URL", {"properties": {"url": {"type": "string"}}}) \
        == "http"
    assert classify_tool("get_weather", "", {"properties": {"city": {"type": "string"}}}) is None
    assert classify_tool("exec", "", {"properties": {"mode": {"enum": ["a"]}}}) is None


def test_tool_manifests_accept_mcp_and_openai_shapes(tmp_path):
    mcp = tmp_path / "mcp-tools.json"
    mcp.write_text(json.dumps({"tools": [{"name": "bash", "inputSchema": {
        "properties": {"command": {"type": "string"}}}}]}), encoding="utf-8")
    openai = tmp_path / "functions.json"
    openai.write_text(json.dumps([{"type": "function", "function": {
        "name": "lookup", "parameters": {"properties": {"id": {"type": "integer"}}}}}]),
        encoding="utf-8")
    assert read_tool_manifest(mcp)["tools"][0]["freeForm"] == "shell"
    assert read_tool_manifest(openai)["tools"][0] == {
        **read_tool_manifest(openai)["tools"][0], "name": "lookup", "freeForm": None}


def test_skills_report_version_and_signature_presence(tmp_path):
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    (pinned / "SKILL.md").write_text("---\nname: p\nversion: 1.2.0\n---\nbody", encoding="utf-8")
    (pinned / "SKILL.md.sig").write_text("sig", encoding="utf-8")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    skills = {s["name"]: s for s in read_skills(tmp_path)}
    assert skills["pinned"]["version"] == "1.2.0" and skills["pinned"]["signaturePresent"]
    assert skills["loose"]["versionPinned"] is False


def test_containers_are_listed_by_digest_and_never_by_command():
    def runner(command):
        if command[:2] == ["docker", "ps"]:
            return json.dumps({"Names": "api", "Image": "vllm/vllm:latest", "Ports": "8000",
                               "Command": "serve --api-key s3cr3t"}) + "\n"
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps(["vllm/vllm@sha256:feed"])
        return None

    listed = list_containers(runner)
    assert listed["runtime"] == "docker"
    assert listed["containers"][0]["imageDigest"] == "vllm/vllm@sha256:feed"
    assert "s3cr3t" not in json.dumps(listed)
    assert list_containers(lambda c: None)["available"] is False


def test_discover_says_what_it_did_not_observe():
    document = discover(engines=_engines(), listeners={}, containers=False)
    not_observed = " ".join(document["agentStack"]["notObserved"])
    assert "MCP" in not_observed and "Skills" in not_observed
    assert document["hash"] != document["enginesHash"]


def _log(tmp_path, *, chain=True, approvals=False):
    log = EventLog(tmp_path / "agent.jsonl")
    log.append(AuditEvent(action="tool_call", target="search",
                          actor_chain=parse_actor_chain("user:u>agent:a") if chain else None,
                          args=evidence({}), output=evidence("")).to_record())
    if approvals:
        log.append(AuditEvent(action="approval", target="delete", phase="approval",
                              actor_chain=parse_actor_chain("user:u")).to_record())
    return log.path


def test_the_gap_scan_reports_gaps_ok_and_not_observed(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "direct": {"command": "uvx", "args": ["mcp-server-git==1.0.0"]},
        "proxied": {"type": "http", "url": "http://127.0.0.1:9000/mcp"},
    }}), encoding="utf-8")
    document = discover(engines=_engines("0.0.0.0"), listeners={}, configs=[config],
                        containers=False)
    result = scan_gaps(document, logs=[_log(tmp_path, chain=False)],
                       logging_endpoints=["http://127.0.0.1:9000"])
    by = {(f["rule"], f["subject"]): f["status"] for f in result["findings"]}
    assert by[("G01", "direct")] == GAP and by[("G01", "proxied")] == OK
    assert by[("G02", "agent.jsonl")] == OK  # an OPAAS log is hash-chained
    assert by[("G04", "MCP direct")] == OK
    assert by[("G06", "approvals")] == GAP
    assert by[("G07", "ollama :11434")] == GAP
    assert by[("G08", "agent.jsonl")] == GAP
    assert by[("G03", "tools")] == NOT_OBSERVED
    assert result["ruleset"]["hash"] == ruleset_hash()


def test_a_declaration_without_evidence_is_unsupported_not_ok(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"x": {"command": "node"}}}), encoding="utf-8")
    document = discover(engines=_engines(), listeners={}, configs=[config], containers=False)
    result = scan_gaps(document, logs=[_log(tmp_path)],
                       declaration={"toolLogging": {"layer": "our-proxy"}, "approvals": True})
    statuses = {f["rule"]: f for f in result["findings"]}
    assert statuses["G01"]["status"] == UNSUPPORTED and statuses["G01"]["basis"] == "declared"
    assert statuses["G06"]["status"] == UNSUPPORTED


def test_a_plain_jsonl_log_has_no_integrity(tmp_path):
    plain = tmp_path / "trace.jsonl"
    plain.write_text('{"action": "tool_call"}\n', encoding="utf-8")
    document = discover(engines=_engines(), listeners={}, containers=False)
    finding = next(f for f in scan_gaps(document, logs=[plain])["findings"] if f["rule"] == "G02")
    assert finding["status"] == GAP and "none" in finding["detail"]


def test_the_inventory_watch_alerts_on_new_models_and_widened_binds():
    baseline = discover(engines=_engines(), listeners={}, containers=False)
    same = discover(engines=_engines(), listeners={}, containers=False)
    assert check_environment(baseline, same)["state"] == "current"

    models = [{"name": "qwen2.5:7b", "digest": "abc"}, {"name": "llama3:8b", "digest": "def"}]
    wider = discover(engines=_engines("0.0.0.0", models=models), listeners={}, containers=False)
    result = check_environment(baseline, wider, certified_models={"qwen2.5:7b"})
    alerts = {a["alert"] for a in result["alerts"]}
    assert result["state"] == "drifted"
    assert alerts == {"uncertifiedModel", "bindWidened"}


def test_a_changed_skill_is_a_change_of_environment(tmp_path):
    skills = tmp_path / "skills" / "s"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("v1", encoding="utf-8")
    before = discover(engines=_engines(), listeners={}, containers=False,
                      skill_dirs=[tmp_path / "skills"])
    (skills / "SKILL.md").write_text("v2", encoding="utf-8")
    after = discover(engines=_engines(), listeners={}, containers=False,
                     skill_dirs=[tmp_path / "skills"])
    assert compare_discoveries(before, after)["skillsChanged"] == [{"name": "s"}]
    assert check_environment(before, after)["state"] == "drifted"
