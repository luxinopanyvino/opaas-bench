"""The ceremony of `docs/ceremony.md`, rehearsed by `scripts/rehearse_ceremony.py`.

The file-key rehearsal always runs: every `opaas` command of the ceremony, in
its order, as a subprocess, with throwaway keys. The token rehearsal needs a
real PKCS#11 module with Ed25519 and runs only when `SOFTHSM2_MODULE` names
one (SoftHSM 2.5+ built with OpenSSL 1.1.1+); without it, it is skipped, never
faked. Neither touches the network or hardware, and both leave
`core/credential/anchors/` empty.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rehearse_ceremony.py"

#: What `verify` checks on a delegated credential with the anchor installed.
WALK = ["structure", "cryptosuite", "anchor", "delegation", "delegation-validity", "revocation",
        "delegated-key", "signature", "validity", "report", "log"]


def _script():
    spec = importlib.util.spec_from_file_location("rehearse_ceremony", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rehearse(tmp_path, *args):
    summary = tmp_path / "summary.json"
    code = _script().main(["--workdir", str(tmp_path / "work"), "--summary", str(summary), *args])
    return code, json.loads(summary.read_text(encoding="utf-8"))


def test_the_ceremony_rehearses_end_to_end_with_key_files(tmp_path):
    code, summary = _rehearse(tmp_path, "--files")
    failed = [step for step in summary["steps"] if not step["ok"]]
    assert code == 0, failed
    assert summary["verifyChecks"] == WALK
    assert len(summary["fingerprints"]) == 2


@pytest.mark.skipif(
    not os.environ.get("SOFTHSM2_MODULE")
    or not (os.environ.get("SOFTHSM2_UTIL") or shutil.which("softhsm2-util")),
    reason="needs SOFTHSM2_MODULE (SoftHSM2 with Ed25519) and softhsm2-util",
)
def test_the_ceremony_rehearses_end_to_end_on_two_softhsm_tokens(tmp_path):
    code, summary = _rehearse(tmp_path)
    failed = [step for step in summary["steps"] if not step["ok"]]
    assert code == 0, failed or "this SoftHSM2 module has no Ed25519 (exit 3)"
    assert summary["mode"] == "softhsm2"
    assert summary["verifyChecks"] == WALK
    steps = {step["step"]: step for step in summary["steps"]}
    assert "private value is unreadable" in steps["token: keygen opaas-root-A root key-1"]["detail"]
