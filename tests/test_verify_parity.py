"""`opaas verify` and the published site's `verify.js` agree on the same credentials.

D65 put the root → issuing key → credential walk in two verifiers. If they
drifted, the site would accept less than the CLI does, or more. Every fixture
below is verified by both, and the full list of checks — names, order and
outcome — must be identical.

The JavaScript side runs under Node (`tests/js/verify_parity.cjs`), which has
the same WebCrypto a browser has. Where `node` is not on the PATH the test is
skipped, not passed, and in CI (`$CI` set) it fails instead; the runners
have Node. To check by hand:
`uv run pytest tests/test_verify_parity.py -rs` and look for the skip reason.
"""

import base64
import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from opaas.core.credential import (
    DELEGATION_FIELD,
    TrustRoot,
    build_credential,
    sign_credential,
    verify_credential,
)
from opaas.core.credential.proof import iso, sign_document
from opaas.core.hashing import file_digest
from tests.test_delegation import (
    ISSUING_ID,
    NOW,
    REPORT,
    ROOT_DID,
    Chain,
    FakeSigner,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFY_JS = ROOT / "src" / "opaas" / "web" / "assets" / "verify.js"
HARNESS = Path(__file__).parent / "js" / "verify_parity.cjs"


def _fixtures(tmp_path):
    """(name, credential, revocation lists, report path or None, now) for both verifiers."""
    chain = Chain()
    report = tmp_path / "report.json"
    report.write_bytes(json.dumps(REPORT).encode())

    def direct():
        unsigned = build_credential(REPORT, file_digest(report), issuer=ROOT_DID,
                                    key_id=f"{ROOT_DID}#key-1", issued_at=NOW)
        return sign_credential(unsigned, chain.token_a, key_id=f"{ROOT_DID}#key-1", created=NOW)

    delegated = chain.credential(report_hash=file_digest(report))
    tampered = json.loads(json.dumps(delegated))
    tampered["credentialSubject"]["evalRun"]["status"] = "evaluated-forged"
    altered_delegation = json.loads(json.dumps(delegated))
    altered_delegation[DELEGATION_FIELD]["validUntil"] = "2031-01-01T00:00:00Z"
    outside = json.loads(json.dumps(delegated))
    outside["proof"]["created"] = "2020-01-01T00:00:00Z"

    no_delegation = sign_credential(
        build_credential(REPORT, "sha256:x", issuer=ROOT_DID, key_id=ISSUING_ID, issued_at=NOW),
        chain.issuing, key_id=ISSUING_ID, created=NOW,
    )
    wrong_method = build_credential(REPORT, "sha256:x", issuer=ROOT_DID, key_id=ISSUING_ID,
                                    issued_at=NOW)
    wrong_method[DELEGATION_FIELD] = chain.delegation()
    wrong_method = sign_document(wrong_method, chain.issuing, key_id=f"{ROOT_DID}#other",
                                 created=NOW)

    revoked = chain.revocations((ISSUING_ID, chain.issuing.public_key()))
    clean = chain.revocations()
    forged = chain.revocations(signer=FakeSigner())
    later = NOW + timedelta(seconds=1)
    return chain, [
        ("direct root signature", direct(), [], report, later),
        ("direct, with a clean list", direct(), [clean], None, later),
        ("delegated", delegated, [], report, NOW),
        ("delegated, clean list", delegated, [clean], None, NOW),
        ("delegated, issuing key revoked", delegated, [revoked], None, NOW),
        ("delegated, forged list", delegated, [forged], None, NOW),
        ("delegated by the second token",
         chain.credential(chain.delegation(signer=chain.token_a, key_id=f"{ROOT_DID}#key-1")),
         [], None, NOW),
        ("self-delegated", chain.credential(chain.delegation(signer=FakeSigner())), [], None, NOW),
        ("tampered credential", tampered, [], None, NOW),
        ("altered delegation", altered_delegation, [], None, NOW),
        ("proof outside the delegation", outside, [], None, NOW),
        ("issuing key with no delegation", no_delegation, [], None, later),
        ("proof names another key", wrong_method, [], None, NOW),
        ("expired credential", delegated, [], None, NOW + timedelta(days=91)),
        ("report swapped", delegated, [], tmp_path / "other.json", NOW),
    ]


def _python_checks(chain, credential, lists, report, now):
    root = TrustRoot(
        keys=[(method["id"], key) for method, key in zip(
            chain.root_document["verificationMethod"],
            [chain.token_a.public_key(), chain.token_b.public_key()], strict=True)],
        origin="parity", issuer=ROOT_DID,
    )
    result = verify_credential(credential, root, now=now, report_path=report,
                               revocation_lists=lists)
    return [[check.name, check.ok] for check in result.checks]


def test_python_and_browser_verifiers_reach_the_same_checks(tmp_path):
    node = shutil.which("node")
    if node is None:
        # In CI a missing node must fail: a skipped parity test there is a parity untested.
        assert not os.environ.get("CI"), "node is required in CI for the verify.js parity test"
        pytest.skip("node is not on the PATH: the browser verifier cannot be run here")
    (tmp_path / "other.json").write_bytes(b'{"not": "the report"}')
    chain, cases = _fixtures(tmp_path)

    fixtures, expected = [], {}
    for name, credential, lists, report, now in cases:
        fixtures.append({
            "name": name,
            "credential": credential,
            "didDocument": chain.root_document,
            "revocations": lists,
            "now": iso(now),
            "reportB64": base64.b64encode(report.read_bytes()).decode() if report else None,
        })
        expected[name] = _python_checks(chain, credential, lists, report, now)
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(fixtures), encoding="utf-8")

    completed = subprocess.run(
        [node, str(HARNESS), str(VERIFY_JS), str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    browser = json.loads(completed.stdout)

    # Both verdicts are what the fixture is for, not only identical.
    assert all(ok for _, ok in expected["delegated"])
    assert not all(ok for _, ok in expected["self-delegated"])
    for name in expected:
        assert browser[name] == expected[name], name


def test_the_browser_verifier_is_loadable_under_node_without_a_page():
    """verify.js only touches `document` when one exists, so the harness can require it."""
    source = VERIFY_JS.read_text(encoding="utf-8")
    assert 'typeof document !== "undefined"' in source
    assert "module.exports" in source
