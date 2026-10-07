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

**Weights on disk count only once the server is seen holding them (D35).**
`--weights-path` is the operator pointing at files; hashing them proves what
is in the files, not that the engine loaded them. When the server runs on this
host, the files its process tree has mapped or open are compared with the
declared ones. Only a full match puts `weightsHash` and `tokenizerFilesHash`
in the fingerprint; anything less leaves them unknown, with `weightsCheck`
saying why. The declared hash stays in `model.weightsHash` either way.

**An unknown field is listed, never guessed.** `unknown` maps each field the
deployment did not expose to the reason. Two fingerprints with the same `hash`
match on every field that was exposed; a field unknown on both sides proves
nothing, and whoever compares them has to say so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opaas.adapters.base import InferenceBackend
from opaas.hashing import sha256_json
from opaas.telemetry.system import files_held_by, normalise_path

#: Why each field can be missing, in the words the report uses.
_UNKNOWN_REASONS = {
    "engineVersion": "The engine did not report its version.",
    "modelDigest": "The engine does not publish a digest of the model it serves "
    "(Ollama does; vLLM and generic OpenAI-compatible servers do not). The model id is "
    "a name, and a name can be re-pulled onto different weights.",
    "chatTemplateHash": "The engine does not expose the chat template it applies.",
}


def deployment_fingerprint(
    backend: InferenceBackend,
    metadata: dict[str, Any],
    environment: dict[str, Any],
    *,
    weights_path: Path | None = None,
    weights: dict[str, Any] | None = None,
    tokenizer_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The identity of the serving deployment, with what could not be established.

    `weights` and `tokenizer_files` are the results of `hashing.weights_hash`
    and `hashing.tokenizer_files_hash` over `weights_path`, computed once by
    the runner: hashing 40 GB twice to fill two fields would be absurd.
    """
    weights_check = _check_weights_loaded(backend, weights_path, weights or {})
    confirmed = weights_check["status"] == "confirmed"
    tokenizer_files = tokenizer_files or {}
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
        "weightsHash": (weights or {}).get("hash") if confirmed else None,
        "tokenizerFilesHash": tokenizer_files.get("hash") if confirmed else None,
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
    if not confirmed:
        unknown["weightsHash"] = unknown["tokenizerFilesHash"] = (
            f"Weights {weights_check['status']}: {weights_check['detail']}"
        )
    elif fields["tokenizerFilesHash"] is None:
        unknown["tokenizerFilesHash"] = str(
            tokenizer_files.get("note") or "No tokenizer or template files were found."
        )

    return {
        "fields": fields,
        "unknown": unknown,
        "hash": sha256_json(fields),
        "weightsCheck": weights_check,
    }


def _check_weights_loaded(
    backend: InferenceBackend, weights_path: Path | None, weights: dict[str, Any]
) -> dict[str, Any]:
    """Whether the server's own processes hold the declared weight files."""
    if weights_path is None:
        return {
            "status": "not-declared",
            "detail": "No --weights-path was given, so there are no files to compare.",
        }
    if not weights.get("available"):
        return {"status": "not-hashed", "detail": str(weights.get("note"))}

    pid = backend.server_pid()
    if pid is None:
        return {
            "status": "unverifiable",
            "detail": "The server process could not be identified on this host (a remote "
            "server, or no permission to enumerate connections). The files were hashed but "
            "nothing shows the engine loaded them.",
        }
    held, reason = files_held_by(pid)
    if held is None:
        return {"status": "unverifiable", "detail": str(reason)}

    base = weights_path if weights_path.is_dir() else weights_path.parent
    expected = {normalise_path(str(base / entry["path"])) for entry in weights["files"]}
    found = expected & held
    if found == expected:
        return {
            "status": "confirmed",
            "detail": f"All {len(expected)} declared weight file(s) are mapped or open by "
            f"the server process {pid} or its children.",
        }
    if found:
        return {
            "status": "partial",
            "detail": f"Only {len(found)} of {len(expected)} declared weight files are held by "
            "the server process tree. It may be serving a different set.",
        }
    return {
        "status": "not-observed",
        "detail": "None of the declared weight files is mapped or open by the server process "
        "tree. Engines that copy the weights to the GPU and then release the mapping (vLLM "
        "does) look exactly like this: it does not show that other weights were loaded, only "
        "that these could not be confirmed.",
    }
