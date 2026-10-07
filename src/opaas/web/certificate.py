"""Printable certificate: the credential as a document a person files (D29).

The signed credential is JSON and the certificate page lives on a site. Neither
is what an operator hands to an auditor, attaches to a tender or pins on a
wall. This module renders one self-contained HTML file laid out for A4
landscape, which the browser prints to PDF.

**The paper is not the certificate.** Anyone can edit HTML or a PDF. The page
therefore carries what lets a reader reach the real thing: a QR code to the
published certificate page, where the signature is verified in the browser,
and the identifiers that `opaas verify` checks offline. The CLI only renders it
for a credential that verifies against a trust anchor, so a certificate cannot
be printed for a signature that would fail.

**It reads like a diploma but does not grade like one.** A diploma has a
final mark; this one does not, because OPAAS has none (no aggregate score, no
tiers). Each block gets its own line, a block that was not evaluated says so
as visibly as one that was, and every limitation the run declared is printed,
even when that takes a second page.

Rendered self-contained (inline CSS, inline SVG, no fonts or scripts from
anywhere) so it prints the same offline and years later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import segno

from opaas.credential.vc import public_identifier
from opaas.hashing import short_hash
from opaas.web.site import Entry, SiteError, esc, quantization_html

#: The design blocks, in the order a reader expects them. Duplicated from
#: `suites.BLOCKS` on purpose: importing `suites` would drag the evaluation
#: machinery into a module that only formats a finished report.
BLOCK_NAMES = {
    "A": "Performance",
    "B": "Capability / Quality",
    "C": "Consistency / Robustness",
    "D": "Behavior and safety",
    "E": "Compliance readiness",
}

#: How many capability categories fit on the block's single line.
_MAX_CATEGORIES = 4


def check_verify_url(url: str) -> str:
    """The QR is the only route from paper to signature: it has to be a web address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise SiteError(
            f"'{url}' is not a web address. Pass the URL of the published certificate "
            "page (https://…/cert/<id>/index.html), the one 'opaas publish' generates."
        )
    return url


def render_printable(
    entry: Entry, *, verify_url: str | None = None, now: datetime | None = None
) -> str:
    """The certificate as a standalone HTML document ready to print."""
    report, credential = entry.report, entry.credential
    subject = credential.get("credentialSubject", {})
    configuration = subject.get("configuration", {})
    proof = credential.get("proof") or {}

    qr_html = ""
    if verify_url is not None:
        qr_html = f"""<figure class="qr">{_qr_svg(check_verify_url(verify_url))}
      <figcaption>Scan to verify the signature</figcaption></figure>"""
        verify_text = (
            f"Verify at <span class='mono'>{esc(verify_url)}</span>, or offline with:"
        )
    else:
        verify_text = "Verify offline with:"

    partial_html = ""
    if not entry.complete:
        missing = [
            f"{block} ({BLOCK_NAMES.get(block, block)})"
            for block in entry.eval_run.get("blocksNotEvaluated") or []
        ]
        partial_html = f"""<p class="partial"><strong>Partial evaluation.</strong>
    Not evaluated: {esc(", ".join(missing) or "unknown")}. This certificate claims
    nothing about those blocks.</p>"""

    issued = str(credential.get("issuanceDate", ""))[:10]
    expires = str(credential.get("expirationDate", ""))[:10]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OPAAS Certificate</title>
<style>{_CSS}</style>
</head>
<body>
<main class="sheet">
  <header>
    <p class="issuer">onpremIALabs · OPAAS — On-Prem AI Audit Suite</p>
    <h1>Certificate of Evaluation</h1>
    <p class="identifier mono">{esc(public_identifier(credential))}</p>
  </header>

  <p class="statement">This certifies that the deployment described below was evaluated
  with OPAAS methodology {esc(report.get("opaasVersion", "unknown"))} on {esc(issued)}.
  Results are reported per block. OPAAS issues no overall score or grade.</p>

  <section class="subject">
    <h2>{esc(entry.model)}</h2>
    <p class="facts">
      <span>Quantization {quantization_html(report)}</span>
      <span>Backend <b>{esc(configuration.get("backend") or "unknown")}</b></span>
      <span>Hardware <b>{esc(configuration.get("hardware") or "unknown")}</b></span>
      <span>Run <b>{esc(entry.eval_run.get("runId") or "unknown")}</b></span>
    </p>
  </section>

  {partial_html}

  <table class="blocks">
    <tbody>
{_render_block_rows(report, entry.eval_run)}
    </tbody>
  </table>

  {_notes_pointer(report)}

  <section class="validity">
    <p><strong>Valid from {esc(issued)} until {esc(expires)}.</strong> After that date the
    deployment requires re-certification: a new driver, engine release or quantization can
    change these results even for the same model.</p>
  </section>

  <footer>
    {qr_html}
    <div class="trace">
      <p>{verify_text}</p>
      <p class="mono command">opaas verify --credential credential.json --report report.json</p>
      <dl>
        <dt>Report SHA-256</dt><dd class="mono">{esc(entry.eval_run.get("reportHash"))}</dd>
        <dt>Deployment</dt><dd><span class="mono">{esc(configuration.get("deploymentHash")
          or "not recorded")}</span>{_fingerprint_html(report)}</dd>
        <dt>Credential</dt><dd class="mono">{esc(credential.get("id"))}</dd>
        <dt>Issuer key</dt><dd class="mono">{esc(proof.get("verificationMethod"))}</dd>
      </dl>
      <p class="note">This page is a rendering. The certificate is the signed credential:
      if anything here differs from it, the credential is right and this page is not.</p>
    </div>
  </footer>
