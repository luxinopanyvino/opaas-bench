"""Structural policies and three-valued verdicts (N1, N2; D43).

A policy here is a predicate over *recorded fields*, nothing else: "no
`tool_call` targets `shell`", "every call to `delete_file` is preceded by an
approval", "at most 10 model calls per trace". There is no operator that asks
what a text *means*. A semantic policy needs a judge, a judge is a model, and
a verdict that depends on which model judged is not reproducible by a third
party (invariant 3). Those policies are out of scope, said so, until a
deterministic decision procedure for them exists (OP4).

**Verdicts have three values: comply, violate, undecidable** (N1). Predicates
are evaluated in Kleene's three-valued logic: a predicate over a field the
record does not carry is *unknown*, and unknown propagates. An undecidable
verdict is never rounded to either side, and it always names the fields whose
absence made it so. That list is the most useful thing an audit can hand an
operator: it is exactly what to start logging (Proposition 1).

The language, in JSON::

    {"id": "no-shell", "kind": "forbid",
     "scope": {"field": "action", "eq": "tool_call"},
     "match": {"field": "target", "in": ["shell", "bash"]}}

Kinds: `forbid` (no event in scope matches), `require` (every event in scope
that matches `when` satisfies `then`), `precedes` (every event matching
`match` has an earlier event matching `before`, optionally sharing the value
of `sameField`), `max_count` (at most `max` events match).

Predicates: `{"field", <op>: operand}` with ops `eq`, `ne`, `in`, `regex`,
`exists`, `gte`, `lte`, `contains`; and `all`, `any`, `not` to combine them.
Unknown keys are refused rather than ignored: a typo in a policy must not
turn it into a policy that always complies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opaas.core.hashing import sha256_json

POLICY_LANGUAGE_VERSION = 1

COMPLY, VIOLATE, UNDECIDABLE = "comply", "violate", "undecidable"
VERDICTS = (COMPLY, VIOLATE, UNDECIDABLE)

KINDS = ("forbid", "require", "precedes", "max_count")
_FIELD_OPS = ("eq", "ne", "in", "regex", "exists", "gte", "lte", "contains")
_COMBINATORS = ("all", "any", "not")
_MISSING = object()

#: How many event ids a verdict cites. A violation in ten thousand events does
#: not need ten thousand ids to be checkable.
MAX_CITED = 20


class PolicyError(ValueError):
    """A policy is malformed or uses something that is not structural."""


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    kind: str
    spec: dict[str, Any]
    description: str = ""
    #: What the sentinel gate does with a violation: "observe" only records it,
    #: "block" refuses the action, "approve" holds it for a human.
    enforce: str = "observe"


@dataclass(slots=True)
class Verdict:
    policy: str
    verdict: str
    #: Events that violate, for `violate`; events that could not be decided,
    #: for `undecidable`. Capped at MAX_CITED.
    events: list[str] = field(default_factory=list)
    #: Fields whose absence made the verdict undecidable (Proposition 1).
    missing: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"policy": self.policy, "verdict": self.verdict,
                               "detail": self.detail}
        if self.events:
            out["events"] = self.events
        if self.missing:
            out["missing"] = self.missing
        return out


# --------------------------------------------------------------------- loading


def load_policies(raw: Any) -> list[Policy]:
    """From a JSON list (or `{"policies": [...]}`), validated in full before any is used."""
    items = raw.get("policies") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise PolicyError("A policy set is a JSON list, or an object with a 'policies' list.")
    policies, seen = [], set()
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise PolicyError(f"Policy {position} is not an object.")
        policy_id = str(item.get("id") or "")
        if not policy_id:
            raise PolicyError(f"Policy {position} has no 'id'.")
        if policy_id in seen:
            raise PolicyError(f"Policy id '{policy_id}' is used twice.")
        seen.add(policy_id)
        kind = item.get("kind")
        if kind not in KINDS:
            raise PolicyError(
                f"Policy '{policy_id}': kind '{kind}' is not one of {', '.join(KINDS)}. "
                "Only structural policies are supported (N2)."
            )
        allowed = {"id", "kind", "description", "enforce", "scope"} | {
            "forbid": {"match"},
            "require": {"when", "then"},
            "precedes": {"match", "before", "sameField"},
            "max_count": {"match", "max", "groupBy"},
        }[kind]
        if unknown := set(item) - allowed:
            raise PolicyError(f"Policy '{policy_id}': unknown keys {sorted(unknown)}.")
        for key in ("scope", "match", "when", "then", "before"):
            if key in item:
                _validate(item[key], f"{policy_id}.{key}")
        required = {"forbid": ("match",), "require": ("when", "then"),
                    "precedes": ("match", "before"), "max_count": ("match", "max")}[kind]
        for key in required:
            if key not in item:
                raise PolicyError(f"Policy '{policy_id}' ({kind}) needs '{key}'.")
        enforce = item.get("enforce", "observe")
        if enforce not in ("observe", "block", "approve"):
            raise PolicyError(f"Policy '{policy_id}': enforce must be observe, block or approve.")
        policies.append(Policy(
            id=policy_id, kind=kind, description=str(item.get("description", "")),
            enforce=enforce,
            spec={k: v for k, v in item.items() if k not in ("id", "kind", "description",
                                                              "enforce")},
        ))
    return policies


def policies_hash(policies: Sequence[Policy]) -> str:
    """Travels with every verdict, like the refusal patterns' hash (D11)."""
    return sha256_json(
        {"language": POLICY_LANGUAGE_VERSION,
         "policies": [{"id": p.id, "kind": p.kind, "enforce": p.enforce, **p.spec}
                      for p in policies]}
    )


