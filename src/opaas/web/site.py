"""Certificate site generation (Phase 5).

A **static** site: no server, no database, no API. It is generated and copied
wherever. That is consistent with the rest of the project — verification
cannot depend on the issuer's infrastructure — and it also makes the public
registry auditable with a `git diff`.

The certificate page verifies the signature **in the browser**, with the data
embedded in the HTML itself: it works without a network and without querying
onpremIALabs.
"""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opaas.credential.vc import public_identifier
from opaas.hashing import file_digest, slugify
from opaas.web.badge import badge_state, render_badge

ASSETS = Path(__file__).parent / "assets"


class SiteError(ValueError):
    """The certificate cannot be published."""


@dataclass(slots=True)
class Entry:
    report_path: Path
    credential_path: Path
    report: dict[str, Any] = field(default_factory=dict)
    credential: dict[str, Any] = field(default_factory=dict)
    report_bytes: bytes = b""

    @property
    def slug(self) -> str:
        return slugify(str(self.credential.get("id", "")).replace("urn:opaas:cert:", ""))

    @property
    def model(self) -> str:
        return str(self.report.get("model", {}).get("name", "unknown model"))

    @property
    def quantization(self) -> str | None:
        return self.report.get("model", {}).get("quantization")

    @property
    def eval_run(self) -> dict[str, Any]:
        return self.credential.get("credentialSubject", {}).get("evalRun", {})

    @property
    def complete(self) -> bool:
        return bool(self.eval_run.get("completeEvaluation"))


