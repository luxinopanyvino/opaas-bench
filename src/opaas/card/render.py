"""A printable rendering of the Auditability Card.

A rendering, like the printable certificate (D29): the card JSON and its
signature are the evidence; this page only shows them. Every value printed
comes from the card, and every answer prints the file and hash it rests on.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

_STYLE = """
:root { --ink:#1d1d1b; --muted:#5c5a53; --line:#c9c4b6; --accent:#1f4e3d; --warn:#a3471b;
  --paper:#fff; --bg:#e9e7e1; }
@media (prefers-color-scheme: dark) { :root { --ink:#e8e6e0; --muted:#a9a69c; --line:#4a473f;
  --accent:#7fc4a8; --warn:#e39a6f; --paper:#1f1e1b; --bg:#141311; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 Georgia, "Times New Roman", serif; }
main { background:var(--paper); max-width:960px; margin:16px auto; padding:24px 16px;
  border:3px double var(--accent); }
h1 { font-weight:normal; margin:0 0 4px; }
h2 { font-size:18px; margin:24px 0 4px; color:var(--accent); }
.q { color:var(--muted); margin:0 0 8px; }
.mono { font-family:Consolas, "Courier New", monospace; font-size:12px; word-break:break-all; }
.answer { border-left:3px solid var(--line); padding:4px 10px; margin:6px 0; }
.gap { border-left:3px solid var(--warn); padding:4px 10px; margin:6px 0; }
.none { color:var(--warn); }
pre { white-space:pre-wrap; margin:4px 0; font-size:12px; }
@media print { body { background:#fff; } main { border:none; margin:0; } }
"""


def render_card(card: dict[str, Any], *, title: str = "Auditability Card") -> str:
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body><main>",
        f"<h1>{escape(title)}</h1>",
        f"<p class=\"q\">{escape(card.get('note', ''))}</p>",
    ]
    for qid, entry in card["questions"].items():
        parts.append(f"<h2>{qid} · {escape(entry['dimension'])}</h2>")
        parts.append(f"<p class=\"q\">{escape(entry['text'])}</p>")
        for answer in entry["answers"]:
            values = {k: v for k, v in answer.items()
                      if k not in ("kind", "file", "hash", "comparedWith")}
            parts.append(
                "<div class=\"answer\">"
                f"<div class=\"mono\">{escape(answer['kind'])} · {escape(answer['file'])} · "
                f"{escape(answer['hash'])}</div>"
                f"<pre>{escape(json.dumps(values, indent=1, ensure_ascii=False))}</pre></div>"
            )
        for gap in entry["gaps"]:
            parts.append(
                f"<div class=\"gap\"><b>{escape(gap['status'])}</b> {escape(gap['rule'])} "
                f"{escape(gap['subject'])}: {escape(gap['detail'])}</div>"
            )
        if entry.get("notAnswered"):
            parts.append(f"<p class=\"none\">Not answered. {escape(entry['notAnswered'])}</p>")
    retention = card.get("retention", {})
    parts.append("<h2>Retention (declared)</h2>")
    parts.append(f"<pre>{escape(json.dumps(retention, indent=1, ensure_ascii=False))}</pre>")
    parts.append("<h2>Evidence</h2>")
    for item in card.get("evidence", []):
        parts.append(f"<div class=\"mono\">{escape(item['kind'])} · {escape(item['file'])} · "
                     f"{escape(item['hash'])}</div>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


__all__ = ["render_card"]
