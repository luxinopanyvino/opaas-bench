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
from datetime import datetime
from pathlib import Path
from typing import Any

from opaas.core.credential.vc import public_identifier
from opaas.core.hashing import file_digest, slugify
from opaas.core.report import describe_scored_dataset
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
    def is_system(self) -> bool:
        """A system certificate (D60): the subject is a system, not one model."""
        return "OpaasSystemCertificate" in (self.credential.get("type") or [])

    @property
    def slug(self) -> str:
        identifier = str(self.credential.get("id", ""))
        for prefix in ("urn:opaas:cert:", "urn:opaas:system:"):
            identifier = identifier.replace(prefix, "")
        return slugify(identifier)

    @property
    def model(self) -> str:
        """What the certificate is about: the model, or the system."""
        if self.is_system:
            return str(self.system.get("id", "unknown system"))
        return str(self.report.get("model", {}).get("name", "unknown model"))

    @property
    def system(self) -> dict[str, Any]:
        return self.credential.get("credentialSubject", {}).get("system", {})

    @property
    def report_hash(self) -> str | None:
        return self.eval_run.get("reportHash") or self.system.get("reportHash")

    @property
    def quantization(self) -> str | None:
        return self.report.get("model", {}).get("quantization")

    @property
    def eval_run(self) -> dict[str, Any]:
        return self.credential.get("credentialSubject", {}).get("evalRun", {})

    @property
    def complete(self) -> bool:
        """All five blocks for a model; certifiable for a system."""
        if self.is_system:
            return bool(self.credential.get("credentialSubject", {}).get("certifiable"))
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

    subject = credential.get("credentialSubject", {})
    declared = (subject.get("evalRun") or {}).get("reportHash") or (
        subject.get("system") or {}).get("reportHash")
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
    revocations: dict[str, Any] | None = None,
) -> list[Path]:
    """Writes the public registry and one page per certificate.

    With `revocations`, the issuer's signed revocation list is published at
    `/.well-known/opaas-revocations.json` and embedded in every page, so the
    browser checks it as `opaas verify` does (D65). The browser still believes
    it only once it verifies under the DID document.
    """
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
    if revocations is not None:
        revocations_path = well_known / "opaas-revocations.json"
        revocations_path.write_text(json.dumps(revocations, indent=2) + "\n", encoding="utf-8")
        written.append(revocations_path)

    for entry in entries:
        written.extend(_write_certificate(entry, out_dir, did_document, now, revocations))

    index = out_dir / "index.html"
    index.write_text(_render_index(entries, now), encoding="utf-8")
    written.append(index)
    return written


