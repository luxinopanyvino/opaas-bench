# The production key ceremony — runbook

Phase 10, step 3 of the [work plan](workplan.md#phase-10-production-root-of-trust): create the onpremIALabs root on two hardware tokens, delegate to the first issuing key, publish the root's DID document, and prepare the pull request that ships it as the bundled anchor. What is decided, and why, is in [D65](decisions.md#d65--an-offline-root-on-two-tokens-delegates-to-an-issuing-key-issuances-stay-on-the-published-hash-chain), [D66](decisions.md#d66--the-delegation-walk-and-the-revocation-list-as-built) and [D67](decisions.md#d67--the-ceremonys-token-profile-and-how-the-signer-finds-the-tokens-key); the custody rules are in [key custody](key-custody.md). This page is only the order of operations.

The ceremony is run once, by the maintainer, by hand. Every `opaas` command in it has already been run in the same order by [`scripts/rehearse_ceremony.py`](../scripts/rehearse_ceremony.py) (see [*The ceremony, rehearsed*](key-custody.md#the-ceremony-rehearsed)). The token's own tooling could not be run here, because it needs a YubiKey. Each command carries one of these marks:

| mark | meaning |
|---|---|
| **[rehearsed]** | run end to end by the rehearsal script, with the same arguments except paths, PINs and the DID |
| **[stand-in]** | the OPAAS code path runs, but against a stand-in for the token (a fake binding, or SoftHSM without Ed25519), not against a real Ed25519 token |
| **[unverified]** | written from the vendor's documentation and never run by the author of this page; check it in the dress rehearsal (§2) |

Nothing below is a secret, except the PINs, PUKs and the issuing key's passphrase, which are never written into a file or a shell command.

---

## 1. Prerequisites

**Two tokens that generate Ed25519 on the device.** YubiKey 5 series with **firmware 5.7 or later** (`ykman info` prints *Firmware version*). Earlier firmware has no Ed25519 in its PIV applet, and the firmware cannot be upgraded. Buy a **third** token of the same model for the dress rehearsal (§2). Do not use it for the root afterwards.

**Software, at the versions that know Ed25519 PIV keys** [unverified]:

| tool | minimum | used for |
|---|---|---|
| `ykman` (yubikey-manager) | 5.4 | PIN/PUK/management key, key generation, attestation |
| `ykcs11` (yubico-piv-tool) | 2.6 | the PKCS#11 module `opaas` signs through |
| `opaas` with the `token` extra | this release | `root-document`, `delegate`, `revoke` |
| OpenSC `pkcs11-tool` (optional) | 0.25 | listing what `ykcs11` shows, to find labels |

Distribution packages are often older than this (Debian 12 ships yubico-piv-tool 2.3). Check the version of each before the day.

**The offline machine.** A laptop booted from a live Linux USB stick, with Wi-Fi and Bluetooth off and no network cable. It never connects to a network during or after the ceremony. Everything it needs comes in on a second USB stick, prepared on an online machine:

```bash
# On the online machine, from a checkout of the release that will ship the anchor. [unverified]
uv build                                               # dist/opaas-<version>-py3-none-any.whl
uv export --extra token --no-dev --no-emit-project --format requirements-txt > requirements.txt
python -m pip download -r requirements.txt -d wheelhouse \
  --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all:
cp dist/opaas-*.whl wheelhouse/
python -m pip download yubikey-manager -d wheelhouse --only-binary=:all: \
  --platform manylinux2014_x86_64 --python-version 3.12
# plus yubico-piv-tool >= 2.6 for the live system (package or build), copied the same way
```

```bash
# On the offline machine. [unverified]
python3 -m venv ~/opaas && . ~/opaas/bin/activate
pip install --no-index --find-links wheelhouse -r requirements.txt wheelhouse/opaas-*.whl
pip install --no-index --find-links wheelhouse yubikey-manager
opaas version
```

**The clock.** Delegations start at the offline machine's *now* and `sign` refuses one that has not started yet. Set the offline machine's clock by hand to UTC and compare it against a phone before §6. A clock in the future yields a delegation the signing machine refuses; one in the past shortens it.

**People and paper.** Two people if possible: one runs the commands, one reads the fingerprints back. A printed copy of the record sheet (§9). Two tamper-evident bags, one per token, and two separate places to keep them in, each away from where its PIN is kept.

**Decide before the day:**

- the issuing key's id, e.g. `did:web:onpremialabs.com#issuing-2026-10` (year-month of the ceremony);
- the delegation length: 180 days (the default) or another number of months, always over 90 (D65);
- who holds token A, who holds token B, where, and where each PIN is kept.

---

## 2. Dress rehearsal with the spare token

The rehearsal script covered every `opaas` step against key files and the PKCS#11 code against a stand-in. What no machine here could run is `ykman` and `ykcs11` against a real Ed25519 PIV key. Run sections 3 to 7 **once, completely, with the spare token playing both A and B** (reset it between the two: `ykman piv reset`), on the same offline machine, with `did:web:rehearsal.example` instead of `did:web:onpremialabs.com`, and throwaway PINs. Then reset the spare token and wipe the rehearsal files.

This is where any **[unverified]** command is found to be wrong, before a root exists. Note what had to change, and fix this page in the follow-up pull request.

Before starting, run the OPAAS rehearsal on the offline machine itself:

```bash
python scripts/rehearse_ceremony.py --files            # [rehearsed] 16/16 steps expected
```

---

## 3. Prepare each token (A, then B)

Plug in **only one token at a time**. Everything in this section is done for A, then repeated for B. All of it is [unverified].

```bash
ykman info                              # serial number, firmware >= 5.7; write both down
ykman piv reset                         # start from a known state (wipes the PIV applet)
ykman piv access change-pin             # default PIN 123456  -> a new PIN, 6–8 characters
ykman piv access change-puk             # default PUK 12345678 -> a new PUK
ykman piv access change-management-key --generate --protect --algorithm AES256
```

`--protect` stores the new random management key on the token, behind the PIN, so there is no third secret to keep. The PIN and PUK are typed at the prompt and written on the token's own sheet, which is kept apart from the token.

**Generate the root key on the token.** The slot, PIN policy and touch policy are D67's:

```bash
ykman piv keys generate --algorithm ED25519 \
  --pin-policy ONCE --touch-policy ALWAYS 9a A.pub.pem      # for token B: B.pub.pem
```

- **Slot 9a**, not 9c. `ykcs11` asks for the PIN again at every signature on 9c (`CKA_ALWAYS_AUTHENTICATE`), and `opaas` cannot answer that: the signer refuses such a key when it opens it, before signing anything (D67).
- **PIN policy ONCE, touch policy ALWAYS.** The PIN unlocks the session; every signature needs a finger on the token. A machine that has the token and the PIN still cannot sign without someone touching it.
- The private key is generated inside the token and cannot be exported: there is no command to do so, and that is the point (D65).

**Prove the key was generated on the token.** The attestation is a certificate the token signs with Yubico's factory key, stating that the key in 9a was generated on the device and not imported:

```bash
ykman piv keys attest 9a A.attestation.pem
ykman piv certificates export f9 A.intermediate.pem
```

Verify the chain against Yubico's PIV root CA (downloaded beforehand and brought on the USB stick) [unverified]:

```bash
openssl verify -CAfile yubico-piv-ca-1.pem -untrusted A.intermediate.pem A.attestation.pem
```

**Give `ykcs11` a certificate to show the key by.** `ykcs11` lists a slot's key objects from the certificate in it [unverified: newer versions may read the slot's metadata instead]:

```bash
ykman piv certificates generate --subject "CN=onpremIALabs OPAAS root key-1" \
  --valid-days 3650 9a A.pub.pem                            # key-2 for token B; touch the token
```

The certificate is never used by OPAAS. It only makes the key visible through PKCS#11.

**See the key through PKCS#11.** With OpenSC [unverified]:

```bash
pkcs11-tool --module /usr/lib/x86_64-linux-gnu/libykcs11.so --list-token-slots
pkcs11-tool --module /usr/lib/x86_64-linux-gnu/libykcs11.so --login --list-objects
```

Expected: a token labelled like `YubiKey PIV #<serial>`, a private key `Private key for PIV Authentication` and a public key `Public key for PIV Authentication`, both `EC_EDWARDS`, with the same `ID` (01). `opaas` finds the private key by the label given with `--key-label`, and its public half by `ID` (D67). Write the token label and the key label down: §6 uses them. The module's path varies by system (`libykcs11.so`, `libykcs11.dylib` on macOS, `libykcs11.dll` on Windows).

Unplug the token. Repeat for B.

---

## 4. Build the root's DID document

Only the two public halves meet here. [rehearsed]

```bash
opaas root-document --public-key A.pub.pem --public-key B.pub.pem \
  --did did:web:onpremialabs.com --out root/did.json
```

It writes `root/did.json` with `#key-1` (token A) and `#key-2` (token B), each listed under `assertionMethod` and `capabilityDelegation`, and prints for each key its **fingerprint** (16 hex characters), its **multibase**, and the **SHA-256 of the file**. It refuses one key given twice, and refuses to overwrite an existing document without `--overwrite`.

**Record and check over a second channel.** Write both fingerprints and both multibase values on the record sheet. The second person then reads them back from a different source than the terminal that produced them:

- from the token itself: on the online machine, after the ceremony is over, `ykman piv keys export 9a -` for each token prints the public key again. Convert it with `opaas root-document` into a scratch file and compare the fingerprints [unverified];
- after publication (§8), from the URL, on another device.

The fingerprints also go into the follow-up pull request description, so that the reviewer compares them a third time.

---

## 5. The issuing key

On the **signing machine**, the one that will sign credentials. Not on the offline machine: the issuing key lives where it is used (D65). [rehearsed]

```bash
opaas keygen --out issuing-2026-10 --did did:web:onpremialabs.com
```

Type the passphrase at the prompt. `OPAAS_KEY_PASSPHRASE` is for scripts, and an argument goes into the shell history. `keygen` writes `opaas-root-ed25519.pem` (encrypted PKCS#8), `opaas-root-ed25519.pub.pem` and a `did.json` that is **not used**: with a delegation, credentials are issued under the root's name.

The file names say *root* for historical reasons; this is the issuing key. Make the two encrypted backups on different media now (key custody rule 3). Keep the passphrase apart from them.

Copy **only** `issuing-2026-10/opaas-root-ed25519.pub.pem` to the offline machine.

---

## 6. Delegate to the issuing key, from token A

On the offline machine, with token A plugged in. [stand-in: the PKCS#11 path has signed with a fake binding and opened SoftHSM, but never a real Ed25519 token. `delegate` re-verifies what the token signed before writing it, so a wrong signature fails here, not later]

```bash
opaas delegate --issuing-key opaas-root-ed25519.pub.pem \
  --root-did-document root/did.json \
  --pkcs11-module /usr/lib/x86_64-linux-gnu/libykcs11.so \
  --token-label "YubiKey PIV #<serial A>" --key-label "Private key for PIV Authentication" \
  --id did:web:onpremialabs.com#issuing-2026-10 --valid-days 180 --out delegation.json
```

The PIN is asked at the keyboard (or read from `OPAAS_TOKEN_PIN`, which this ceremony does not use). Touch the token when it blinks. Expected output: `Delegation signed.`, root key `did:web:onpremialabs.com#key-1` with token A's fingerprint, the issuing key's id and fingerprint, and the window. `delegate` refuses with a clear message if:

- the token's key is not in `root/did.json`. This is also the cross-check that what `ykman` exported is the key `ykcs11` signs with;
- the key asks for the PIN at every signature (slot 9c, PIN policy ALWAYS);
- the window is 90 days or less.

---

## 7. Check token B, and write the first revocation list

Token B has to be known to sign before it is put away as the backup. It must not sign a second, live delegation to the production issuing key. It signs a delegation to a **throwaway** key, which is then revoked at once. That revocation is the first revocation list, signed by A. [rehearsed with key files; the token path is stand-in as in §6]

```bash
# Still on the offline machine. The throwaway key is never used for anything.
opaas keygen --out check-b --did did:web:onpremialabs.com        # any passphrase; destroyed below

# Token B plugged in (A unplugged):
opaas delegate --issuing-key check-b/opaas-root-ed25519.pub.pem \
  --root-did-document root/did.json \
  --pkcs11-module /usr/lib/x86_64-linux-gnu/libykcs11.so \
  --token-label "YubiKey PIV #<serial B>" --key-label "Private key for PIV Authentication" \
  --id did:web:onpremialabs.com#ceremony-check-key-2 --valid-days 91 --out check-b.delegation.json

# Token A plugged in again (B unplugged):
opaas revoke --root-did-document root/did.json --delegation check-b.delegation.json \
  --reason "ceremony check of token B; key destroyed" \
  --pkcs11-module /usr/lib/x86_64-linux-gnu/libykcs11.so \
  --token-label "YubiKey PIV #<serial A>" --key-label "Private key for PIV Authentication" \
  --out revocations.json

shred -u check-b/opaas-root-ed25519.pem && rm -r check-b                       # [unverified]
```

The output of `delegate` must name `#key-2` with token B's fingerprint. `revoke` writes `revocations.json` with one entry, signed by `#key-1`. `opaas revoke` has no option for an empty list. Starting with this entry gives verifiers a list that already verifies under the root. A later revocation extends it with `--list revocations.json`.

Put token B in its bag, sealed, and write the seal number on the record sheet.

---

## 8. First credential, verification, publication

**On the signing machine**, with `delegation.json` brought back on the USB stick. [rehearsed]

```bash
opaas sign --input report.json --key issuing-2026-10/opaas-root-ed25519.pem \
  --delegation delegation.json --log log/issuances.jsonl --out credential.json
opaas log-verify log/issuances.jsonl
```

**On a clean machine** that has never pinned anything (`OPAAS_ANCHOR_DIR` pointed at an empty directory is enough). [rehearsed]

```bash
opaas verify --credential credential.json            # must refuse: no anchor (exit 2)
opaas trust --add root/did.json                      # compare the fingerprints with the sheet
opaas trust --add revocations.json
opaas verify --credential credential.json --report report.json --log log/issuances.jsonl
```

The last command passes eleven checks: `structure`, `cryptosuite`, `anchor`, `delegation`, `delegation-validity`, `revocation`, `delegated-key`, `signature`, `validity`, `report`, `log`. `revocation` now says the list was checked, not that none was held.

**Publish the DID document** at `https://onpremialabs.com/.well-known/did.json`, byte for byte the file `root-document` wrote, and the revocation list beside it at `https://onpremialabs.com/.well-known/opaas-revocations.json` (the path `opaas publish --revocations` uses). Then, from another device and network [unverified]:

```bash
curl -fsS https://onpremialabs.com/.well-known/did.json | sha256sum   # = the sha256 on the sheet
curl -fsS https://onpremialabs.com/.well-known/opaas-revocations.json | sha256sum
```

**The public log repository** (D65: every clone is a dated copy held by someone else) receives, in one signed commit:

| file | what it is |
|---|---|
| `did.json` | the root document, identical to the published one |
| `revocations.json` | the first revocation list |
| `delegations/issuing-2026-10.json` | `delegation.json` |
| `attestation/key-1.pem`, `key-1.intermediate.pem`, same for `key-2` | the on-device generation proofs of §3 |
| `log/issuances.jsonl` | the D21 hash chain, from its first entry |
| `CEREMONY.md` | date, place, the two fingerprints, the file hashes, who was present |

No PIN, PUK, passphrase, token serial (keep those on the private sheet) or private key goes there.

---

## 9. Record sheet

Fill it in by hand during the ceremony. One copy stays with each token's custodian. The public parts go into `CEREMONY.md`.

```
Date (UTC) ........  Place ........  Present ........
Offline machine clock checked against ........ at ........
Token A  serial ........  firmware ........  seal ........  custodian ........
         key-1 fingerprint ................  multibase z6Mk................
Token B  serial ........  firmware ........  seal ........  custodian ........
         key-2 fingerprint ................  multibase z6Mk................
root/did.json sha256 ................................................................
Issuing key id did:web:onpremialabs.com#issuing-......  fingerprint ................
Delegation window ........ → ........   signed by key-1
Check delegation to #ceremony-check-key-2 signed by key-2: yes / no
revocations.json sha256 ................................................................
Fingerprints read back over a second channel by ........ : match / MISMATCH
```

A mismatch anywhere stops the ceremony. Nothing has been published yet at that point, and starting over costs two `ykman piv reset`.

---

## 10. The follow-up pull request

One pull request, after the DID document is live, titled like *Ship the onpremIALabs root as the bundled anchor*. It contains:

1. **`src/opaas/core/credential/anchors/did-web-onpremialabs.com.json`**: byte for byte the published `did.json`. `verify` reads every `*.json` in that directory (`trust.BUNDLED_ANCHORS`). The anchors `README.md` is updated to say what it now holds.
2. **No bundled revocation list** unless one is decided. `anchors/revocations/` would freeze a list into a release, while verifiers already install the current one with `trust --add`. If one is shipped, it must be the published one and the decision goes in a D-entry.
3. **A fixture**: `tests/fixtures/production-root/` with `report.json`, `credential.json` and `delegation.json` from §8. These are public documents.
4. **A test pinning resolution against the bundled anchor**, with no `--key`, no `--did-document` and an empty `OPAAS_ANCHOR_DIR`:
   - `resolve_trust("did:web:onpremialabs.com")` returns a root whose origin is the bundled directory, and whose two keys are **the two multibase values written on the sheet**, hard-coded in the test. Replacing the anchor then fails a test, as bumping a pinned dataset hash does (invariant 7);
   - `verify_credential(fixture, resolve_trust(issuer), now=<the credential's proof.created>)` is valid and walks `anchor` → `delegation` → … → `signature`. The time is fixed so that the test does not expire with the credential's 90 days or the delegation's 180;
   - `opaas verify --credential <fixture>` through the CLI with the same empty anchor directory reaches the bundled anchor (its output names it). The validity check fails once the credential expires, so this test asserts the origin and the delegation checks, not the exit code.
5. **Documentation:** the workplan's phase 10 row (step 3 done, *Done when* met); D65's status (all steps implemented); `key-custody.md` (*What applies today* says the production root exists, with its fingerprints); the anchors README; CHANGELOG (*Upgrading*: `verify` now trusts onpremIALabs' root out of the box); this page, with whatever the dress rehearsal corrected.
6. **The fingerprints in the pull request description**, for the reviewer to compare against the published `did.json` and the public log repository: the third channel.

Release it as a version bump (`pyproject.toml`, `opaas.__version__`, `CITATION.cff` together). From that release on, the anchor is very hard to take back (D65): a lost token is handled as in [key custody](key-custody.md#rotation-and-revocation-under-d65-step-by-step), and a new root means a new anchor in a new release.
