"""The deployment manifest: one signed list of every report behind a deployment (D52).

A multi-model, multi-agent deployment is not certified by one report. It is
described by many: a bench report per model, a smith report per agent, gap
scans, audits. The manifest lists each one by the hash of its file bytes, so
invariant 4 holds per report — verifying any member is still `sha256sum` —
and the manifest itself is signed as evidence (`opaas evidence sign`).

It also says whether the members agree on the environment they were measured
in (N5). Members bound to different inventories describe different
environments, and a manifest that silently put them together would certify a
deployment that never existed at one time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opaas.core.hashing import file_digest


class ManifestError(ValueError):
    """A member cannot be read or identified."""


def _identify(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from None
    if not isinstance(document, dict):
        raise ManifestError(f"{path} is not an OPAAS document.")
    if "runId" in document and "model" in document:
        if document.get("referenceType") == "cloud-baseline":
            raise ManifestError(f"{path} is a cloud baseline: it cannot be part of a "
                                "deployment certificate (D22).")
        return {
            "kind": "model-report",
            "subject": (document.get("model") or {}).get("name"),
            "deploymentHash": (document.get("deploymentFingerprint") or {}).get("hash"),
            "inventoryHash": (document.get("environmentInventory") or {}).get("hash"),
            "coverage": (document.get("coverage") or {}).get("blocksEvaluated"),
        }
    if document.get("kind"):
        binding = document.get("environmentFingerprint") or {}
        return {
            "kind": document["kind"],
            "subject": (document.get("agent") or {}).get("adapter"),
            "inventoryHash": binding.get("inventoryHash"),
            "deploymentHash": binding.get("deploymentHash"),
            "certifiable": document.get("certifiable"),
        }
    raise ManifestError(f"{path} is neither a report nor an evidence document.")


def build_manifest(paths: list[Path], *, name: str | None = None) -> dict[str, Any]:
    if not paths:
        raise ManifestError("A manifest needs at least one member.")
    members = []
    for path in paths:
        members.append({"file": path.name, "hash": file_digest(path), **_identify(path)})
    inventories = sorted({m["inventoryHash"] for m in members if m.get("inventoryHash")})
    unbound = [m["file"] for m in members if not m.get("inventoryHash")
               and not m.get("deploymentHash")]
    findings = []
    if len(inventories) > 1:
        findings.append(f"Members are bound to {len(inventories)} different environment "
                        "inventories: they were not measured in one environment.")
    if unbound:
        findings.append(f"Bound to no environment: {', '.join(unbound)}.")
    not_certifiable = [m["file"] for m in members if m.get("certifiable") is False]
    if not_certifiable:
        findings.append("Built on reference scenarios only, not certifiable for this "
                        f"deployment: {', '.join(not_certifiable)}.")
    return {
        "deployment": name,
        "members": members,
        "environments": inventories,
        "consistentEnvironment": len(inventories) <= 1 and not unbound,
        "findings": findings,
        "note": "Each member is certified by the hash of its own bytes (invariant 4); this "
                "manifest adds no score and no aggregate of their results.",
    }


__all__ = ["ManifestError", "build_manifest"]
