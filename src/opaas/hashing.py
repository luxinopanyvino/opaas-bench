"""Model and configuration hashes, and run identifiers.

Step 1 of the end-to-end flow is "model + config selection → hash of both".
Everything computed here ends up in the credential's `credentialSubject`, so
the canonicalization has to be stable across runs and machines.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opaas.canonical import canonicalize, canonicalize_str

#: Weight files that get hashed when pointing at a model directory.
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")

_CHUNK = 1024 * 1024


def canonical_json(payload: Any) -> str:
    """Canonical RFC 8785 JSON. See `opaas.canonical`."""
    return canonicalize_str(payload)


def sha256_json(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(payload)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    """Hash of the file's exact bytes.

    This is what gets signed: verifying a credential comes down to `sha256sum`
    of the report received, with no need to re-canonicalize anything.
    """
    return "sha256:" + sha256_file(path)


def text_file_digest(path: Path) -> str:
    """Hash of a text file's content, with line endings normalized.

    For datasets the *content* is hashed, not the bytes: the same dataset
    cloned on Windows arrives with CRLF and on Linux with LF, and a certificate
    claiming they are different datasets would be false. The repo's
    `.gitattributes` forces LF, but a dataset created by a third party on
    Windows never goes through it.

    In exchange, this hash does not match the file's `sha256sum`. That is
    deliberate: it answers "are these the same items?", not "are these the same
    bytes?".
    """
    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def config_hash(config: dict[str, Any]) -> str:
    """Hash of the run's effective configuration.

    Fields that do not change the model's behavior but do vary between hosts
    (server URL, credentials, timeouts) are excluded.
    """
    volatile = {"base_url", "api_key", "timeout_s", "output_dir", "transport"}
    return sha256_json({k: v for k, v in sorted(config.items()) if k not in volatile})


def weights_hash(path: Path | None) -> dict[str, Any]:
    """Hash of the weights if we have access to the artifact on disk.

    With a model served over HTTP there is no access to the weights:
    `available: false` is returned along with the reason, instead of inventing
    a hash.
    """
    if path is None:
        return {
            "available": False,
            "note": "Model served remotely: the weights are not reachable from OPAAS. "
            "Use --weights-path to hash the local artifact.",
        }
    if not path.exists():
        return {"available": False, "note": f"Weights path does not exist: {path}"}

    if path.is_file():
        files = [path]
    else:
        files = sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in _WEIGHT_SUFFIXES
        )
    if not files:
        return {"available": False, "note": f"No recognizable weight files in {path}"}

    # Hash of the set: a hash per file (with its relative path) and a hash of the index.
    base = path if path.is_dir() else path.parent
    per_file = [
        {
            "path": f.relative_to(base).as_posix(),
            "sha256": sha256_file(f),
            "bytes": f.stat().st_size,
        }
        for f in files
    ]
    return {
        "available": True,
        "hash": sha256_json(per_file),
        "fileCount": len(per_file),
        "totalBytes": sum(f["bytes"] for f in per_file),
        "files": per_file,
    }


#: Files next to the weights that change what the model is asked, not what it
#: knows: the chat template, the tokenizer, and the generation defaults vLLM
#: reads from `generation_config.json`. A new template changes block B and D as
#: much as new weights do (D25), so they are hashed alongside them (D35).
TOKENIZER_FILES = (
    "chat_template.jinja",
    "chat_template.json",
    "generation_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def tokenizer_files_hash(path: Path | None) -> dict[str, Any]:
    """Hash of the template, tokenizer and generation files beside the weights.

    Only for a model directory. A single GGUF file carries its chat template in
    its own metadata, so `weights_hash` already covers it.
    """
    if path is None or not path.is_dir():
        return {
            "available": False,
            "note": "Not a model directory: a GGUF file embeds its template, and without "
            "--weights-path there are no files to read.",
        }
    files = [path / name for name in TOKENIZER_FILES if (path / name).is_file()]
    if not files:
        return {"available": False, "note": f"No tokenizer or template files in {path}"}
    per_file = [{"path": f.name, "sha256": sha256_file(f)} for f in files]
    return {"available": True, "hash": sha256_json(per_file), "files": per_file}


def short_hash(full_hash: str, length: int = 8) -> str:
    """Short form used in the certificate's public identifier."""
    return full_hash.split(":", 1)[-1][:length]


def slugify(value: str) -> str:
    """Normalizes a model id so it can be used in paths and identifiers."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return slug or "unknown"


def allocate_run_id(runs_dir: Path, now: datetime | None = None) -> str:
    """Generates `YYYYMMDD-NNN`, with NNN running sequentially within the day."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    existing = {p.name for p in runs_dir.glob(f"{stamp}-*")} if runs_dir.exists() else set()
    for index in range(1, 1000):
        candidate = f"{stamp}-{index:03d}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"More than 999 runs for day {stamp}")
