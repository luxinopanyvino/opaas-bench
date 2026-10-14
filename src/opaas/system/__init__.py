"""The system audit (D60): the system as the audited subject.

Phase S0 delivers the formats every later phase is built on: the system
manifest (`manifest.py`), the measurement record with its three statuses
(`measurement.py`), and one extractor per existing document kind
(`extract.py`). It imports from the modules it reads, never the reverse.
"""

from __future__ import annotations

from opaas.system.extract import (
    EXTRACTORS,
    Extraction,
    ExtractionError,
    document_kind,
    extract,
)
from opaas.system.manifest import (
    COMPONENT_FIELDS,
    PENDING_ADAPTERS,
    Component,
    SystemManifest,
    SystemManifestError,
    load_manifest,
)
from opaas.system.measurement import (
    AUDITABILITY,
    AXES,
    BEHAVIOUR,
    STATUSES,
    Measurement,
    MeasurementError,
    check,
)

__all__ = [
    "AUDITABILITY",
    "AXES",
    "BEHAVIOUR",
    "COMPONENT_FIELDS",
    "EXTRACTORS",
    "PENDING_ADAPTERS",
    "STATUSES",
    "Component",
    "Extraction",
    "ExtractionError",
    "Measurement",
    "MeasurementError",
    "SystemManifest",
    "SystemManifestError",
    "check",
    "document_kind",
    "extract",
    "load_manifest",
]
