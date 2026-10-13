"""`card`: the Auditability Card and the deployment manifest (D52).

- `auditability`: Q1–Q6 filled from `discover`, `smith`, `sentinel`,
  `inquest` and `swarm` evidence, each answer citing its file and hash;
- `manifest`: the signed list of every report behind a multi-model,
  multi-agent deployment, with whether they share one environment;
- `render`: a printable page for the card.
"""

from opaas.card.auditability import QUESTIONS, build_card
from opaas.card.manifest import ManifestError, build_manifest
from opaas.card.render import render_card

__all__ = ["QUESTIONS", "ManifestError", "build_card", "build_manifest", "render_card"]
