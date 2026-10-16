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

**Approvers are told, not left to poll (D64).** A held action waits for a
human who has no reason to look at the queue. The queue can notify them
through a webhook (a JSON POST) or a command (the same JSON on its standard
input). The notice carries the pending id, the event id and the request's
digest, and nothing else: no target, no actor chain, no kept field (N4). It
says *something is waiting, here is its id*; what it is, the approver reads
from the queue, on the host, with `opaas sentinel approvals`. A notice that
fails does not release or refuse the action: it stays held until decided or
timed out, and the pending event records whether each notice was delivered.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

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


#: What a notice says. Ids and a digest only: never content (N4).
NOTICE_TYPE = "opaas.approval.pending"


def notice(pending_id: str, event_id: str, digest: str) -> dict[str, str]:
    return {"type": NOTICE_TYPE, "pending": pending_id, "event": event_id, "digest": digest}


class Notifier(Protocol):
    """Tells approvers an action is waiting. Raises `GateError` when it could not."""

    def describe(self) -> dict[str, Any]: ...

    def __call__(self, payload: dict[str, str]) -> None: ...


@dataclass(slots=True)
class WebhookNotifier:
    """POSTs the notice as JSON. Any answer but 2xx is a failed notice."""

    url: str
    timeout_s: float = 5.0
    transport: httpx.BaseTransport | None = None

    def describe(self) -> dict[str, Any]:
        # Scheme, host and port only: chat webhooks carry their secret in the path.
        parsed = urlparse(self.url)
        port = f":{parsed.port}" if parsed.port else ""
        return {"channel": "webhook", "endpoint": f"{parsed.scheme}://{parsed.hostname}{port}"}

    def __call__(self, payload: dict[str, str]) -> None:
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                response = client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise GateError(f"webhook unreachable: {type(exc).__name__}") from None
        if not 200 <= response.status_code < 300:
            raise GateError(f"webhook answered HTTP {response.status_code}")


#: Runs a command with stdin text and extra environment; returns its exit status.
CommandRunner = Callable[[Sequence[str], str, dict[str, str], float], int]


def _run_command(argv: Sequence[str], stdin: str, env: dict[str, str], timeout_s: float) -> int:
    try:
        completed = subprocess.run(list(argv), input=stdin, text=True, capture_output=True,
                                   env={**os.environ, **env}, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise GateError(f"command did not finish in {timeout_s:g} s") from None
    except OSError as exc:
        raise GateError(f"command could not start: {type(exc).__name__}") from None
    return completed.returncode


@dataclass(slots=True)
class CommandNotifier:
    """Runs a command with the notice as JSON on stdin and in OPAAS_* variables.

    Its output is not read and not recorded: whatever the command prints is
    the operator's, not evidence.
    """

    argv: Sequence[str]
    timeout_s: float = 10.0
    runner: CommandRunner = field(default=_run_command)

    def __post_init__(self) -> None:
        if not self.argv:
            raise GateError("A notification command needs at least a program to run.")

    def describe(self) -> dict[str, Any]:
        # The program's name, never its arguments: those are where tokens live.
        return {"channel": "command", "program": Path(str(self.argv[0])).name}

    def __call__(self, payload: dict[str, str]) -> None:
        env = {"OPAAS_PENDING_ID": payload["pending"], "OPAAS_EVENT_ID": payload["event"],
               "OPAAS_ACTION_DIGEST": payload["digest"]}
        status = self.runner(self.argv, json.dumps(payload), env, self.timeout_s)
        if status != 0:
            raise GateError(f"command exited with status {status}")


class ApprovalQueue:
    def __init__(self, directory: Path, *, poll_s: float = 0.25,
                 notifiers: Sequence[Notifier] = ()) -> None:
        self.directory = directory
        self.poll_s = poll_s
        self.notifiers = list(notifiers)

    def notify(self, pending_id: str, *, event_id: str, digest: str) -> list[dict[str, Any]]:
        """Sends the notice through every notifier; what each did, for the record.

        A failure is reported, never raised: the action stays held either way.
        """
        payload = notice(pending_id, event_id, digest)
        outcomes = []
        for notifier in self.notifiers:
            outcome = notifier.describe()
            try:
                notifier(payload)
            except GateError as exc:
                outcome.update(delivered=False, reason=str(exc))
            else:
                outcome["delivered"] = True
            outcomes.append(outcome)
        return outcomes

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


__all__ = [
    "DECISIONS",
    "NOTICE_TYPE",
    "ApprovalQueue",
    "CommandNotifier",
    "CommandRunner",
    "Decision",
    "GateError",
    "Notifier",
    "WebhookNotifier",
    "notice",
]
