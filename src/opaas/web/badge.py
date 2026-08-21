"""Embeddable SVG badge.

The badge is the only thing most people will ever see, so it **cannot lie by
omission**. A green "OPAS certified" over an evaluation that only measured
speed would be exactly the misleading claim this project exists to prevent: the
badge says which blocks were evaluated, and changes color when the evaluation
is partial or the credential has expired.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

_GREEN = "#12683f"
_AMBER = "#8a5a00"
_RED = "#b21f2d"
_GREY = "#4a4f58"

#: Approximate width per character at 11px in the badge's typeface.
_CHAR = 6.4


def badge_state(credential: dict[str, Any], now: datetime | None = None) -> tuple[str, str]:
    """Returns (label, color) according to what the credential can claim."""
    eval_run = credential.get("credentialSubject", {}).get("evalRun", {})
    expiration = credential.get("expirationDate")

    if _expired(expiration, now):
        return "expired", _RED
    if eval_run.get("completeEvaluation"):
        return "A-E evaluated", _GREEN

    blocks = eval_run.get("blocksEvaluated") or []
    return (f"partial: {', '.join(blocks)}" if blocks else "no blocks"), _AMBER


def _expired(expiration: Any, now: datetime | None) -> bool:
    if not isinstance(expiration, str):
        return False
    try:
        parsed = datetime.fromisoformat(expiration)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) >= parsed


def render_badge(credential: dict[str, Any], now: datetime | None = None) -> str:
    """Self-contained SVG, with no external fonts or resources."""
    label, colour = badge_state(credential, now)
    left, right = "OPAAS", label

    left_width = round(len(left) * _CHAR + 14)
    right_width = round(len(right) * _CHAR + 14)
    total = left_width + right_width

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" \
role="img" aria-label="{escape(left)}: {escape(right)}">
  <title>{escape(left)}: {escape(right)}</title>
  <rect width="{left_width}" height="20" rx="3" fill="{_GREY}"/>
  <rect x="{left_width}" width="{right_width}" height="20" rx="3" fill="{colour}"/>
  <rect x="{left_width - 3}" width="6" height="20" fill="{colour}"/>
  <g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
    <text x="{left_width / 2}" y="14" text-anchor="middle">{escape(left)}</text>
    <text x="{left_width + right_width / 2}" y="14" text-anchor="middle">{escape(right)}</text>
  </g>
</svg>
"""
