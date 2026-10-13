"""Inventory watch: has the environment left the fingerprint results are bound to? (N5)

`opaas watch` asks whether one certificate still describes one deployment.
This asks the wider question, on a schedule: is the environment still the
one every result was measured in? It re-runs `discover` and compares with a
signed baseline inventory, and it alerts on what voids a bound result:

- a model served now that no certificate covers;
- an engine upgraded, or its bind scope widened (loopback → network);
- an MCP server, tool definition, skill or container image added or changed.

Like `watch`, it has three outcomes and no score: `current`, `drifted`,
`inconclusive`. It signs nothing; it runs next to production, where no key
belongs.
"""

from __future__ import annotations

from typing import Any

from opaas.discover import compare_discoveries

#: Narrowest first. Moving right is widening.
BIND_SCOPES = ("loopback", "network", "all-interfaces")

#: Changes that void a result bound to the baseline.
_DRIFT = (
    "enginesUpgraded", "enginesRemoved", "modelsChanged", "modelsRemoved",
    "mcpServersAdded", "mcpServersChanged", "toolsAdded", "toolsChanged",
    "skillsAdded", "skillsChanged", "containersChanged", "containersAdded",
)


def _served(document: dict[str, Any]) -> set[str]:
    return {str(m.get("name")) for e in document.get("engines", []) for m in e.get("models", [])}


def check_environment(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    certified_models: set[str] | None = None,
) -> dict[str, Any]:
    """Settles the state and lists every alert with the change behind it."""
    if not baseline.get("available", True) or not current.get("available", True):
        return {"state": "inconclusive", "alerts": [],
                "detail": "Discovery was unavailable for the baseline or now."}

    changes = compare_discoveries(baseline, current)
    alerts: list[dict[str, Any]] = []

    known = certified_models if certified_models is not None else _served(baseline)
    for name in sorted(_served(current) - known):
        alerts.append({
            "alert": "uncertifiedModel", "name": name,
            "detail": f"{name} is served and "
            + ("no certificate given covers it." if certified_models is not None
               else "was not served when the baseline was taken."),
        })
    for change in changes.get("bindChanged", []):
        then, now = change.get("certified"), change.get("current")
        if then in BIND_SCOPES and now in BIND_SCOPES and (
            BIND_SCOPES.index(now) > BIND_SCOPES.index(then)
        ):
            alerts.append({"alert": "bindWidened", **change,
                           "detail": f"{change['kind']} on :{change['port']} widened from "
                                     f"{then} to {now}."})
    for kind in _DRIFT:
        for change in changes.get(kind, []):
            alerts.append({"alert": kind, **change,
                           "detail": f"{kind}: {change.get('name') or change.get('kind')}"})

    if alerts:
        state, detail = "drifted", f"{len(alerts)} alert(s): the environment left the baseline."
    else:
        others = sum(len(v) for v in changes.values())
        state = "current"
        detail = (f"{others} change(s), none that voids a bound result." if others
                  else "The environment matches the baseline.")
    return {
        "state": state,
        "detail": detail,
        "baselineHash": baseline.get("hash"),
        "currentHash": current.get("hash"),
        "alerts": alerts,
        "changes": changes,
    }


__all__ = ["BIND_SCOPES", "check_environment"]
