import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opaas.core.events import (
    GENESIS,
    AuditEvent,
    EventError,
    EventLog,
    digest,
    evidence,
    missing_fields,
    parse_actor_chain,
    read_log,
    verify_chain,
    verify_seals,
)


def _event(target="search", **kwargs):
    return AuditEvent(
        action="tool_call",
        target=target,
        actor_chain=parse_actor_chain("user:alice>agent:planner"),
        args=evidence({"q": "x"}),
        output=evidence("result"),
        **kwargs,
    ).to_record()


def test_a_field_the_producer_did_not_have_is_absent_not_empty():
    """Absent and empty mean different things to an auditor: nobody recorded vs nobody acted."""
    record = AuditEvent(action="tool_call", target="search", actor_chain=None).to_record()
    assert "actorChain" not in record
    assert set(missing_fields(record)) == {"actorChain", "args", "output"}


def test_arguments_are_recorded_as_digests_unless_kept():
    kept = evidence({"path": "/etc"}, keep=True)
    assert evidence({"path": "/etc"}) == {"digest": kept["digest"]}
    assert kept["value"] == {"path": "/etc"}
    assert digest(b"raw") == digest(b"raw") != digest("raw")


def test_unknown_actions_and_phases_are_refused():
    with pytest.raises(EventError):
        AuditEvent(action="think", target=None, actor_chain=None)
    with pytest.raises(EventError):
        AuditEvent(action="tool_call", target=None, actor_chain=None, phase="maybe")


def test_the_actor_chain_header_parses_outermost_first():
    assert parse_actor_chain("user:alice > agent:planner>tool:search") == [
        {"role": "user", "id": "alice"},
        {"role": "agent", "id": "planner"},
        {"role": "tool", "id": "search"},
    ]


def test_the_log_chains_from_genesis_and_survives_reopening(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    first = log.append(_event())
    assert first["previousHash"] == GENESIS
    reopened = EventLog(path)
    second = reopened.append(_event("fetch"))
    assert second["seq"] == 1 and second["previousHash"] == first["hash"]
    entries, seals = read_log(path)
    verify_chain(entries)
    assert seals == []


def test_rewriting_a_past_record_breaks_the_chain(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    for target in ("a", "b", "c"):
        log.append(_event(target))
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["event"]["target"] = "shell"
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    entries, _ = read_log(path)
    with pytest.raises(EventError, match="entry 1"):
        verify_chain(entries)


def test_seals_sign_the_head_and_verify_only_with_the_verifiers_key(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "events.jsonl"
    log = EventLog(path, signing_key=key, seal_every=2)
    for target in ("a", "b", "c"):
        log.append(_event(target))
    log.seal()
    entries, seals = read_log(path)
    assert [s.up_to for s in seals] == [1, 2]
    covered, problems = verify_seals(entries, seals, key.public_key())
    assert covered == 3 and problems == []

    covered, problems = verify_seals(entries, seals, Ed25519PrivateKey.generate().public_key())
    assert covered == 0 and len(problems) == 2


def test_a_line_that_is_neither_entry_nor_seal_rejects_the_whole_log(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"hello": 1}\n', encoding="utf-8")
    with pytest.raises(EventError, match="neither"):
        read_log(path)