def _write_certificate(
    entry: Entry,
    out_dir: Path,
    did_document: dict[str, Any],
    now: datetime | None,
    revocations: dict[str, Any] | None = None,
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
    render = _render_system_certificate if entry.is_system else _render_certificate
    page.write_text(
        _with_revocations(render(entry, did_document, now), revocations), encoding="utf-8"
    )
    written.append(page)
    return written


def _with_revocations(page: str, revocations: dict[str, Any] | None) -> str:
    """Embeds the revocation list next to the DID document, where verify.js looks for it."""
    if revocations is None:
        return page
    data = json.dumps([revocations], ensure_ascii=False).replace("<", "\\u003c")
    anchor = '<script src="../../assets/verify.js"></script>'
    return page.replace(
        anchor,
        f'<script id="revocations-data" type="application/json">{data}</script>\n{anchor}',
        1,
    )


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


def _subject_kind(entry: Entry) -> str:
    if entry.is_system:
        components = entry.credential.get("credentialSubject", {}).get("components") or []
        return f"system · {len(components)} components"
    return str(entry.credential.get("credentialSubject", {}).get("configuration", {})
               .get("backend", "—"))


def _render_index(entries: list[Entry], now: datetime | None) -> str:
    rows = "\n".join(
        f"""    <tr>
      <td><a href="cert/{esc(entry.slug)}/index.html">{esc(entry.model)}</a></td>
      <td>{esc(entry.quantization or "—")}</td>
      <td>{esc(_subject_kind(entry))}</td>
      <td><span class="state {"complete" if entry.complete else "partial"}">\
{esc(badge_state(entry.credential, now)[0])}</span></td>
      <td class="num">{esc(str(entry.credential.get("expirationDate", ""))[:10])}</td>
    </tr>"""
        for entry in entries
    )
    partial = sum(1 for entry in entries if not entry.complete)
    warning = (
        f"""<p class="warning">{partial} of {len(entries)} certificates are
    <strong>partial</strong> model evaluations or systems that are <strong>not
    certifiable</strong>. Each entry says exactly what was measured and what was not.</p>"""
        if partial
        else ""
    )

    return _layout(
        "OPAAS certificate registry",
        f"""<h1>Certificate registry</h1>
<p class="subtitle">Models and systems audited with OPAAS. Each entry includes the full
report, its hash and the signed credential.</p>
{warning}
<table>
  <thead><tr><th>Model or system</th><th>Quantization</th><th>Backend</th><th>Coverage</th>
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
  quantization {quantization_html(report)} ·
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

{_render_operator_notes(report)}

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
    <tr><th>Deployment fingerprint</th>
        <td>{_render_fingerprint(report, configuration)}</td></tr>
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


# ------------------------------------------------------------- systems (D60)

#: Dimensions in the order a reader expects them, with the column heading.
DIMENSION_COLUMNS = (
    ("performance", "Perf."), ("capability", "Capab."), ("consistency", "Consist."),
    ("safety", "Safety"), ("compliance", "Compl."), ("actionRecoverability", "Q1"),
    ("lifecycleCoverage", "Q2"), ("policyCheckability", "Q3"),
    ("responsibilityAttribution", "Q4"), ("evidenceIntegrity", "Q5"), ("degradation", "Q6"),
)
_MARKS = {"measured": ("●", "measured"), "not-measured": ("○", "not measured"),
          "not-applicable": ("·", "not applicable")}


def coverage_table_html(coverage: dict[str, dict[str, str]]) -> str:
    """Components × dimensions, one mark per cell: whether measured, never how well."""
    head = "".join(f"<th>{esc(label)}</th>" for _, label in DIMENSION_COLUMNS)
    rows = []
    for component, cells in coverage.items():
        marks = []
        for dimension, _ in DIMENSION_COLUMNS:
            mark, title = _MARKS.get(cells.get(dimension, ""), ("", ""))
            marks.append(f'<td class="mark {esc(cells.get(dimension, ""))}" '
                         f'title="{esc(title)}">{mark}</td>')
        rows.append(f"<tr><th>{esc(component)}</th>{''.join(marks)}</tr>")
    return (f"<table class='coverage'><thead><tr><th>Component</th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            "<p class='legend'>● measured · ○ not measured · · not applicable. Q1–Q6: the "
            "Auditability Card's questions. A mark says whether a cell was measured, never "
            "how well: there is no score.</p>")


def format_value(value: dict[str, Any]) -> str:
    kind = value.get("type")
    if kind == "rate":
        return f"{value['point']:.0%} [{value['low']:.0%}–{value['high']:.0%}, n={value['n']}]"
    if kind == "number":
        return f"{value['value']} {value['unit']}"
    if kind == "distribution":
        centre = value.get("p50", value.get("point"))
        return f"p50 {centre} {value['unit']}"
    return str(value.get("verdict", ""))


def cell_summaries(
    measurements: list[dict[str, Any]], limit: int = 3
) -> list[tuple[str, str, str]]:
    """(component, dimension, text): a few measured figures per cell, then how many more."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in measurements:
        if record.get("status") == "measured":
            grouped.setdefault((record["component"], record["dimension"]), []).append(record)
    order = {d: i for i, (d, _) in enumerate(DIMENSION_COLUMNS)}
    rows = []
    for (component, dimension), records in sorted(
        grouped.items(), key=lambda item: (item[0][0], order.get(item[0][1], 99))
    ):
        shown = [f"{r['aspect']}: {format_value(r['value'])}" for r in records[:limit]]
        if len(records) > limit:
            shown.append(f"and {len(records) - limit} more")
        rows.append((component, dimension, " · ".join(shown)))
    return rows


def threshold_verdicts(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in measurements if "verdict" in r]


def _render_system_certificate(
    entry: Entry, did_document: dict[str, Any], now: datetime | None
) -> str:
    report, credential = entry.report, entry.credential
    subject = credential.get("credentialSubject", {})
    label, _ = badge_state(credential, now)
    credential_json = json.dumps(credential, ensure_ascii=False).replace("<", "\\u003c")
    did_json = json.dumps(did_document, ensure_ascii=False).replace("<", "\\u003c")
    report_b64 = base64.b64encode(entry.report_bytes).decode("ascii")

    reasons = subject.get("notCertifiableBecause") or []
    certifiable_html = (
        "<p><strong>Certifiable</strong>: every member was measured in one environment, "
        "every agent ran this deployment's own scenarios, and no step failed.</p>"
        if entry.complete
        else "<div class='warning'><p><strong>Not certifiable.</strong> The audit is a true "
        "account of what was measured, and this is why it cannot certify the system:</p><ul>"
        + "".join(f"<li>{esc(r)}</li>" for r in reasons) + "</ul></div>"
    )
    composition = report.get("composition") or {}
    observed = {True: "observed", False: "NOT found", None: "not observable"}
    components = "".join(
        f"<tr><td>{esc(c.get('id'))}</td><td>{esc(c.get('kind'))}</td>"
        f"<td>{esc(observed.get(c.get('observed')))} — {esc(c.get('basis', ''))}</td></tr>"
        for c in composition.get("components") or []
    )
    findings = "".join(f"<li>{esc(f)}</li>" for f in composition.get("findings") or [])
    results = "".join(
        f"<tr><td>{esc(c)}</td><td>{esc(d)}</td><td>{esc(t)}</td></tr>"
        for c, d, t in cell_summaries(report.get("measurements") or [])
    )
    verdicts = "".join(
        f"<tr><td>{esc(r['component'])}</td><td>{esc(r['aspect'])}</td>"
        f"<td>{esc(r['threshold'].get('min', r['threshold'].get('max', '')))} "
        f"({'min' if 'min' in r['threshold'] else 'max'}, by {esc(r['threshold']['scope'])})"
        f"</td><td>{esc(r['verdict']['verdict'])}</td></tr>"
        for r in threshold_verdicts(report.get("measurements") or [])
    )
    limitations = "".join(
        f"<li><strong>{esc(item.get('component'))}</strong>: {esc(item.get('text'))}</li>"
        for item in report.get("limitations") or []
    )
    verdicts_html = (
        "<h2>Operator thresholds</h2><table><thead><tr><th>Component</th><th>Measurement</th>"
        f"<th>Threshold</th><th>Verdict</th></tr></thead><tbody>{verdicts}</tbody></table>"
        if verdicts else ""
    )
    limitations_html = (f"<ul class='limitations'>{limitations}</ul>" if limitations
                        else "<p>The members recorded no limitations.</p>")
    findings_html = (f"<h3>Composition findings</h3><ul class='limitations'>{findings}</ul>"
                     if findings else "")
    members = "".join(
        f"<tr><td>{esc(m.get('component'))}</td><td>{esc(m.get('kind'))}</td>"
        f"<td class='identifier'>{esc(m.get('hash'))}</td></tr>"
        for m in report.get("members") or []
    )
    return _layout(
        f"OPAAS system certificate — {entry.model}",
        f"""<h1>{esc(entry.model)}</h1>
<p class="subtitle">{esc(entry.system.get("purpose", ""))}</p>
<p><span class="state {"complete" if entry.complete else "partial"}">{esc(label)}</span></p>
<p class="identifier">{esc(public_identifier(credential))}</p>

<h2>Verification</h2>
<div class="panel">
  <p>Checks the Ed25519 signature and that the system report on this page is exactly the
     certified one. It all runs in your browser.</p>
  <button id="verify" type="button">Verify signature</button>
  <div id="verification" hidden></div>
</div>

<h2>Certifiability</h2>
{certifiable_html}

<h2>Components</h2>
<table><thead><tr><th>Component</th><th>Kind</th><th>Declared, and observed?</th></tr></thead>
<tbody>{components}</tbody></table>
{findings_html}

<h2>Coverage</h2>
{coverage_table_html(subject.get("coverage") or {})}

<h2>Results</h2>
<p class="subtitle">A few measured figures per component and dimension, each read from a
member document whose hash is listed below. Nothing is combined across cells.</p>
<table><thead><tr><th>Component</th><th>Dimension</th><th>Measured</th></tr></thead>
<tbody>{results}</tbody></table>

{verdicts_html}

<h2>Declared limitations</h2>
{limitations_html}

<h2>Traceability</h2>
<table><tbody>
  <tr><th>Identifier</th><td class="identifier">{esc(credential.get("id"))}</td></tr>
  <tr><th>Issuer</th><td>{esc(credential.get("issuer"))}</td></tr>
  <tr><th>Issued</th><td>{esc(credential.get("issuanceDate"))}</td></tr>
  <tr><th>Expires</th><td>{esc(credential.get("expirationDate"))}</td></tr>
  <tr><th>System report hash</th><td class="identifier">{esc(entry.report_hash)}</td></tr>
  <tr><th>Manifest hash</th><td class="identifier">{esc(entry.system.get("manifestHash"))}</td>
  </tr>
</tbody></table>
<h3>Members</h3>
<table><thead><tr><th>Component</th><th>Document</th><th>SHA-256</th></tr></thead>
<tbody>{members}</tbody></table>

<h2>Downloads</h2>
<ul>
  <li><a href="report.json">report.json</a> — the system report. Its SHA-256 is the
      certified hash: <code>sha256sum report.json</code></li>
  <li><a href="credential.json">credential.json</a> — the signed credential</li>
  <li><a href="badge.svg">badge.svg</a> — embeddable badge</li>
</ul>

<script id="credential-data" type="application/json">{credential_json}</script>
<script id="did-data" type="application/json">{did_json}</script>
<script id="report-data" type="text/plain">{report_b64}</script>
<script src="../../assets/verify.js"></script>""",
        depth=2,
    )


def quantization_html(report: dict[str, Any]) -> str:
    """The value, and whose word it is: the engine's or the operator's (D37)."""
    model = report.get("model", {})
    shown, source = model.get("quantization"), model.get("quantizationSource")
    reported = model.get("quantizationReported")
    if not shown:
        return "<b>not declared</b>"
    value = f"<b>{esc(shown)}</b>"
    if source == "engine":
        return f"{value} <span class='src'>(reported by the engine)</span>"
    if source == "declared" and reported:
        a, b = str(shown).upper(), str(reported).upper()
        if a in b or b in a:
            return (
                f"{value} <span class='src'>(declared; the engine reports "
                f"{esc(reported)})</span>"
            )
        return (
            f"{value} <span class='src'>(declared)</span> · "
            f"<span class='conflict'>the engine reports <b>{esc(reported)}</b></span>"
        )
    if source == "declared":
        return (
            f"{value} <span class='src'>(declared by the operator, not reported by "
            "the engine)</span>"
        )
    return f"{value} <span class='src'>(source not recorded)</span>"


def _render_fingerprint(report: dict[str, Any], configuration: dict[str, Any]) -> str:
    """The hash, then every field the deployment did not expose: a match proves nothing there."""
    fingerprint = report.get("deploymentFingerprint") or {}
    digest = configuration.get("deploymentHash") or fingerprint.get("hash")
    if not digest:
        return "<span class='note'>not recorded: the report predates the fingerprint</span>"
    unknown = fingerprint.get("unknown") or {}
    if not unknown:
        return f"<span class='identifier'>{esc(digest)}</span>"
    missing = "".join(
        f"<li>{esc(name)}: {esc(reason)}</li>" for name, reason in sorted(unknown.items())
    )
    return f"""<span class='identifier'>{esc(digest)}</span>
        <p class='note'>Not exposed by the deployment, so not part of what this hash
        proves:</p><ul class='limitations'>{missing}</ul>"""


def _render_operator_notes(report: dict[str, Any]) -> str:
    """Operator context, headed so it cannot be read as something OPAAS measured (D31)."""
    notes = report.get("operatorNotes") or []
    if not notes:
        return ""
    items = "".join(f"<li>{esc(note)}</li>" for note in notes)
    return f"""<h2>Declared by the operator</h2>
<p class="subtitle">Context the operator stated about this run. Signed with the report,
so unaltered since, but <strong>not verified by OPAAS</strong>.</p>
<ul>{items}</ul>"""


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
    return f"""<p class="subtitle">Dataset {esc(describe_scored_dataset(metrics))}
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
    {_under_load_row(determinism.get("underLoad") or {})}
    <tr><th>Robustness</th><td>{esc(robustness.get("handled"))} of
        {esc(robustness.get("evaluated"))} adverse prompts handled;
        {esc(robustness.get("silentTruncations"))} silent truncations</td></tr>
    <tr><th>Verified window</th><td>{esc(window_text)}</td></tr>
  </tbody>
</table>"""


def _under_load_row(under_load: dict[str, Any]) -> str:
    """Absent from older reports, and then absent here too: not 'not measured'."""
    if not under_load:
        return ""
    if not under_load.get("measured"):
        value = _not_measured(under_load.get("reason"))
    else:
        value = (
            f"{_rate(under_load.get('identicalToSerialRate'))} — "
            f"{esc(under_load.get('matches'))} of {esc(under_load.get('outputs'))} rounds "
            f"matched the serial output at {esc(under_load.get('concurrency'))} "
            "simultaneous requests"
        )
    return f"<tr><th>Determinism (under load)</th><td>{value}</td></tr>"


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
    for task in metrics.get("tasks", []):
        subsets = task.get("subsets") or {}
        if task.get("type") != "grounded_qa" or not subsets:
            continue
        rows_data += [
            ("Answers from the passage (answerable questions)",
             _rate(subsets.get("answerable", {}).get("extractionRate"))),
            ("Hallucinates (questions the passage cannot answer)",
             _rate(subsets.get("unanswerable", {}).get("hallucinationRate"))),
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

    adversarial = metrics.get("adversarialToxicity") or {}
    if adversarial.get("available"):
        rows_data.append(
            (
                "Toxicity on provoking openings (RealToxicityPrompts, classifier)",
                _rate(adversarial.get("aboveThresholdRate")),
            )
        )
    for entry in metrics.get("notImplemented", []):
        rows_data.append((entry["dimension"].capitalize(), _not_measured(entry["reason"])))

    rows = "".join(f"<tr><th>{esc(name)}</th><td>{value}</td></tr>" for name, value in rows_data)
    return f"<table><tbody>{rows}</tbody></table>{_bias(metrics)}"


def _bias(metrics: dict[str, Any]) -> str:
    """BBQ per category: the three figures and the verdict, never pooled (D55)."""
    for task in metrics.get("tasks", []):
        categories = (task.get("subsets") or {}).get("categories")
        if task.get("type") != "bias_mc" or not categories:
            continue
        rows = "".join(
            f"""<tr><td>{esc(name.replace("_", " "))}</td>
        <td class="num">{_rate(figures.get("ambiguousUnknownRate"))}</td>
        <td class="num">{_rate(figures.get("disambiguatedAccuracy"))}</td>
        <td class="num">{_rate(figures.get("stereotypeAlignedRate"))}</td>
        <td>{esc(figures.get("verdict"))}</td></tr>"""
            for name, figures in categories.items()
        )
        return f"""<h3>Bias (BBQ)</h3>
<p class="subtitle">When the context does not say who, the correct answer is "cannot be
determined". A category is flagged only when stereotype-aligned picks are above 50% with the
whole 95% interval.</p>
<table>
  <thead><tr><th>Category</th><th>Unknown when ambiguous</th>
  <th>Accuracy when disambiguated</th><th>Stereotype-aligned</th><th>Verdict</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""
    return ""


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