</main>
{_render_limitations(report)}
</body>
</html>
"""


#: Short names for the fingerprint fields, for a line that has to fit.
_FINGERPRINT_LABELS = {
    "engine": "engine",
    "engineVersion": "engine version",
    "modelDigest": "model digest",
    "chatTemplateHash": "chat template",
    "weightsHash": "weights",
    "tokenizerFilesHash": "tokenizer files",
    "gpus": "GPU",
    "gpuDrivers": "GPU driver",
}


def _fingerprint_html(report: dict[str, Any]) -> str:
    """What the deployment hash stands for, and what it does not cover.

    A bare hash tells a reader nothing. The fields that went into it are named,
    digests shortened, and the fields the deployment did not expose are listed,
    because a matching hash proves nothing about them (D34).
    """
    fingerprint = report.get("deploymentFingerprint") or {}
    fields = fingerprint.get("fields") or {}
    if not fields:
        return ""
    parts = []
    if fields.get("engine"):
        parts.append(" ".join(str(v) for v in (fields["engine"], fields.get("engineVersion")) if v))
    for name in ("modelDigest", "chatTemplateHash", "weightsHash", "tokenizerFilesHash"):
        if fields.get(name):
            label = _FINGERPRINT_LABELS[name]
            if name == "weightsHash":
                label += " (seen loaded)"
            parts.append(f"{label} {short_hash(str(fields[name]), 12)}")
    if fields.get("gpus"):
        gpu = ", ".join(sorted(set(fields["gpus"])))
        drivers = ", ".join(fields.get("gpuDrivers") or [])
        parts.append(f"{gpu}" + (f", driver {drivers}" if drivers else ""))
    # Plain text throughout, escaped once here: every part can carry report data.
    line = f"<span class='fp'>{esc(' · '.join(parts))}</span>"
    unknown = sorted(
        {_FINGERPRINT_LABELS.get(name, name) for name in fingerprint.get("unknown") or {}}
    )
    if unknown:
        line += (
            f"<span class='fp missing'>Not exposed by the deployment, so not covered: "
            f"{esc(', '.join(unknown))}.</span>"
        )
    return line


def _render_block_rows(report: dict[str, Any], eval_run: dict[str, Any]) -> str:
    evaluated = set(eval_run.get("blocksEvaluated") or [])
    suites = {
        str(suite.get("block") or _SUITE_BLOCKS.get(str(suite.get("suite")))): suite
        for suite in report.get("suites", [])
        if suite.get("status") == "completed"
    }
    rows = []
    for block, name in BLOCK_NAMES.items():
        suite = suites.get(block)
        if block in evaluated and suite is not None:
            summary = _SUMMARIES.get(str(suite.get("suite")), _no_summary)(
                suite.get("metrics") or {}
            )
            state = "<span class='state ok'>evaluated</span>"
        else:
            summary = "Not evaluated. This certificate claims nothing about this block."
            state = "<span class='state missing'>not evaluated</span>"
        rows.append(
            f"""      <tr><th><span class="letter">{block}</span> {esc(name)}</th>
        <td>{state}</td><td>{summary}</td></tr>"""
        )
    return "\n".join(rows)


def _render_limitations(report: dict[str, Any]) -> str:
    """Every declared limitation, on its own sheet if it does not fit on the first."""
    limitations = report.get("limitations") or []
    if not limitations:
        body = "<p>The run recorded no limitations.</p>"
    else:
        body = "<ol>" + "".join(f"<li>{esc(item)}</li>" for item in limitations) + "</ol>"
    return f"""<section class="sheet limitations">
  <h2>What this certificate does not claim</h2>
  <p class="note">The limitations the run declared about itself, as recorded in the
  certified report.</p>
  {body}
  {_render_operator_notes(report)}
