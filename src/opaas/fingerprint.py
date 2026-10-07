"""Deployment fingerprint: what was serving, as opposed to how OPAAS ran (D34).

`configHash` identifies the run's own configuration: suites, options, prompt
mode. It says nothing about the deployment underneath. Re-pull the model onto
different weights under the same tag, upgrade the engine or the GPU driver,
and the configuration hash stays the same while the certificate quietly stops
describing what is serving. The fingerprint records that identity in the
signed report, so a later check (the proposed sentinel, D33) has something
signed to compare against.

**Only what the deployment reports about itself goes in.** A value the
operator declared (`--quantization`) or that OPAAS chose (`--num-ctx`, the
prompt mode) is not evidence of what is serving, and it already lives in the
configuration. Mixing it in would let a declaration pass for an observation
(D14).

**An unknown field is listed, never guessed.** `unknown` maps each field the
deployment did not expose to the reason. Two fingerprints with the same `hash`
match on every field that was exposed; a field unknown on both sides proves
nothing, and whoever compares them has to say so.
"""

from __future__ import annotations

from typing import Any

from opaas.adapters.base import InferenceBackend
from opaas.hashing import sha256_json

#: Why each field can be missing, in the words the report uses.
_UNKNOWN_REASONS = {
    "engineVersion": "The engine did not report its version.",
    "modelDigest": "The engine does not publish a digest of the model it serves "
    "(Ollama does; vLLM and generic OpenAI-compatible servers do not). The model id is "
    "a name, and a name can be re-pulled onto different weights.",
    "chatTemplateHash": "The engine does not expose the chat template it applies.",
}


def deployment_fingerprint(
    backend: InferenceBackend, metadata: dict[str, Any], environment: dict[str, Any]
) -> dict[str, Any]:
    """The identity of the serving deployment, with what could not be established."""
    gpus = environment.get("gpus") or []
    drivers = sorted({str(gpu["driverVersion"]) for gpu in gpus if gpu.get("driverVersion")})
    fields: dict[str, Any] = {
        "engine": metadata.get("engine"),
        "engineVersion": metadata.get("engineVersion"),
        "model": backend.model_id,
        "modelDigest": backend.model_digest(),
        "chatTemplateHash": backend.chat_template_hash(),
        "gpus": [str(gpu.get("name")) for gpu in gpus],
        # Several GPUs on one host share a driver; a set with more than one
        # entry is itself worth seeing.
        "gpuDrivers": drivers,
    }

    unknown = {
        name: _UNKNOWN_REASONS.get(name, "Not reported.")
        for name, value in fields.items()
        if value is None and name != "engine"
    }
    if fields["engine"] is None:
        unknown["engine"] = "The adapter did not name its engine."
    if not gpus:
        note = environment.get("gpuTelemetryNote") or "No GPU was detected."
        unknown["gpus"] = unknown["gpuDrivers"] = str(note)
    elif not drivers:
        unknown["gpuDrivers"] = "nvidia-smi did not report a driver version."

    return {"fields": fields, "unknown": unknown, "hash": sha256_json(fields)}
