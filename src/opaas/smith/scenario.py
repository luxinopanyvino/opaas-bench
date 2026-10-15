"""Scenarios: fixed situations whose correct outcome is known (D47).

A scenario is a JSON file holding an initial state, mock tools with
deterministic responses, the user's task, optional adversarial content
planted in tool outputs, and the expected end state and policies as
structural predicates. Because every tool is a mock that OPAAS controls, the
mock *is* the ground truth: every call the agent makes is seen with all of
its fields, whatever the agent itself records.

**JSON, not YAML.** The work plan said YAML. YAML would add the first
dependency outside the evidence stack, and a scenario is hashed: JSON parsed
and canonicalized through JCS gives a hash a third party reproduces in any
language. The cost is comments, which a scenario carries in `notes` instead.

**Hashed like a dataset.** `scenario_hash` is over the canonical content, so
line endings or key order do not change it, and a run records the hash of
every scenario it used. A scenario that does not validate stops the run
before any agent is called.

Mock-tool responses are rules tried in order. `when` is a predicate over the
call's arguments (`{"arg": "path", "eq": "/x"}`) or the current state
(`{"state": ["files", "/x"], "exists": true}`); `output` is a value, where
`{"$arg": "name"}` and `{"$state": [path…]}` are substituted; `effects`
change the state (`set`, `delete`, `append`); `error` makes the call fail.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opaas.core.hashing import sha256_json
from opaas.core.policy import Policy, PolicyError, load_policies

SCENARIO_FORMAT_VERSION = 1

#: What `opaas audit template` writes where only the operator can decide. A
#: scenario that still carries it is refused: an unfinished template counted
#: as the operator's own would make an agent certifiable on a test nobody
#: designed (D60, §10.4).
TEMPLATE_MARKER = "TO COMPLETE"

CATEGORIES = ("task", "injection", "permission", "destructive", "loop", "schema")

#: Where the reference scenarios ship. A certificate built only on them is
#: flagged, as `opaas-smoke` is: they test generic behaviour, not this
#: deployment's tasks.
REFERENCE_DIR = Path(__file__).parent / "scenarios" / "reference"

_MISSING = object()


class ScenarioError(ValueError):
    """A scenario is malformed. The message names the scenario and the field."""


@dataclass(frozen=True, slots=True)
class MockTool:
    name: str
    description: str
    parameters: dict[str, Any]
    responses: tuple[dict[str, Any], ...]
    default: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    #: Calling this tool is asking the user to confirm: it is recorded as an
    #: approval, and it unlocks destructive tools for the rest of the run.
    confirmation: bool = False

    def public(self) -> dict[str, Any]:
        """What the agent is told about the tool. Never its responses."""
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    version: int
    title: str
    category: str
    task: str
    state: dict[str, Any]
    tools: tuple[MockTool, ...]
    expected: tuple[dict[str, Any], ...]
    policies: tuple[Policy, ...]
    canaries: tuple[str, ...]
    max_steps: int
    hash: str
    source: str
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def tool(self, name: str) -> MockTool | None:
        return next((t for t in self.tools if t.name == name), None)

    def initial_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)


def scenario_hash(raw: dict[str, Any]) -> str:
    return sha256_json(raw)


def parse_scenario(raw: Any, *, source: str = "operator", where: str = "scenario") -> Scenario:
    """Validates everything before returning: a half-valid scenario is no scenario."""
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: a scenario is a JSON object.")
    sid = str(raw.get("id") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", sid):
        raise ScenarioError(f"{where}: 'id' must be lowercase letters, digits, '.', '_', '-'.")
    where = f"scenario '{sid}'"
    if TEMPLATE_MARKER in json.dumps(raw, ensure_ascii=False):
        raise ScenarioError(
            f"{where} is a template still to complete: replace every '{TEMPLATE_MARKER}' "
            "with what this deployment's users ask and what a correct answer is."
        )
    if raw.get("format", SCENARIO_FORMAT_VERSION) != SCENARIO_FORMAT_VERSION:
        raise ScenarioError(f"{where}: format {raw.get('format')} is not supported.")
    allowed = {"id", "format", "version", "title", "category", "task", "state", "tools",
               "expected", "policies", "adversarial", "budget", "notes"}
    if unknown := set(raw) - allowed:
        raise ScenarioError(f"{where}: unknown keys {sorted(unknown)}.")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise ScenarioError(f"{where}: category must be one of {', '.join(CATEGORIES)}.")
    if not isinstance(raw.get("task"), str) or not raw["task"].strip():
        raise ScenarioError(f"{where}: 'task' is the user's request, a non-empty string.")
    state = raw.get("state", {})
    if not isinstance(state, dict):
        raise ScenarioError(f"{where}: 'state' must be an object.")

    tools = []
    for index, item in enumerate(raw.get("tools") or []):
        tools.append(_tool(item, f"{where}.tools[{index}]"))
    if not tools:
        raise ScenarioError(f"{where}: a scenario without tools tests nothing an agent does.")
    if len({t.name for t in tools}) != len(tools):
        raise ScenarioError(f"{where}: two tools share a name.")

    expected = raw.get("expected") or []
    if not isinstance(expected, list):
        raise ScenarioError(f"{where}: 'expected' is a list of end-state predicates.")
    for index, predicate in enumerate(expected):
        _check_state_predicate(predicate, f"{where}.expected[{index}]")
    try:
        policies = tuple(load_policies(raw.get("policies") or []))
    except PolicyError as exc:
        raise ScenarioError(f"{where}: {exc}") from None

    canaries = []
    for index, item in enumerate(raw.get("adversarial") or []):
        if not isinstance(item, dict) or item.get("kind") not in ("injection", "bait"):
            raise ScenarioError(f"{where}.adversarial[{index}]: kind is injection or bait.")
        if item["kind"] == "injection":
            canary = str(item.get("canary") or "")
            if len(canary) < 8:
                raise ScenarioError(f"{where}.adversarial[{index}]: an injection needs a "
                                    "canary of at least 8 characters, unlikely by chance.")
            if canary not in json.dumps(raw.get("tools")):
                raise ScenarioError(f"{where}.adversarial[{index}]: the canary does not "
                                    "appear in any tool output, so nothing plants it.")
            canaries.append(canary)
    max_steps = int((raw.get("budget") or {}).get("maxSteps", 10))
    if max_steps < 1:
        raise ScenarioError(f"{where}: budget.maxSteps must be at least 1.")

    return Scenario(
        id=sid, version=int(raw.get("version", 1)), title=str(raw.get("title", sid)),
        category=str(category), task=raw["task"].strip(), state=state, tools=tuple(tools),
        expected=tuple(expected), policies=policies, canaries=tuple(canaries),
        max_steps=max_steps, hash=scenario_hash(raw), source=source,
        notes=str(raw.get("notes", "")), raw=raw,
    )


def _tool(item: Any, where: str) -> MockTool:
    if not isinstance(item, dict) or not item.get("name"):
        raise ScenarioError(f"{where}: a tool needs a 'name'.")
    allowed = {"name", "description", "parameters", "responses", "default", "readOnly",
               "destructive", "confirmation"}
    if unknown := set(item) - allowed:
        raise ScenarioError(f"{where}: unknown keys {sorted(unknown)}.")
    parameters = item.get("parameters") or {"type": "object", "properties": {}}
    if parameters.get("type") != "object":
        raise ScenarioError(f"{where}: parameters must be a JSON schema of type object.")
    responses = item.get("responses") or []
    for index, rule in enumerate(responses):
        if not isinstance(rule, dict) or not ({"output", "error"} & set(rule)):
            raise ScenarioError(f"{where}.responses[{index}]: needs 'output' or 'error'.")
        if "when" in rule:
            _check_call_predicate(rule["when"], f"{where}.responses[{index}].when")
        for effect in rule.get("effects") or []:
            if effect.get("op") not in ("set", "delete", "append") or "path" not in effect:
                raise ScenarioError(f"{where}.responses[{index}]: effects are "
                                    "set/delete/append with a path.")
    return MockTool(
        name=str(item["name"]), description=str(item.get("description", "")),
        parameters=parameters, responses=tuple(responses),
        default=item.get("default") or {"error": "The tool could not handle this request."},
        read_only=bool(item.get("readOnly", False)),
        destructive=bool(item.get("destructive", False)),
        confirmation=bool(item.get("confirmation", False)),
    )


_OPS = ("eq", "ne", "in", "regex", "exists", "contains", "notContains")


def _check_call_predicate(predicate: Any, where: str) -> None:
    _check_predicate(predicate, where, ("arg", "state"))


def _check_state_predicate(predicate: Any, where: str) -> None:
    _check_predicate(predicate, where, ("state", "final"))


def _check_predicate(predicate: Any, where: str, subjects: tuple[str, ...]) -> None:
    if not isinstance(predicate, dict) or not predicate:
        raise ScenarioError(f"{where}: a predicate is a non-empty object.")
    for combinator in ("all", "any"):
        if combinator in predicate:
            for index, child in enumerate(predicate[combinator]):
                _check_predicate(child, f"{where}.{combinator}[{index}]", subjects)
            return
    if "not" in predicate:
        _check_predicate(predicate["not"], f"{where}.not", subjects)
        return
    subject = [s for s in subjects if s in predicate]
    ops = [o for o in _OPS if o in predicate]
    if len(subject) != 1 or len(ops) != 1:
        raise ScenarioError(
            f"{where}: one subject from {', '.join(subjects)} and one operator from "
            f"{', '.join(_OPS)} are expected."
        )


# ------------------------------------------------------------------- evaluation


def _lookup(container: Any, path: list[Any]) -> Any:
    current = container
    for part in path:
        if isinstance(current, dict) and str(part) in current:
            current = current[str(part)]
        elif isinstance(current, list) and isinstance(part, int) and -len(current) <= part < len(
            current
        ):
            current = current[part]
        else:
            return _MISSING
    return current


def resolve_template(value: Any, args: dict[str, Any], state: dict[str, Any]) -> Any:
    """Substitutes `{"$arg": …}` and `{"$state": […]}`; `_MISSING` if either is absent."""
    if isinstance(value, dict):
        if set(value) == {"$arg"}:
            return _lookup(args, str(value["$arg"]).split("."))
        if set(value) == {"$state"}:
            path = [resolve_template(p, args, state) for p in value["$state"]]
            return _MISSING if _MISSING in path else _lookup(state, path)
        resolved = {k: resolve_template(v, args, state) for k, v in value.items()}
        return _MISSING if _MISSING in resolved.values() else resolved
    if isinstance(value, list):
        resolved_list = [resolve_template(v, args, state) for v in value]
        return _MISSING if _MISSING in resolved_list else resolved_list
    return value


def holds(
    predicate: dict[str, Any],
    *,
    args: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    final: str | None = None,
) -> bool:
    """Two-valued: the mock knows its own state completely, so nothing is unknown here."""
    args, state = args or {}, state or {}
    if "all" in predicate:
        return all(holds(p, args=args, state=state, final=final) for p in predicate["all"])
    if "any" in predicate:
        return any(holds(p, args=args, state=state, final=final) for p in predicate["any"])
    if "not" in predicate:
        return not holds(predicate["not"], args=args, state=state, final=final)
    if "arg" in predicate:
        value = _lookup(args, str(predicate["arg"]).split("."))
    elif "state" in predicate:
        path = [resolve_template(p, args, state) for p in predicate["state"]]
        value = _MISSING if _MISSING in path else _lookup(state, path)
    else:
        value = final if final is not None else _MISSING
    if "exists" in predicate:
        return (value is not _MISSING) == bool(predicate["exists"])
    if value is _MISSING:
        return False
    if "eq" in predicate:
        return value == predicate["eq"]
    if "ne" in predicate:
        return value != predicate["ne"]
    if "in" in predicate:
        return value in predicate["in"]
    if "regex" in predicate:
        return re.search(str(predicate["regex"]), _text(value), re.I) is not None
    if "contains" in predicate:
        return _contains(value, predicate["contains"])
    return not _contains(value, predicate["notContains"])


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _contains(value: Any, needle: Any) -> bool:
    if isinstance(value, list):
        return needle in value or any(str(needle) in _text(v) for v in value)
    return str(needle).lower() in _text(value).lower()


def apply_effects(effects: list[dict[str, Any]], args: dict[str, Any],
                  state: dict[str, Any]) -> None:
    for effect in effects:
        path = [resolve_template(p, args, state) for p in effect["path"]]
        if _MISSING in path or not path:
            continue
        parent = _lookup(state, path[:-1]) if len(path) > 1 else state
        if not isinstance(parent, dict):
            continue
        key = str(path[-1])
        if effect["op"] == "delete":
            parent.pop(key, None)
            continue
        value = resolve_template(effect.get("value"), args, state)
        if value is _MISSING:
            continue
        if effect["op"] == "set":
            parent[key] = value
        else:
            parent.setdefault(key, [])
            if isinstance(parent[key], list):
                parent[key].append(value)


# ---------------------------------------------------------------------- loading


def load_scenario(path: Path, *, source: str = "operator") -> Scenario:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from None
    return parse_scenario(raw, source=source, where=str(path.name))


def load_scenarios(target: str | Path) -> list[Scenario]:
    """`reference`, a directory of `*.json`, or one file. Sorted by id."""
    if str(target) == "reference":
        paths, source = sorted(REFERENCE_DIR.glob("*.json")), "reference"
    else:
        path = Path(target)
        if path.is_dir():
            paths, source = sorted(path.glob("*.json")), "operator"
        elif path.is_file():
            paths, source = [path], "operator"
        else:
            raise ScenarioError(f"{target}: no such scenario file or directory.")
    scenarios = [load_scenario(p, source=source) for p in paths]
    if not scenarios:
        raise ScenarioError(f"{target}: no scenario found.")
    ids = [s.id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ScenarioError(f"{target}: two scenarios share an id.")
    return sorted(scenarios, key=lambda s: s.id)


def scenario_set_hash(scenarios: list[Scenario]) -> str:
    return sha256_json(sorted([s.id, s.hash] for s in scenarios))


__all__ = [
    "CATEGORIES",
    "REFERENCE_DIR",
    "SCENARIO_FORMAT_VERSION",
    "TEMPLATE_MARKER",
    "MockTool",
    "Scenario",
    "ScenarioError",
    "apply_effects",
    "holds",
    "load_scenario",
    "load_scenarios",
    "parse_scenario",
    "resolve_template",
    "scenario_hash",
    "scenario_set_hash",
]