</section>"""


def _notes_pointer(report: dict[str, Any]) -> str:
    """One line on the first sheet, so the notes on the second are not missed."""
    count = len(report.get("operatorNotes") or [])
    if not count:
        return ""
    return f"""<p class="note">The operator added {count} note(s) about this run, not verified
  by OPAAS. They are on the following page.</p>"""


def _render_operator_notes(report: dict[str, Any]) -> str:
    """The operator's own context, under a heading that says nobody checked it (D31)."""
    notes = report.get("operatorNotes") or []
    if not notes:
        return ""
    items = "".join(f"<li>{esc(note)}</li>" for note in notes)
    return f"""<h2 class="declared">Declared by the operator</h2>
  <p class="note">Context the operator stated about this run. It is part of the signed
  report, so it has not been altered since, but <strong>OPAAS did not verify it</strong>.</p>
  <ul>{items}</ul>"""


# ------------------------------------------------------------------ block lines


def _pct(entry: dict[str, Any] | None) -> str | None:
    if not entry or entry.get("point") is None:
        return None
    return f"{entry['point']:.0%}"


def _num(value: Any, digits: int = 1) -> str:
    return f"{value:,.{digits}f}" if isinstance(value, (int, float)) else "—"


def _no_summary(_: dict[str, Any]) -> str:
    return "Evaluated. See the full report for the figures."


def _performance(metrics: dict[str, Any]) -> str:
    scenarios = metrics.get("scenarios") or []
    rated = [s for s in scenarios if s.get("throughput", {}).get("outputTokensPerS")]
    if not rated:
        return _no_summary(metrics)
    peak = max(rated, key=lambda s: s["throughput"]["outputTokensPerS"])
    parts = [
        f"Up to {_num(peak['throughput']['outputTokensPerS'])} output tok/s "
        f"(concurrency {esc(peak.get('concurrency'))}, "
        f"{esc(peak.get('promptWords'))}-word prompt)"
    ]
    serial = min(scenarios, key=lambda s: s.get("concurrency") or 0)
    ttft = serial.get("ttftS", {}).get("p50")
    if ttft is not None:
        parts.append(
            f"time to first token p50 {_num(ttft, 2)} s at concurrency "
            f"{esc(serial.get('concurrency'))}"
        )
    if observations := metrics.get("observations"):
        parts.append(f"{len(observations)} finding(s) recorded")
    return " · ".join(parts)


def _capability(metrics: dict[str, Any]) -> str:
    dataset = metrics.get("dataset", {})
    tasks = metrics.get("tasks") or []
    scored = [
        f"{esc(task.get('category'))} {pct}"
        for task in tasks
        if (pct := _pct(task.get("accuracy"))) is not None
    ]
    if not scored:
        return _no_summary(metrics)
    shown = ", ".join(scored[:_MAX_CATEGORIES])
    if len(scored) > _MAX_CATEGORIES:
        shown += f", and {len(scored) - _MAX_CATEGORIES} more categories"
    name = " ".join(str(dataset.get(k)) for k in ("name", "version") if dataset.get(k))
    return f"Accuracy on {esc(name or 'the dataset')}: {shown}"


def _consistency(metrics: dict[str, Any]) -> str:
    determinism = metrics.get("determinism", {})
    robustness = metrics.get("robustness", {})
    context = metrics.get("context", {})
    parts = []
    if (rate := _pct(determinism.get("identicalRate"))) is not None:
        parts.append(f"identical output on {rate} of repetitions")
    total = robustness.get("total", robustness.get("evaluated"))
    if total:
        parts.append(f"{esc(robustness.get('handled'))} of {esc(total)} adverse cases handled")
    if window := context.get("largestVerifiedWords"):
        text = f"context verified to {esc(window)} words"
        if context.get("contextLossDetected"):
            text += " (context loss detected)"
        parts.append(text)
    if not parts:
        return _no_summary(metrics)
    line = " · ".join(parts)
    return line[0].upper() + line[1:]


def _safety(metrics: dict[str, Any]) -> str:
    balance = metrics.get("safetyBalance", {})
    parts = []
    if (refusal := _pct(balance.get("correctRefusalRate"))) is not None:
        parts.append(f"Refuses {refusal} of harmful requests")
    if (over := _pct(balance.get("overRefusalRate"))) is not None:
        parts.append(f"over-refuses {over} of benign ones")
    unmeasured = len(metrics.get("notImplemented") or []) + sum(
        1
        for key in ("faithfulness", "toxicity")
        if (metrics.get(key) or {}) and not metrics[key].get("available")
    )
    if unmeasured:
        parts.append(f"{unmeasured} dimension(s) not measured")
    return " · ".join(parts) if parts else _no_summary(metrics)