def load_entry(report_path: Path, credential_path: Path) -> Entry:
    """Loads report and credential, checking that they match each other."""
    for path in (report_path, credential_path):
        if not path.exists():
            raise SiteError(f"Does not exist: {path}")

    report_bytes = report_path.read_bytes()
    try:
        report = json.loads(report_bytes.decode("utf-8"))
        credential = json.loads(credential_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SiteError(f"Invalid JSON: {exc}") from None

    declared = credential.get("credentialSubject", {}).get("evalRun", {}).get("reportHash")
    actual = file_digest(report_path)
    if declared != actual:
        raise SiteError(
            f"Report {report_path.name} is not the one {credential_path.name} certifies: "
            f"{actual} != {declared}. Publishing a certificate next to a report that is not "
            "its own would be worse than not publishing it."
        )

    return Entry(report_path, credential_path, report, credential, report_bytes)


def build_site(
    entries: list[Entry],
    out_dir: Path,
    did_document: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Writes the public registry and one page per certificate."""
    if not entries:
        raise SiteError("There are no certificates to publish")

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    assets_dir = out_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    for asset in ("opaas.css", "verify.js"):
        shutil.copyfile(ASSETS / asset, assets_dir / asset)
        written.append(assets_dir / asset)

    well_known = out_dir / ".well-known"
    well_known.mkdir(exist_ok=True)
    did_path = well_known / "did.json"
    did_path.write_text(json.dumps(did_document, indent=2) + "\n", encoding="utf-8")
    written.append(did_path)

    for entry in entries:
        written.extend(_write_certificate(entry, out_dir, did_document, now))

    index = out_dir / "index.html"
    index.write_text(_render_index(entries, now), encoding="utf-8")
    written.append(index)
    return written


def _write_certificate(
    entry: Entry, out_dir: Path, did_document: dict[str, Any], now: datetime | None
) -> list[Path]:
    cert_dir = out_dir / "cert" / entry.slug
    cert_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "report.json": entry.report_bytes,
        "credential.json": json.dumps(entry.credential, indent=2, ensure_ascii=False).encode(),
        "badge.svg": render_badge(entry.credential, now).encode("utf-8"),
    }
    written = []
    for name, payload in files.items():
        (cert_dir / name).write_bytes(payload)
        written.append(cert_dir / name)

    page = cert_dir / "index.html"
    page.write_text(_render_certificate(entry, did_document, now), encoding="utf-8")
    written.append(page)
    return written


# ---------------------------------------------------------------------- templates


def esc(value: Any) -> str:
    """Escapes for HTML. Everything coming from the report goes through here."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _layout(title: str, body: str, depth: int = 0) -> str:
    root = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{root}assets/opaas.css">
</head>
<body>
{body}
<footer>
  <p>OPAAS — On-Prem AI Audit Suite ·
     <a href="{root}index.html">certificate registry</a></p>
  <p>Every certificate is verified in your browser against the public key published
     at <code>/.well-known/did.json</code>. There is no need to trust this server.</p>
</footer>
</body>
</html>
"""


def _render_index(entries: list[Entry], now: datetime | None) -> str:
    rows = "\n".join(
        f"""    <tr>
      <td><a href="cert/{esc(entry.slug)}/index.html">{esc(entry.model)}</a></td>
      <td>{esc(entry.quantization or "—")}</td>
      <td>{esc(entry.credential.get("credentialSubject", {}).get("configuration", {})
              .get("backend", "—"))}</td>
      <td><span class="state {"complete" if entry.complete else "partial"}">\
{esc(badge_state(entry.credential, now)[0])}</span></td>
      <td class="num">{esc(str(entry.credential.get("expirationDate", ""))[:10])}</td>
    </tr>"""
        for entry in entries
    )
    partial = sum(1 for entry in entries if not entry.complete)
    warning = (
        f"""<p class="warning">{partial} of {len(entries)} certificates correspond to
    <strong>partial</strong> evaluations: they do not cover the five blocks of the standard.
    Each entry says exactly what was measured and what was not.</p>"""
        if partial
        else ""
    )

    return _layout(
        "OPAAS certificate registry",
        f"""<h1>Certificate registry</h1>
<p class="subtitle">Models evaluated with OPAAS. Each entry includes the full
report, its hash and the signed credential.</p>
{warning}
<table>
  <thead><tr><th>Model</th><th>Quantization</th><th>Backend</th><th>Coverage</th>
  <th>Expires</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>""",
    )


def _render_certificate(
    entry: Entry, did_document: dict[str, Any], now: datetime | None
) -> str:
    report, credential = entry.report, entry.credential
    subject = credential.get("credentialSubject", {})
    configuration = subject.get("configuration", {})
    coverage = report.get("coverage", {})
    label, _ = badge_state(credential, now)

    # Embedded so verification works without a network. `<` is escaped because a
    # `</script>` inside a value would close the block early.
    credential_json = json.dumps(credential, ensure_ascii=False).replace("<", "\\u003c")
    did_json = json.dumps(did_document, ensure_ascii=False).replace("<", "\\u003c")
    report_b64 = base64.b64encode(entry.report_bytes).decode("ascii")

    missing = coverage.get("blocksMissing") or []
    coverage_html = (
        "<p><strong>Complete</strong> evaluation: all five blocks of the standard.</p>"
        if entry.complete
        else f"""<p class="warning"><strong>Partial evaluation.</strong>
      Blocks evaluated: {esc(", ".join(coverage.get("blocksEvaluated") or []) or "none")}.
      Not evaluated: {esc(", ".join(f"{b['block']} ({b['name']})" for b in missing))}.
      This certificate claims <strong>nothing</strong> about the unevaluated blocks.</p>"""
    )

    limitations = report.get("limitations") or []
    limitations_html = (
        "<ul class='limitations'>"
        + "".join(f"<li>{esc(limitation)}</li>" for limitation in limitations)
        + "</ul>"
        if limitations
        else "<p>The run recorded no limitations.</p>"
    )

    return _layout(
        f"OPAAS certificate — {entry.model}",
        f"""<h1>{esc(entry.model)}</h1>
<p class="subtitle">
  {esc(entry.quantization or "quantization not declared")} ·
  {esc(configuration.get("backend", "unknown backend"))} ·
  {esc(configuration.get("hardware", "unknown hardware"))}
</p>
<p><span class="state {"complete" if entry.complete else "partial"}">{esc(label)}</span></p>
<p class="identifier">{esc(public_identifier(credential))}</p>

<h2>Verification</h2>
<div class="panel">
  <p>Checks the Ed25519 signature and that the report on this page is exactly the
     certified one. It all runs in your browser.</p>
  <button id="verify" type="button">Verify signature</button>
  <div id="verification" hidden></div>
</div>

<h2>Coverage</h2>
{coverage_html}

{_render_blocks(report)}

<h2>Declared limitations</h2>
<p class="subtitle">What this run can <em>not</em> claim, according to its own report.</p>
{limitations_html}

<h2>Traceability</h2>
<table>
  <tbody>
    <tr><th>Identifier</th><td class="identifier">{esc(credential.get("id"))}</td></tr>
    <tr><th>Issuer</th><td>{esc(credential.get("issuer"))}</td></tr>
    <tr><th>Issued</th><td>{esc(credential.get("issuanceDate"))}</td></tr>
    <tr><th>Expires</th><td>{esc(credential.get("expirationDate"))}</td></tr>
    <tr><th>Report hash</th>
        <td class="identifier">{esc(entry.eval_run.get("reportHash"))}</td></tr>
    <tr><th>Configuration hash</th>
        <td class="identifier">{esc(configuration.get("configHash"))}</td></tr>
    <tr><th>Methodology</th><td>OPAAS {esc(report.get("opaasVersion"))}</td></tr>
  </tbody>
</table>

<h2>Downloads</h2>
<ul>
  <li><a href="report.json">report.json</a> — the full report. Its SHA-256 is the certified
      hash: <code>sha256sum report.json</code></li>
  <li><a href="credential.json">credential.json</a> — the signed credential</li>
  <li><a href="badge.svg">badge.svg</a> — embeddable badge</li>
</ul>

<script id="credential-data" type="application/json">{credential_json}</script>
<script id="did-data" type="application/json">{did_json}</script>
<script id="report-data" type="text/plain">{report_b64}</script>
<script src="../../assets/verify.js"></script>""",
        depth=2,
    )


def _render_blocks(report: dict[str, Any]) -> str:
    """Per-block results. No aggregate score: it does not exist and is not invented."""
    sections = []
    for suite in report.get("suites", []):
        renderer = _BLOCK_RENDERERS.get(str(suite.get("suite")))
        if suite.get("status") != "completed" or not renderer:
            continue
        body = renderer(suite.get("metrics", {}))
        if body:
            sections.append(f"<h2>{esc(_BLOCK_TITLES[suite['suite']])}</h2>\n{body}")
    return "\n".join(sections) or "<h2>Results</h2><p>The run produced no results.</p>"


def _rate(entry: dict[str, Any] | None) -> str:
    if not entry or entry.get("point") is None:
        return "—"
    interval = (
        f" <span class='note'>({entry['low']:.0%}–{entry['high']:.0%})</span>"
        if entry.get("low") is not None
        else ""
    )
    return f"{entry['point']:.1%}{interval}"


def _performance(metrics: dict[str, Any]) -> str:
    rows = "".join(
        f"""<tr><td class="num">{esc(s.get("promptWords"))}</td>
        <td class="num">{esc(s.get("concurrency"))}</td>
        <td class="num">{esc(s.get("throughput", {}).get("outputTokensPerS"))}</td>
        <td class="num">{esc(s.get("ttftS", {}).get("p50"))}</td>
        <td class="num">{esc(s.get("resources", {}).get("vramPeakMb"))}</td>
        <td class="num">{esc(s.get("energy", {}).get("whPerMillionTokens") or "—")}</td></tr>"""
        for s in metrics.get("scenarios", [])
    )
    findings = "".join(
        f"<li class='fail'><strong>{esc(o['kind'])}</strong>: {esc(o['detail'])}</li>"
        for o in metrics.get("observations", [])
    )
    return f"""<table>
  <thead><tr><th>Prompt (words)</th><th>Concurrency</th><th>Output tok/s</th>
  <th>TTFT p50 (s)</th><th>VRAM peak (MB)</th><th>Wh/1M tok</th></tr></thead>
  <tbody>{rows}</tbody>
</table>{f"<ul class='limitations'>{findings}</ul>" if findings else ""}"""


def _capability(metrics: dict[str, Any]) -> str:
    dataset = metrics.get("dataset", {})
    rows = "".join(
        f"""<tr><td>{esc(t.get("category"))}</td>
        <td class="num">{_rate(t.get("accuracy"))}</td>
        <td class="num">{esc(t.get("items"))}</td></tr>"""
        for t in metrics.get("tasks", [])
    )
    return f"""<p class="subtitle">Dataset {esc(dataset.get("name"))}
{esc(dataset.get("version"))} · <span class="identifier">\
{esc(dataset.get("datasetHash"))}</span> · style {esc(metrics.get("answerStyle"))}</p>
<table>
  <thead><tr><th>Category</th><th>Accuracy (95% CI)</th><th>Items</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def _consistency(metrics: dict[str, Any]) -> str:
    determinism = metrics.get("determinism", {})
    robustness = metrics.get("robustness", {})
    context = metrics.get("context", {})
    window = context.get("largestVerifiedWords")
    window_text = f"{window} words" if window else "inconclusive"
    if context.get("contextLossDetected"):
        window_text += " · context loss detected"

    return f"""<table>
  <tbody>
    <tr><th>Determinism (serial)</th><td>{_rate(determinism.get("identicalRate"))}
        — {esc(determinism.get("distinctOutputs"))} distinct outputs across
        {esc(determinism.get("repeats"))} repetitions</td></tr>
    <tr><th>Robustness</th><td>{esc(robustness.get("handled"))} of
        {esc(robustness.get("evaluated"))} adverse prompts handled;
        {esc(robustness.get("silentTruncations"))} silent truncations</td></tr>
    <tr><th>Verified window</th><td>{esc(window_text)}</td></tr>
  </tbody>
</table>"""


def _not_measured(reason: Any) -> str:
    """An unmeasured dimension is shown just as visibly as a measured one."""
    return f"<span class='note'>not measured: {esc(reason)}</span>"


def _safety(metrics: dict[str, Any]) -> str:
    balance = metrics.get("safetyBalance", {})
    rows_data = [
        ("Explicit refusal of harmful requests", _rate(balance.get("correctRefusalRate"))),
        ("…plus those that dissuade without refusing", _rate(balance.get("nonComplianceRate"))),
        ("Over-refusal of benign requests", _rate(balance.get("overRefusalRate"))),
    ]
    for measure, label in (
        (metrics.get("faithfulness", {}), "Faithfulness to the passage"),
        (metrics.get("toxicity", {}), "Toxicity above threshold"),
    ):
        if measure.get("available"):
            key = "faithfulRate" if "faithfulRate" in measure else "aboveThresholdRate"
            rows_data.append((f"{label} (classifier)", _rate(measure.get(key))))
        else:
            rows_data.append((label, _not_measured(measure.get("reason"))))

    for entry in metrics.get("notImplemented", []):
        rows_data.append((entry["dimension"].capitalize(), _not_measured(entry["reason"])))

    rows = "".join(f"<tr><th>{esc(name)}</th><td>{value}</td></tr>" for name, value in rows_data)
    return f"<table><tbody>{rows}</tbody></table>"


def _compliance(metrics: dict[str, Any]) -> str:
    def _origin(item: dict[str, Any]) -> str:
        return (
            "verified by OPAAS"
            if item.get("verifiedByOpaasBench")
            else "declared by the operator"
        )

    rows = "".join(
        f"""<tr><td>{esc(i.get("title"))}</td>
        <td>{_origin(i)}</td>
        <td>{esc(i.get("status"))}</td>
        <td class="note">{esc(", ".join(i.get("frameworks", [])))}</td></tr>"""
        for i in metrics.get("items", [])
    )
    return f"""<p class="subtitle">What the operator declares and what OPAAS
verifies are listed separately and are not added together.</p>
<table>
  <thead><tr><th>Requirement</th><th>Origin</th><th>Status</th><th>Framework</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""


_BLOCK_RENDERERS = {
    "performance": _performance,
    "capability": _capability,
    "consistency": _consistency,
    "safety": _safety,
    "compliance": _compliance,
}
_BLOCK_TITLES = {
    "performance": "Block A — Performance",
    "capability": "Block B — Capability",
    "consistency": "Block C — Consistency",
    "safety": "Block D — Behavior and safety",
    "compliance": "Block E — Compliance",
}


def now_utc() -> datetime:
    return datetime.now(UTC)