def _validate(predicate: Any, where: str) -> None:
    if not isinstance(predicate, dict) or not predicate:
        raise PolicyError(f"{where}: a predicate is a non-empty object.")
    combinators = set(predicate) & set(_COMBINATORS)
    if combinators:
        if len(predicate) != 1:
            raise PolicyError(f"{where}: '{next(iter(combinators))}' stands alone in its object.")
        (op, operand), = predicate.items()
        if op == "not":
            _validate(operand, f"{where}.not")
        else:
            if not isinstance(operand, list) or not operand:
                raise PolicyError(f"{where}.{op}: expects a non-empty list of predicates.")
            for index, child in enumerate(operand):
                _validate(child, f"{where}.{op}[{index}]")
        return
    if "field" not in predicate:
        raise PolicyError(f"{where}: a field predicate needs 'field'.")
    ops = set(predicate) - {"field"}
    if len(ops) != 1 or not ops <= set(_FIELD_OPS):
        raise PolicyError(
            f"{where}: exactly one operator from {', '.join(_FIELD_OPS)} is expected, "
            f"got {sorted(ops) or 'none'}."
        )
    if "regex" in ops:
        try:
            re.compile(str(predicate["regex"]))
        except re.error as exc:
            raise PolicyError(f"{where}: invalid regex: {exc}") from None


# ------------------------------------------------------------------ evaluation


def resolve(record: dict[str, Any], path: str) -> Any:
    """A dotted path into a record; list indices are numbers. `_MISSING` when absent."""
    current: Any = record
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit():
            index = int(part)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return _MISSING if current is None else current


