"""vLLM adapter — the first one of Phase 1.

vLLM speaks the OpenAI protocol, so it only adds the `/version` probe and its
own metadata, which ends up in the credential's `configuration.backend`.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from opaas.adapters.openai_compatible import OpenAICompatibleBackend


class VllmBackend(OpenAICompatibleBackend):
    name: ClassVar[str] = "vllm"
    engine_label: ClassVar[str] = "vLLM"
    default_base_url: ClassVar[str] = "http://localhost:8000/v1"

    def _engine_version(self) -> str | None:
        """`/version` hangs off the server root, not off `/v1`."""
        try:
            root = str(httpx.URL(self._base_url).copy_with(raw_path=b"/version"))
            response = httpx.get(root, timeout=5.0)
            response.raise_for_status()
            return str(response.json().get("version"))
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    def get_backend_metadata(self) -> dict[str, Any]:
        metadata = super().get_backend_metadata()
        version = metadata.get("engineVersion")
        metadata["backend"] = f"vLLM {version}" if version else "vLLM (unknown version)"
        return metadata