def _compliance(metrics: dict[str, Any]) -> str:
    """Two counts side by side, never one: declared and probed are not added (D14)."""
    summary = metrics.get("summary", {})
    declared, probed = summary.get("declared", {}), summary.get("probed", {})
    probed_text = f"{esc(probed.get('pass', 0))} of {esc(probed.get('total', 0))} pass"
    if declared.get("unknown", 0) >= declared.get("total", 0):
        declared_text = "no declaration supplied"
    else:
        declared_text = f"{esc(declared.get('yes', 0))} of {esc(declared.get('total', 0))} met"
    return f"Verified by OPAAS: {probed_text} · Declared by the operator: {declared_text}"


#: Reports written by `run` always carry `block`; this covers one built by hand.
_SUITE_BLOCKS = {
    "performance": "A",
    "capability": "B",
    "consistency": "C",
    "safety": "D",
    "compliance": "E",
}

_SUMMARIES = {
    "performance": _performance,
    "capability": _capability,
    "consistency": _consistency,
    "safety": _safety,
    "compliance": _compliance,
}


def _qr_svg(url: str) -> str:
    # Level M survives a crease or a coffee ring without making the code dense.
    return segno.make(url, error="m").svg_inline(scale=3, border=2, dark="#111111")


# Paper is light: no dark-mode variant, because the page is meant to be printed
# and a dark sheet would print as a block of ink.
_CSS = """
@page { size: A4 landscape; margin: 12mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #e9e7e1; color: #1d1d1b;
  font: 10.5pt/1.45 Georgia, "Times New Roman", serif; }
.sheet { background: #fff; max-width: 273mm; margin: 16px auto; padding: 14mm 16mm;
  border: 3px double #1f4e3d; }
.mono { font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: 8.5pt; word-break: break-all; }
header { text-align: center; border-bottom: 1px solid #c9c4b6; padding-bottom: 6mm; }
.issuer { margin: 0; letter-spacing: .08em; text-transform: uppercase; font-size: 8.5pt;
  color: #1f4e3d; }
h1 { margin: 3mm 0 2mm; font-size: 26pt; font-weight: normal; letter-spacing: .02em; }
.identifier { margin: 0; color: #5c5a53; }
.statement { text-align: center; margin: 5mm auto; max-width: 200mm; }
.subject { text-align: center; }
.subject h2 { margin: 0 0 2mm; font-size: 17pt; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 1mm 4mm; margin: 0; }
.facts { display: flex; flex-wrap: wrap; justify-content: center; gap: 1mm 7mm;
  margin: 0; color: #5c5a53; }
.facts b { color: #1d1d1b; }
.partial { border-left: 4px solid #a3471b; background: #fbf1ea; padding: 2mm 4mm;
  margin: 4mm 0; }
.blocks { width: 100%; border-collapse: collapse; margin: 5mm 0; }
.blocks th, .blocks td { border-top: 1px solid #e2ded3; padding: 2mm 3mm;
  text-align: left; vertical-align: top; }
.blocks th { width: 58mm; font-weight: normal; white-space: nowrap; }
.blocks td:nth-child(2) { width: 30mm; }
.letter { display: inline-block; width: 6mm; font-weight: bold; color: #1f4e3d; }
.state { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .05em; }
.state.ok { color: #1f4e3d; } .state.missing { color: #a3471b; }
.validity { border-top: 1px solid #c9c4b6; padding-top: 3mm; }
footer { display: flex; gap: 6mm; align-items: flex-start; margin-top: 4mm; }
.qr { margin: 0; text-align: center; font-size: 8pt; color: #5c5a53; }
.qr svg { display: block; width: 32mm; height: 32mm; }
.trace { flex: 1; font-size: 9pt; } .trace p { margin: 0 0 1.5mm; }
.trace dt { color: #5c5a53; }
.command { background: #f4f2ec; padding: 1mm 2mm; display: inline-block; }
.note { color: #5c5a53; font-size: 8.5pt; }
.src { color: #5c5a53; font-size: 8.5pt; }
.conflict { color: #a3471b; }
.fp { display: block; font-size: 8.5pt; }
.fp.missing { color: #5c5a53; }
.limitations h2 { margin-top: 0; font-size: 14pt; font-weight: normal; }
.limitations li { margin-bottom: 1.5mm; }
.limitations h2.declared { margin-top: 8mm; border-top: 1px solid #c9c4b6; padding-top: 4mm; }
@media print {
  body { background: #fff; }
  .sheet { margin: 0; max-width: none; box-shadow: none; }
  .limitations { break-before: page; border: none; padding: 0; }
}
@media (max-width: 720px) {
  .sheet { margin: 0; padding: 16px; border-width: 0; }
  .blocks th { white-space: normal; width: auto; }
  footer { flex-direction: column; }
}
"""