def evaluate(predicate: dict[str, Any], record: dict[str, Any]) -> tuple[bool | None, set[str]]:
    """Kleene evaluation. Returns (True | False | None, fields that were missing)."""
    if "all" in predicate:
        results = [evaluate(p, record) for p in predicate["all"]]
        missing = set().union(*(m for _, m in results))
        if any(v is False for v, _ in results):
            return False, set()
        return (None, missing) if any(v is None for v, _ in results) else (True, set())
    if "any" in predicate:
        results = [evaluate(p, record) for p in predicate["any"]]
        missing = set().union(*(m for _, m in results))
        if any(v is True for v, _ in results):
            return True, set()
        return (None, missing) if any(v is None for v, _ in results) else (False, set())
    if "not" in predicate:
        value, missing = evaluate(predicate["not"], record)
        return (None if value is None else not value), missing

    path = str(predicate["field"])
    value = resolve(record, path)
    if "exists" in predicate:
        # Asking whether a field exists is always decidable.
        return (value is not _MISSING) == bool(predicate["exists"]), set()
    if value is _MISSING:
        return None, {path}
    if "eq" in predicate:
        return value == predicate["eq"], set()
    if "ne" in predicate:
        return value != predicate["ne"], set()
    if "in" in predicate:
        return value in list(predicate["in"]), set()
    if "regex" in predicate:
        return re.search(str(predicate["regex"]), str(value)) is not None, set()
    if "contains" in predicate:
        operand = predicate["contains"]
        if isinstance(value, list):
            return operand in value, set()
        return str(operand) in str(value), set()
    try:
        if "gte" in predicate:
            return float(value) >= float(predicate["gte"]), set()
        return float(value) <= float(predicate["lte"]), set()
    except (TypeError, ValueError):
        return None, {path}


def _in_scope(policy: Policy, record: dict[str, Any]) -> tuple[bool | None, set[str]]:
    scope = policy.spec.get("scope")
    return (True, set()) if scope is None else evaluate(scope, record)


def _and(a: tuple[bool | None, set[str]], b: tuple[bool | None, set[str]]):
    if a[0] is False or b[0] is False:
        return False, set()
    if a[0] is None or b[0] is None:
        return None, a[1] | b[1]
    return True, set()


def decide(policy: Policy, events: Sequence[dict[str, Any]]) -> Verdict:
    """The verdict of one policy over an ordered sequence of records."""
    return {
        "forbid": _forbid, "require": _require, "precedes": _precedes, "max_count": _max_count,
    }[policy.kind](policy, events)


def decide_all(policies: Iterable[Policy], events: Sequence[dict[str, Any]]) -> list[Verdict]:
    return [decide(policy, events) for policy in policies]


def _ident(record: dict[str, Any], position: int) -> str:
    return str(record.get("id") or f"#{position}")


def _forbid(policy: Policy, events: Sequence[dict[str, Any]]) -> Verdict:
    violating, unknown, missing = [], [], set()
    for position, record in enumerate(events):
        value, absent = _and(_in_scope(policy, record), evaluate(policy.spec["match"], record))
        if value is True:
            violating.append(_ident(record, position))
        elif value is None:
            unknown.append(_ident(record, position))
            missing |= absent
    return _settle(policy, violating, unknown, missing,
                   f"{len(violating)} forbidden event(s)")


def _require(policy: Policy, events: Sequence[dict[str, Any]]) -> Verdict:
    violating, unknown, missing = [], [], set()
    for position, record in enumerate(events):
        when, when_missing = _and(_in_scope(policy, record),
                                  evaluate(policy.spec["when"], record))
        if when is False:
            continue
        then, then_missing = evaluate(policy.spec["then"], record)
        if then is True:
            continue
        if when is True and then is False:
            violating.append(_ident(record, position))
        else:
            unknown.append(_ident(record, position))
            missing |= when_missing | then_missing
    return _settle(policy, violating, unknown, missing,
                   f"{len(violating)} event(s) failing the requirement")


def _precedes(policy: Policy, events: Sequence[dict[str, Any]]) -> Verdict:
    same = policy.spec.get("sameField")
    violating, unknown, missing = [], [], set()
    for position, record in enumerate(events):
        hit, hit_missing = _and(_in_scope(policy, record),
                                evaluate(policy.spec["match"], record))
        if hit is False:
            continue
        key = resolve(record, same) if same else None
        if same and key is _MISSING:
            unknown.append(_ident(record, position))
            missing |= hit_missing | {same}
            continue
        found, maybe, maybe_missing = False, False, set()
        for earlier in events[:position]:
            before, before_missing = evaluate(policy.spec["before"], earlier)
            if same:
                other = resolve(earlier, same)
                if other is _MISSING:
                    before, before_missing = (
                        (None, before_missing | {same}) if before is not False else (False, set())
                    )
                elif other != key:
                    before = False
            if before is True:
                found = True
                break
            if before is None:
                maybe, maybe_missing = True, maybe_missing | before_missing
        if found:
            continue
        if hit is True and not maybe:
            violating.append(_ident(record, position))
        else:
            unknown.append(_ident(record, position))
            missing |= hit_missing | maybe_missing
    return _settle(policy, violating, unknown, missing,
                   f"{len(violating)} event(s) without the required predecessor")


