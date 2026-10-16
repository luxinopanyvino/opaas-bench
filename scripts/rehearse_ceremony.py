"""Rehearse the production key ceremony end to end with throwaway keys (phase 10, step 3).

    SOFTHSM2_MODULE=/usr/lib/softhsm/libsofthsm2.so python scripts/rehearse_ceremony.py
    python scripts/rehearse_ceremony.py --files

The ceremony in `docs/ceremony.md` is run once, by hand, with two hardware
tokens. Everything in it that is not the token's own tooling (ykman) runs here
first, in the same order and through the same `opaas` commands, so that on
the day nothing runs for the first time:

1. two tokens, A and B, each generating its own Ed25519 key that never leaves it;
2. `opaas root-document` from the two public halves;
3. `opaas keygen` for the issuing key, encrypted with a passphrase;
4. `opaas delegate` from token A, through PKCS#11 (`Pkcs11Signer`);
5. `opaas sign --delegation`, with the issuance on the hash-chain log;
6. `opaas verify` refusing without an anchor, `trust --add`, `verify` passing;
7. a second delegation from token B, so the backup token is known to sign;
8. `opaas revoke`, signed by token B, installed, and `verify` refusing.

**Tokens.** With `SOFTHSM2_MODULE` set, A and B are two SoftHSM2 tokens and
the root keys are generated inside them with `CKM_EC_EDWARDS_KEY_PAIR_GEN`,
non-extractable, exactly as `ykman piv keys generate` does on a YubiKey: the
signer and `delegate` see a real PKCS#11 module. SoftHSM 2.5 or later built
against OpenSSL 1.1.1+ is needed for Ed25519; the module is asked for
`CKM_EDDSA` before anything else and the rehearsal stops if it lacks it.
`SOFTHSM2_UTIL` names `softhsm2-util` when it is not on the PATH, and
`SOFTHSM2_UTIL_MODULE` the module it loads when that differs (the Windows
build ships a 32-bit util beside a 64-bit module).

`--files` replaces the two tokens with key files and rehearses everything
else. It is what a machine without an Ed25519-capable PKCS#11 module can do.

Nothing here is a production key: the DID is `did:web:rehearsal.example`, the
PINs and passphrase are written in this file, and the working directory is a
temporary one, deleted at the end unless `--keep`. The script refuses to run
if `core/credential/anchors/` holds any JSON, and checks it is still empty at
the end. Exit 0 when every step did what the ceremony expects, 1 when one did
not, 3 when this machine cannot rehearse with tokens (no module, no Ed25519).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
ANCHORS = REPO / "src" / "opaas" / "core" / "credential" / "anchors"

DID = "did:web:rehearsal.example"
ISSUING_ID = f"{DID}#issuing-rehearsal"
SO_PIN = "87654321"
PIN = "123456"
PASSPHRASE = "rehearsal passphrase, not a secret"
#: Token label, key label, CKA_ID: the two root tokens and the key each generates.
TOKENS = {
    "A": ("opaas-root-A", "root key-1", b"\x01"),
    "B": ("opaas-root-B", "root key-2", b"\x02"),
}
#: DER of the OID 1.3.101.112 (id-Ed25519): the CKA_EC_PARAMS of an Ed25519 key.
ED25519_PARAMS = bytes.fromhex("06032b6570")
#: A report with the fields `sign` reads; written as canonical JCS like any report.
REPORT = {
    "runId": "20261016-001",
    "opaasVersion": "rehearsal",
    "status": "evaluated",
    "model": {"name": "rehearsal/model", "weightsHash": {"hash": "sha256:00"}},
    "configuration": {"backend": "none", "configHash": "sha256:00"},
    "coverage": {"complete": True, "blocksEvaluated": ["A", "B", "C", "D", "E"]},
}


class Rehearsal:
    def __init__(self, workdir: Path, *, tokens: bool) -> None:
        self.workdir = workdir
        self.tokens = tokens
        self.steps: list[dict[str, Any]] = []
        self.fingerprints: list[str] = []
        self.checks: list[str] = []
        self.env = dict(os.environ)
        self.env.update({
            "OPAAS_ANCHOR_DIR": str(workdir / "anchors"),
            "OPAAS_KEY_PASSPHRASE": PASSPHRASE,
            "OPAAS_TOKEN_PIN": PIN,
            "NO_COLOR": "1",
            "COLUMNS": "200",
            "PYTHONIOENCODING": "utf-8",
        })

    # ------------------------------------------------------------ recording

    def opaas(self, step: str, *args: str, expect: int = 0, contains: str = "") -> str:
        command = [sys.executable, "-m", "opaas", *args]
        result = subprocess.run(
            command, cwd=self.workdir, env=self.env, capture_output=True, text=True,
            encoding="utf-8",
        )
        output = result.stdout + result.stderr
        ok = result.returncode == expect and contains in " ".join(output.split())
        self.record(step, ok, f"exit {result.returncode} (expected {expect})", output,
                    command=["opaas", *args])
        return output

    def record(self, step: str, ok: bool, detail: str, output: str = "", **extra: Any) -> None:
        self.steps.append({"step": step, "ok": ok, "detail": detail, **extra})
        mark = "ok  " if ok else "FAIL"
        print(f"[{mark}] {step}: {detail}")
        if output and not ok:
            print("       " + output.strip().replace("\n", "\n       "))

    # --------------------------------------------------------------- tokens

    def prepare_tokens(self) -> dict[str, list[str]]:
        """Root signer arguments for A and B: a PKCS#11 token each, or a key file each."""
        if not self.tokens:
            return self._key_files()
        module = os.environ["SOFTHSM2_MODULE"]
        conf = self.workdir / "softhsm2.conf"
        (self.workdir / "hsm").mkdir()
        conf.write_text(
            f"directories.tokendir = {(self.workdir / 'hsm').as_posix()}\n"
            "objectstore.backend = file\nlog.level = ERROR\nslots.removable = false\n",
            encoding="utf-8",
        )
        self.env["SOFTHSM2_CONF"] = str(conf)
        util = os.environ.get("SOFTHSM2_UTIL") or shutil.which("softhsm2-util")
        if not util:
            raise CannotRehearse("softhsm2-util is not on the PATH; set SOFTHSM2_UTIL.")
        util_module = os.environ.get("SOFTHSM2_UTIL_MODULE")
        for letter, (token_label, _, _) in TOKENS.items():
            command = [util, "--init-token", "--free", "--label", token_label,
                       "--so-pin", SO_PIN, "--pin", PIN]
            if util_module:
                command[1:1] = ["--module", util_module]
            result = subprocess.run(command, env=self.env, capture_output=True, text=True)
            self.record(f"token {letter}: initialised", result.returncode == 0,
                        token_label, result.stdout + result.stderr)
        self._child("mechanisms", module)
        signers = {}
        for letter, (token_label, key_label, key_id) in TOKENS.items():
            self._child("keygen", module, token_label, key_label, key_id.hex())
            signers[letter] = ["--pkcs11-module", module, "--token-label", token_label,
                               "--key-label", key_label]
            self._child("export", module, token_label, key_label,
                        str(self.workdir / "tokens" / f"{letter}.pub.pem"))
        return signers

    def _child(self, action: str, *args: str) -> None:
        """Token work in a child process: each PKCS#11 session is opened and finalised whole."""
        result = subprocess.run(
            [sys.executable, __file__, "--token-child", action, *args],
            env=self.env, capture_output=True, text=True, encoding="utf-8",
        )
        lines = (result.stdout or "").strip().splitlines() or [""]
        if result.returncode == 3:
            raise CannotRehearse(lines[-1] or result.stderr.strip())
        self.record(f"token: {action} {' '.join(args[1:3])}".strip(), result.returncode == 0,
                    lines[-1], result.stdout + result.stderr)

    def _key_files(self) -> dict[str, list[str]]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        folder = self.workdir / "tokens"
        folder.mkdir()
        signers = {}
        for letter in TOKENS:
            key = Ed25519PrivateKey.generate()
            (folder / f"{letter}.pem").write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(PASSPHRASE.encode("utf-8")),
            ))
            (folder / f"{letter}.pub.pem").write_bytes(key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            signers[letter] = ["--root-key", str(folder / f"{letter}.pem")]
        self.record("tokens replaced by encrypted key files", True, "--files: no PKCS#11")
        return signers

    # ------------------------------------------------------------- ceremony

    def run(self) -> None:
        signers = self.prepare_tokens()
        tokens = self.workdir / "tokens"

        out = self.opaas(
            "root-document from both tokens", "root-document",
            "--public-key", str(tokens / "A.pub.pem"), "--public-key", str(tokens / "B.pub.pem"),
            "--did", DID, "--out", "root/did.json", contains=f"{DID}#key-2",
        )
        self.fingerprints = [line.split(":", 1)[1].strip() for line in out.splitlines()
                             if line.strip().startswith("fingerprint")]

        self.opaas("issuing key with a passphrase", "keygen", "--out", "issuing", "--did", DID)
        pem = (self.workdir / "issuing" / "opaas-root-ed25519.pem").read_text(encoding="utf-8")
        self.record("issuing key is encrypted", "ENCRYPTED PRIVATE KEY" in pem, "PKCS#8")

        self.opaas(
            "delegate from token A", "delegate",
            "--issuing-key", "issuing/opaas-root-ed25519.pub.pem",
            "--root-did-document", "root/did.json", *signers["A"],
            "--id", ISSUING_ID, "--valid-days", "180", "--out", "delegation.json",
            contains=f"{DID}#key-1",
        )

        from opaas.core.canonical import canonicalize

        (self.workdir / "report.json").write_bytes(canonicalize(REPORT))
        self.opaas(
            "sign with the delegation", "sign", "--input", "report.json",
            "--key", "issuing/opaas-root-ed25519.pem", "--delegation", "delegation.json",
            "--log", "log/issuances.jsonl", "--out", "credential.json",
        )
        self.opaas("hash-chain log adds up", "log-verify", "log/issuances.jsonl")

        self.opaas("verify with no anchor refuses", "verify", "--credential", "credential.json",
                   expect=2)
        self.opaas("trust --add root/did.json", "trust", "--add", "root/did.json", "--yes")
        verified = self.opaas(
            "verify with the anchor, no --key", "verify", "--credential", "credential.json",
            "--report", "report.json", "--log", "log/issuances.jsonl",
            contains="delegation-validity",
        )
        self.checks = [line.split()[1].rstrip(":") for line in verified.splitlines()
                       if line.split()[:1] == ["OK"] and len(line.split()) > 1]

        self.opaas(
            "delegate from token B (the backup signs too)", "delegate",
            "--issuing-key", "issuing/opaas-root-ed25519.pub.pem",
            "--root-did-document", "root/did.json", *signers["B"],
            "--id", ISSUING_ID, "--valid-days", "180", "--out", "delegation-b.json",
            contains=f"{DID}#key-2",
        )
        self.opaas(
            "revoke the issuing key, signed by token B", "revoke",
            "--root-did-document", "root/did.json", *signers["B"],
            "--delegation", "delegation.json", "--reason", "rehearsal",
            "--out", "revocations.json",
        )
        self.opaas("trust --add revocations.json", "trust", "--add", "revocations.json", "--yes")
        self.opaas("verify after revocation refuses", "verify", "--credential", "credential.json",
                   expect=1, contains="REVOKED")

        self._no_secret_left()

    def _no_secret_left(self) -> None:
        """Every private key on disk is encrypted; with tokens, no root key is on disk at all."""
        plain = []
        for path in self.workdir.rglob("*"):
            if path.is_file() and path.suffix in (".pem", ".json", ".jsonl", ".key"):
                text = path.read_text(encoding="utf-8", errors="replace")
                if "PRIVATE KEY" in text and "ENCRYPTED PRIVATE KEY" not in text:
                    plain.append(path.relative_to(self.workdir).as_posix())
        self.record("no unencrypted private key on disk", not plain,
                    ", ".join(sorted(plain)) or "none")


class CannotRehearse(RuntimeError):
    """This machine cannot rehearse with tokens."""


def token_child(action: str, module: str, *args: str) -> int:
    """One PKCS#11 action, in its own process. Prints one line; exit 3 = cannot rehearse."""
    try:
        import pkcs11
        from pkcs11 import Attribute, KeyType, Mechanism
    except ImportError:
        print("python-pkcs11 is missing: uv sync --extra token.")
        return 3
    library = pkcs11.lib(module)
    if action == "mechanisms":
        token = library.get_token(token_label=TOKENS["A"][0])
        mechanisms = set(token.slot.get_mechanisms())
        needed = {Mechanism.EC_EDWARDS_KEY_PAIR_GEN, Mechanism.EDDSA}
        if not needed <= mechanisms:
            missing = ", ".join(sorted(m.name for m in needed - mechanisms))
            print(f"{library.manufacturer_id.strip()} {library.library_version} lacks {missing}: "
                  "no Ed25519 on this module (SoftHSM needs 2.5+ built with OpenSSL 1.1.1+).")
            return 3
        print("CKM_EC_EDWARDS_KEY_PAIR_GEN and CKM_EDDSA present")
        return 0

    token_label, key_label = args[0], args[1]
    if action == "keygen":
        with library.get_token(token_label=token_label).open(user_pin=PIN, rw=True) as session:
            parameters = session.create_domain_parameters(
                KeyType.EC_EDWARDS, {Attribute.EC_PARAMS: ED25519_PARAMS}, local=True
            )
            _, private = parameters.generate_keypair(
                mechanism=Mechanism.EC_EDWARDS_KEY_PAIR_GEN, store=True, label=key_label,
                id=bytes.fromhex(args[2]),
                private_template={Attribute.SENSITIVE: True, Attribute.EXTRACTABLE: False},
            )
            try:
                private[Attribute.VALUE]
            except pkcs11.PKCS11Error:
                print(f"generated {key_label!r} on {token_label}; the private value is unreadable")
                return 0
            print(f"generated {key_label!r} on {token_label}, but its private value is readable")
            return 1
    if action == "export":
        from cryptography.hazmat.primitives import serialization

        from opaas.core.credential import Pkcs11Signer, fingerprint

        signer = Pkcs11Signer(module=module, key_label=key_label, pin=PIN,
                              token_label=token_label).open()
        public = signer.public_key()
        signer.close()
        target = Path(args[2])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
        print(f"exported the public half of {key_label!r}: {fingerprint(public)}")
        return 0
    print(f"unknown token action {action!r}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--files", action="store_true", help="Key files instead of tokens.")
    parser.add_argument("--workdir", type=Path, help="Working directory (default: a temp one).")
    parser.add_argument("--keep", action="store_true", help="Keep the working directory.")
    parser.add_argument("--summary", type=Path, help="Write the steps as JSON here.")
    parser.add_argument("--token-child", nargs="+", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.token_child:
        return token_child(*options.token_child)

    if any(ANCHORS.rglob("*.json")):
        print(f"{ANCHORS} is not empty. A rehearsal never runs next to a bundled anchor.")
        return 1
    tokens = not options.files
    if tokens and not os.environ.get("SOFTHSM2_MODULE"):
        print("Set SOFTHSM2_MODULE to a SoftHSM2 module with Ed25519, or pass --files.")
        return 3

    workdir = options.workdir or Path(tempfile.mkdtemp(prefix="opaas-ceremony-"))
    workdir.mkdir(parents=True, exist_ok=True)
    rehearsal = Rehearsal(workdir, tokens=tokens)
    print(f"Rehearsing in {workdir} with {'SoftHSM2 tokens' if tokens else 'key files'}.")
    code = 0
    try:
        rehearsal.run()
    except CannotRehearse as exc:
        print(f"Cannot rehearse with tokens here: {exc}")
        code = 3
    finally:
        anchors_empty = not any(ANCHORS.rglob("*.json"))
        rehearsal.record("bundled anchors still empty", anchors_empty, str(ANCHORS.name))
        if options.summary:
            options.summary.write_text(json.dumps({
                "mode": "softhsm2" if tokens else "files",
                "fingerprints": getattr(rehearsal, "fingerprints", []),
                "verifyChecks": getattr(rehearsal, "checks", []),
                "steps": rehearsal.steps,
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not options.keep and options.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    if code:
        return code
    failed = [step["step"] for step in rehearsal.steps if not step["ok"]]
    print(f"\n{len(rehearsal.steps) - len(failed)}/{len(rehearsal.steps)} steps as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
