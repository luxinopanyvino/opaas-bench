"""The mock-tool environment: the agent's tools, and the ground truth about them (N3).

Every tool call goes through `MockEnvironment.call`, which answers from the
scenario's rules and records an `AuditEvent` with every field filled in. That
record is the ground truth the agent's own log is measured against: it is
produced by instrumentation, never inferred, and never by a model (N3).

The oracle keeps argument and output *values*, not only digests. They are
synthetic scenario data, not a user's, so N4's reason to minimise does not
apply, and the behaviour checks (canaries, end-state predicates) need them.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from opaas.core.events import AuditEvent, evidence
from opaas.smith.scenario import (
    _MISSING,
    MockTool,
    Scenario,
    apply_effects,
    holds,
    resolve_template,
)

_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


def schema_errors(schema: dict[str, Any], args: Any) -> list[str]:
    """A deliberately small JSON-schema check: types, required, enum, extra keys.

    Tool schemas in practice use this subset. Anything richer (`oneOf`,
    `pattern`) is not checked, and a call is never failed for it.
    """
    if not isinstance(args, dict):
        return ["the arguments are not a JSON object"]
    errors = []
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in args:
            errors.append(f"missing required '{name}'")
    for name, value in args.items():
        spec = properties.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                errors.append(f"unexpected '{name}'")
            continue
        expected = _JSON_TYPES.get(str(spec.get("type")))
        if expected and (not isinstance(value, expected) or
                         (spec.get("type") in ("integer", "number") and isinstance(value, bool))):
            errors.append(f"'{name}' is not {spec.get('type')}")
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"'{name}' is not one of {spec['enum']}")
    return errors


@dataclass(slots=True)
class CallResult:
    output: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.error} if self.error is not None else {"output": self.output}


@dataclass(slots=True)
class MockEnvironment:
    scenario: Scenario
    #: Who OPAAS records as acting when the agent does not say: the user that
    #: gave the task and the agent under test.
    default_chain: list[dict[str, str]]
    state: dict[str, Any] = field(init=False)
    record: list[dict[str, Any]] = field(default_factory=list, init=False)
    calls: list[dict[str, Any]] = field(default_factory=list, init=False)
    _confirmed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.state = self.scenario.initial_state()

    def call(
        self,
        name: str,
        args: Any,
        *,
        actor_chain: list[dict[str, str]] | None = None,
        trace: dict[str, str] | None = None,
    ) -> CallResult:
        """Answers one call and records it. Thread-safe: an HTTP agent may call in parallel."""
        with self._lock:
            return self._call(name, args, actor_chain, trace)

    def _call(self, name, args, actor_chain, trace) -> CallResult:
        tool = self.scenario.tool(name)
        args_dict = args if isinstance(args, dict) else {}
        errors = schema_errors(tool.parameters, args) if tool else ["undeclared tool"]
        phase = self._phase(name, args)
        destructive_unconfirmed = bool(tool and tool.destructive and not self._confirmed)

        if tool is None:
            result = CallResult(error=f"There is no tool named '{name}'.")
        else:
            result = self._respond(tool, args_dict)
            if tool.confirmation and result.error is None:
                self._confirmed = True

        event = AuditEvent(
            action="approval" if tool is not None and tool.confirmation else "tool_call",
            target=name,
            actor_chain=copy.deepcopy(actor_chain or self.default_chain),
            phase="approval" if tool is not None and tool.confirmation else phase,
            args=evidence(args, keep=True),
            output=evidence(result.as_dict(), keep=True),
            trace=trace,
            error=result.error,
            source="smith-oracle",
            extra={"scenario": self.scenario.id, "step": len(self.calls) + 1},
        ).to_record()
        self.record.append(event)
        self.calls.append({
            "tool": name,
            "args": copy.deepcopy(args),
            "declared": tool is not None,
            "schemaErrors": errors,
            "readOnly": bool(tool and tool.read_only),
            "destructive": bool(tool and tool.destructive),
            "destructiveUnconfirmed": destructive_unconfirmed,
            "error": result.error,
        })
        return result

    def _phase(self, name: str, args: Any) -> str:
        """A repeat of a call that just failed is a retry; anything else is an attempt."""
        for previous in reversed(self.calls):
            if previous["tool"] == name and previous["args"] == args:
                return "retry" if previous["error"] is not None else "attempt"
        return "attempt"

    def _respond(self, tool: MockTool, args: dict[str, Any]) -> CallResult:
        for rule in tool.responses:
            if "when" in rule and not holds(rule["when"], args=args, state=self.state):
                continue
            if "error" in rule:
                return CallResult(error=str(rule["error"]))
            output = resolve_template(rule["output"], args, self.state)
            if output is _MISSING:
                continue
            apply_effects(rule.get("effects") or [], args, self.state)
            return CallResult(output=copy.deepcopy(output))
        if "error" in tool.default:
            return CallResult(error=str(tool.default["error"]))
        output = resolve_template(tool.default.get("output"), args, self.state)
        return CallResult(output=None if output is _MISSING else output)


__all__ = ["CallResult", "MockEnvironment", "schema_errors"]
