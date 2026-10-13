"""Importers: records from where they already are, converted whole or not at all (D15, D48).

Each importer turns one source into `AuditEvent` records plus a list of what
it could not convert, and why. As with the dataset importers, a record that
cannot be converted whole is dropped and listed, never half-converted: a
record with a guessed field would be worse than no record, because the audit
would decide on the guess.

- `sentinel`: an OPAAS event log, read as is. Its integrity is assessed.
- `otel`: OpenTelemetry spans following the GenAI semantic conventions, in
  the OTLP JSON export (`resourceSpans` → `scopeSpans` → `spans`). Spans that
  are not GenAI operations are skipped and counted. The actor chain is
  rebuilt from the span tree — the agents of the enclosing `invoke_agent`
  spans and the `enduser.id` at the root — which is structure that was
  recorded, not inference, and the record says so (`actorChainSource`).
  Argument values in span attributes are digested, never copied (N4).
- `jsonl`: a framework trace already shaped as records, one per line.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.events import ACTIONS, PHASES, EventError, digest, read_log, records
from opaas.core.integrity import assess_log

FORMATS = ("sentinel", "otel", "jsonl")

_OPERATIONS = {
    "chat": "model_call",
    "text_completion": "model_call",
    "generate_content": "model_call",
    "embeddings": "model_call",
    "execute_tool": "tool_call",
    "invoke_agent": "delegation",
    "create_agent": "delegation",
}


class ImportError_(ValueError):  # noqa: N801 - `ImportError` is a builtin
    """The source cannot be read at all."""


@dataclass(slots=True)
class Imported:
    format: str
    records: list[dict[str, Any]]
    integrity: dict[str, Any]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def import_records(path: Path, fmt: str, *,
                   public_key: Ed25519PublicKey | None = None,
                   declared_append_only: bool = False) -> Imported:
    if fmt not in FORMATS:
        raise ImportError_(f"Unknown format '{fmt}'. Known: {', '.join(FORMATS)}.")
    if not path.exists():
        raise ImportError_(f"{path} does not exist.")
    integrity = assess_log(path, public_key=public_key,
                           declared_append_only=declared_append_only)
    if fmt == "sentinel":
        try:
            entries, _ = read_log(path)
        except EventError as exc:
            raise ImportError_(str(exc)) from None
        return Imported(fmt, list(records(entries)), integrity)
    if fmt == "jsonl":
        return _jsonl(path, integrity)
    return _otel(path, integrity)


def _jsonl(path: Path, integrity: dict[str, Any]) -> Imported:
    result = Imported("jsonl", [], integrity)
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            result.excluded.append({"line": number, "reason": "not JSON"})
            continue
        if not isinstance(item, dict) or item.get("action") not in ACTIONS:
            result.excluded.append({"line": number, "reason": "no known 'action': the record "
                                    "cannot be classified"})
            continue
        if "phase" in item and item["phase"] not in PHASES:
            result.excluded.append({"line": number, "reason": f"unknown phase {item['phase']}"})
            continue
        result.records.append(item)
    return result


# ------------------------------------------------------------------------- OTel


def _attributes(raw: list[dict[str, Any]] | None) -> dict[str, Any]:
    out = {}
    for item in raw or []:
        value = item.get("value") or {}
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                out[str(item.get("key"))] = value[kind]
                break
        else:
            if "arrayValue" in value:
                out[str(item.get("key"))] = [
                    next(iter(v.values()), None) for v in value["arrayValue"].get("values", [])
                ]
    return out


def _iso(nanos: Any) -> str | None:
    try:
        moment = datetime.fromtimestamp(int(nanos) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _otel_documents(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        document = json.loads(text)
        return [document] if isinstance(document, dict) else list(document)
    except json.JSONDecodeError:
        documents = []
        for line in text.splitlines():
            if line.strip():
                try:
                    documents.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ImportError_(f"{path}: neither OTLP JSON nor JSONL: {exc}") from None
        return documents


def _otel(path: Path, integrity: dict[str, Any]) -> Imported:
    result = Imported("otel", [], integrity)
    spans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for document in _otel_documents(path):
        for resource_spans in document.get("resourceSpans", []):
            resource = _attributes((resource_spans.get("resource") or {}).get("attributes"))
            for scope in resource_spans.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans.append((span, resource))

    by_id = {str(s.get("spanId")): (s, r) for s, r in spans if s.get("spanId")}
    for span, resource in spans:
        attrs = _attributes(span.get("attributes"))
        operation = attrs.get("gen_ai.operation.name")
        if operation is None:
            result.skipped += 1
            continue
        missing = [k for k in ("traceId", "spanId", "startTimeUnixNano") if not span.get(k)]
        action = _OPERATIONS.get(str(operation))
        if missing or action is None:
            result.excluded.append({
                "span": span.get("spanId"),
                "reason": f"missing {', '.join(missing)}" if missing
                else f"unknown GenAI operation '{operation}'"})
            continue
        result.records.append(_span_record(span, resource, attrs, action, by_id))
    result.notes.append(
        "OpenTelemetry exports carry no chain or signature: the integrity found is 'none' "
        "unless the export was itself stored in an OPAAS event log."
    )
    return result


def _span_record(span, resource, attrs, action, by_id) -> dict[str, Any]:
    target = {
        "model_call": attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model"),
        "tool_call": attrs.get("gen_ai.tool.name"),
        "delegation": attrs.get("gen_ai.agent.name") or attrs.get("gen_ai.agent.id"),
    }[action]
    record: dict[str, Any] = {
        "schema": 1,
        "id": str(span["spanId"]),
        "ts": _iso(span["startTimeUnixNano"]),
        "action": action,
        "phase": attrs.get("opaas.phase") if attrs.get("opaas.phase") in PHASES else "attempt",
        "trace": {k: str(v) for k, v in (("traceId", span.get("traceId")),
                                          ("spanId", span.get("spanId")),
                                          ("parentSpanId", span.get("parentSpanId"))) if v},
        "source": "import:otel",
        "policyDecisions": [],
        "extra": {"actorChainSource": "span-ancestry"},
    }
    if target:
        record["target"] = str(target)
    for name, keys in (("args", ("gen_ai.tool.call.arguments", "gen_ai.input.messages")),
                       ("output", ("gen_ai.tool.call.result", "gen_ai.output.messages",
                                   "gen_ai.response.finish_reasons"))):
        value = next((attrs[k] for k in keys if k in attrs), None)
        if value is not None:
            record[name] = {"digest": digest(value)}
    if span.get("endTimeUnixNano"):
        with contextlib.suppress(TypeError, ValueError):
            record["durationMs"] = round(
                (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6, 3)
    status = span.get("status") or {}
    if status.get("code") in (2, "STATUS_CODE_ERROR"):
        record["error"] = str(status.get("message") or "span status ERROR")
    chain = _chain(span, resource, attrs, by_id)
    if chain:
        record["actorChain"] = chain
    return record


def _chain(span, resource, attrs, by_id) -> list[dict[str, str]] | None:
    """Principal at the root, then every enclosing agent, outermost first."""
    agents: list[str] = []
    principal = None
    current, seen = span, set()
    while current is not None and str(current.get("spanId")) not in seen:
        seen.add(str(current.get("spanId")))
        current_attrs = _attributes(current.get("attributes"))
        if current_attrs.get("gen_ai.operation.name") in ("invoke_agent", "create_agent"):
            name = current_attrs.get("gen_ai.agent.name") or current_attrs.get("gen_ai.agent.id")
            if name and current is not span:
                agents.append(str(name))
        principal = principal or current_attrs.get("enduser.id") or current_attrs.get("user.id")
        parent = current.get("parentSpanId")
        current = by_id.get(str(parent), (None, None))[0] if parent else None
    principal = principal or resource.get("enduser.id")
    chain = [{"role": "user", "id": str(principal)}] if principal else []
    chain += [{"role": "agent", "id": name} for name in reversed(agents)]
    if not agents and resource.get("service.name"):
        chain.append({"role": "agent", "id": str(resource["service.name"])})
    return chain or None


__all__ = ["FORMATS", "ImportError_", "Imported", "import_records"]
