"""Structured report of a run.

The report is the source of truth: the credential is signed over its hash and
the Phase 5 certificate page renders it. By design **there is no aggregate
score and no tiers** — only per-category results.

The file is written in canonical JCS bytes and **with nothing wrapping it**,
so its hash is the hash of the file's bytes. Verifying a report is
`sha256sum report.json`: nothing has to be re-canonicalized and no subtree has
to be extracted. `RunReport.content_hash()` and `hashing.file_digest()` match
by construction, and a test pins that down.
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from opaas import OPAAS_SPEC_VERSION, __version__
from opaas.canonical import canonicalize
from opaas.hashing import sha256_json
from opaas.telemetry.gpu import query_gpus

REPORT_FILENAME = "report.json"


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        protected_namespaces=(),
    )


class ModelInfo(_Base):
    name: str
    source: str | None = None
    quantization: str | None = None
    weights_hash: dict[str, Any] = Field(default_factory=dict)
    #: Chat template, tokenizer and generation defaults beside the weights (D35).
    tokenizer_files_hash: dict[str, Any] = Field(default_factory=dict)


class ConfigurationInfo(_Base):
    backend: str
    hardware: str
    os: str
    config_hash: str
    backend_metadata: dict[str, Any] = Field(default_factory=dict)
    effective_config: dict[str, Any] = Field(default_factory=dict)


class SuiteResult(_Base):
    """Result of one block (A–E). `metrics` is free-form per block."""

    suite: str
    block: str
    status: str = "completed"
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    error: str | None = None


class RunReport(_Base):
    run_id: str
    opaas_version: str = OPAAS_SPEC_VERSION
    tool_version: str = __version__
    started_at: str
    finished_at: str | None = None
    status: str = "evaluated"
    #: 'on-prem' measures your own deployment; 'cloud-baseline' is a quality
    #: reference for the same model under someone else's conditions and is not
    #: mixed into block A.
    reference_type: str = "on-prem"
    model: ModelInfo
    configuration: ConfigurationInfo
    #: What was serving: engine version, model digest, chat template, GPUs and
    #: driver, as the deployment reported them. `configHash` identifies how
    #: OPAAS ran; this identifies what it ran against (D34).
    deployment_fingerprint: dict[str, Any] = Field(default_factory=dict)
    #: What `opaas watch` compares against between certifications: canary
    #: output hashes and a latency interval, signed with the rest (D36).
    sentinel_baseline: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    suites: list[SuiteResult] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    #: Context the operator states about the run ("shared workstation", "engine
    #: restarted before block A"). Kept apart from `limitations`, which is what
    #: the tool itself could not measure: a claim nobody checked is never mixed
    #: with a finding (D14, D31).
    operator_notes: list[str] = Field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def canonical_bytes(self) -> bytes:
        """The exact bytes written to disk and signed over."""
        return canonicalize(self.payload())

    def content_hash(self) -> str:
        return sha256_json(self.payload())

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.canonical_bytes())
        return path


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def describe_environment() -> dict[str, Any]:
    """Snapshot of the host running the evaluation."""
    snapshot = query_gpus()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "osDetail": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "gpus": [
            {
                "name": gpu.get("name"),
                "vramTotalMb": gpu.get("memory_total_mb"),
                "driverVersion": gpu.get("driver_version"),
            }
            for gpu in snapshot.gpus
        ],
        "gpuTelemetryNote": snapshot.note,
    }


def describe_hardware(environment: dict[str, Any]) -> str:
    """Human-readable hardware string for `configuration.hardware` in the credential."""
    gpus = environment.get("gpus") or []
    if not gpus:
        return "no NVIDIA GPU detected"
    first = gpus[0]
    vram = first.get("vramTotalMb")
    vram_gb = f" {round(vram / 1024)}GB" if isinstance(vram, (int, float)) else ""
    return f"{len(gpus)}x {first.get('name')}{vram_gb}"