def _max_count(policy: Policy, events: Sequence[dict[str, Any]]) -> Verdict:
    limit = int(policy.spec["max"])
    group_by = policy.spec.get("groupBy")
    certain: dict[Any, list[str]] = {}
    possible: dict[Any, list[str]] = {}
    missing: set[str] = set()
    ungrouped: list[str] = []
    for position, record in enumerate(events):
        value, absent = _and(_in_scope(policy, record), evaluate(policy.spec["match"], record))
        if value is False:
            continue
        group: Any = None
        if group_by:
            group = resolve(record, group_by)
            if group is _MISSING:
                ungrouped.append(_ident(record, position))
                missing |= absent | {group_by}
                continue
            group = str(group)
        bucket = certain if value is True else possible
        bucket.setdefault(group, []).append(_ident(record, position))
        missing |= absent
    violating = [i for ids in certain.values() if len(ids) > limit for i in ids]
    unknown: list[str] = []
    # An event whose group is unknown could belong to any group, so it counts
    # against each of them.
    groups = (set(certain) | set(possible)) or ({None} if ungrouped else set())
    for group in groups:
        sure, maybe = certain.get(group, []), possible.get(group, [])
        if len(sure) <= limit < len(sure) + len(maybe) + len(ungrouped):
            unknown += [i for i in maybe + ungrouped if i not in unknown]
    if violating:
        return _settle(policy, violating, [], set(), f"more than {limit} matching event(s)")
    if unknown:
        return _settle(policy, [], unknown, missing, "")
    return _settle(policy, [], [], set(), "")


def _settle(policy: Policy, violating: list[str], unknown: list[str], missing: set[str],
            what: str) -> Verdict:
    if violating:
        return Verdict(policy.id, VIOLATE, violating[:MAX_CITED], [], what + ".")
    if unknown:
        names = sorted(missing)
        return Verdict(
            policy.id, UNDECIDABLE, unknown[:MAX_CITED], names,
            f"{len(unknown)} event(s) cannot be decided: the record does not carry "
            f"{', '.join(names) or 'the fields the policy reads'}.",
        )
    return Verdict(policy.id, COMPLY, [], [], "No event contradicts the policy.")


def policy_fields(policy: Policy) -> set[str]:
    """Every field the policy reads. A record schema without one of them makes it undecidable."""
    fields: set[str] = set()

    def walk(predicate: Any) -> None:
        if not isinstance(predicate, dict):
            return
        if "field" in predicate:
            fields.add(str(predicate["field"]))
        for op in _COMBINATORS:
            if op in predicate:
                children = predicate[op]
                for child in children if isinstance(children, list) else [children]:
                    walk(child)

    for key in ("scope", "match", "when", "then", "before"):
        walk(policy.spec.get(key))
    for key in ("sameField", "groupBy"):
        if policy.spec.get(key):
            fields.add(str(policy.spec[key]))
    return fields


__all__ = [
    "COMPLY",
    "KINDS",
    "MAX_CITED",
    "POLICY_LANGUAGE_VERSION",
    "UNDECIDABLE",
    "VERDICTS",
    "VIOLATE",
    "Policy",
    "PolicyError",
    "Verdict",
    "decide",
    "decide_all",
    "evaluate",
    "load_policies",
    "policies_hash",
    "policy_fields",
    "resolve",
]
