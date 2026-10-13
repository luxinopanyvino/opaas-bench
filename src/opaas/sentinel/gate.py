"""The approval queue behind the sentinel's gate mode (D50).

When a gated policy holds an action, the proxy writes a *pending* entry here
and waits; a human approves or denies it with `opaas sentinel approve` /
`deny`, and the proxy forwards or blocks accordingly. A timeout is a denial:
an action nobody approved does not happen.

The queue is a directory of JSON files, not a service. The proxy and the
approver may run as different users on the same host, and a directory is the
one interface both already have. Each decision names who made it, and the
proxy records that decision as an `approval` event whose actor chain starts
at that person: a human approval that leaves no attributable record is
exactly the gap rule G06 looks for.

**What a pending entry shows the approver** is what the record keeps: target,
actor chain, kept fields and digests (N4). An approver who needs the full
request has to look at it at the source; the queue never becomes a second
copy of user traffic.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opaas.core.events import utcnow_iso

DECISIONS = ("approve", "deny")


class GateError(RuntimeError):
    """The queue cannot be read or written, or an entry does not exist."""


@dataclass(slots=True)
class Decision:
    decision: str
    by: str
    at: str
    note: str = ""


class ApprovalQueue:
    def __init__(self, directory: Path, *, poll_s: float = 0.25) -> None:
        self.directory = directory
        self.poll_s = poll_s

    def submit(self, entry: dict[str, Any]) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        pending_id = uuid.uuid4().hex[:12]
        self._write(self.directory / f"{pending_id}.pending.json",
                    {"id": pending_id, "submittedAt": utcnow_iso(), **entry})
        return pending_id

    def wait(self, pending_id: str, timeout_s: float) -> Decision | None:
        """The decision, or None when nobody decided in time."""
        deadline = time.monotonic() + timeout_s
        path = self.directory / f"{pending_id}.decision.json"
        while True:
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    return Decision(str(raw["decision"]), str(raw["by"]), str(raw["at"]),
                                    str(raw.get("note", "")))
                except (OSError, json.JSONDecodeError, KeyError):
                    pass  # half-written by the approver: read it on the next poll
            if time.monotonic() >= deadline:
                self._close(pending_id, "timeout")
                return None
            time.sleep(self.poll_s)

    def decide(self, pending_id: str, decision: str, *, by: str, note: str = "") -> Decision:
        if decision not in DECISIONS:
            raise GateError(f"A decision is {' or '.join(DECISIONS)}.")
        if not by.strip():
            raise GateError("A decision needs the name of who made it: an anonymous approval "
                            "cannot be attributed.")
        if not (self.directory / f"{pending_id}.pending.json").exists():
            raise GateError(f"No pending action '{pending_id}' in {self.directory}.")
        made = Decision(decision, by.strip(), utcnow_iso(), note)
        self._write(self.directory / f"{pending_id}.decision.json",
                    {"decision": made.decision, "by": made.by, "at": made.at,
                     "note": made.note})
        return made

    def pending(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        entries = []
        for path in sorted(self.directory.glob("*.pending.json")):
            pending_id = path.name.removesuffix(".pending.json")
            if (self.directory / f"{pending_id}.decision.json").exists() or (
                self.directory / f"{pending_id}.closed.json"
            ).exists():
                continue
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return entries

    def _close(self, pending_id: str, reason: str) -> None:
        self._write(self.directory / f"{pending_id}.closed.json",
                    {"reason": reason, "at": utcnow_iso()})

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        # Write then rename, so a reader never sees half a decision.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(temporary, path)


__all__ = ["DECISIONS", "ApprovalQueue", "Decision", "GateError"]
