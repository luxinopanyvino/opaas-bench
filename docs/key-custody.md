# Root key custody — onpremIALabs

The root Ed25519 private key is the system's only secret. Every credential is signed with it; with the public one, published in the DID document, anyone verifies a credential **without depending on onpremIALabs infrastructure**. If the key is lost, no new credentials can be issued and nothing can be re-certified; if it leaks, anyone can issue certificates that look legitimate.

> **What applies today, and what is decided.** The sections from *Generation* to *Rotation and revocation* describe how the tool works now: one key, in an encrypted file, signing every credential. [D65](decisions.md#d65--an-offline-root-on-two-tokens-delegates-to-an-issuing-key-issuances-stay-on-the-published-hash-chain) settles how the production root will be held, and that is different. The root stays offline on two hardware tokens and signs only delegations to an issuing key; the issuing key is the one that signs credentials. The decision is under [*Decided custody*](#decided-custody-d65) below. The tooling it needs exists ([D66](decisions.md#d66--the-delegation-walk-and-the-revocation-list-as-built)): `opaas delegate`, `sign --delegation`, `opaas revoke` and the walk in `verify`. What remains is the ceremony itself (phase 10, step 3 of the [work plan](workplan.md#phase-10-production-root-of-trust)), and until it is run no production key exists.

## Generation

```bash
opaas keygen --out keys --did did:web:onpremialabs.com
```

**`--did did:web:onpremialabs.com` is required for the production key, and it is not a default.** Without it, `keygen` names the key by itself (`did:key:z6Mk…`) and claims no organisation. Until D39 the onpremIALabs DID was the default, so every key anyone generated — test keys included — signed credentials claiming to come from onpremIALabs. The passphrase is typed at the prompt, not passed as an argument, so it stays out of the shell history.

It produces three files:

| File | What it is | Where it goes |
|---|---|---|
| `opaas-root-ed25519.pem` | PKCS#8 private key, encrypted if a passphrase was given | **never** into the repository |
| `opaas-root-ed25519.pub.pem` | public key | may be published |
| `did.json` | `did:web` document with the public key in multibase | served at `https://onpremialabs.com/.well-known/did.json` |

`keys/` is in `.gitignore`. The command refuses to overwrite an existing key except with `--overwrite`, because doing so would invalidate every already-signed credential.

## Custody rules

1. **Generate it on a trusted machine**, preferably offline, not in CI and not on a production server.
2. **Always with a passphrase.** An unencrypted key on disk is a key compromised the moment the machine is.
3. **Two backups on different media in different locations** (for example, two encrypted devices). The passphrase is kept separately from the key.
4. **The key does not travel**: signing happens where the key lives. `opaas sign` (Phase 4) receives the report, not the other way round.
5. **Usage log**: every issued credential records the signed `runId` and `reportHash`, so it is possible to audit what was signed and when.

## Public key distribution

For `opaas verify` to work without a network, the verifier needs the key beforehand. Three routes, from most to least convenient for them:

1. **Bundled anchor**: the `did.json` is included in the OPAAS package (`credential/anchors/`). It works out of the box. Empty until the production key exists.
2. **Installed anchor**: the verifier pins `did.json` once.

   ```bash
   opaas trust --add did.json
   ```

   It prints the fingerprint and waits before writing anything, because **pinning is the moment the whole chain rests on**: from then on, everything that issuer signs verifies without further questions. Compare that fingerprint against a channel other than the one the file arrived through — a document that vouches for itself vouches for nothing. `opaas trust` with no arguments lists what is already pinned, with fingerprints.
3. **Explicit**: `--did-document did.json` or `--key opaas-root-ed25519.pub.pem` on every verification.

The key is never distributed inside the credential: a credential that carries its own key verifies itself.

## The ceremony, rehearsed

The full chain — `keygen` → `sign` → `trust --add` → `verify` with no key flags — has been exercised end to end with a throwaway key, so the production ceremony is not the first time it runs. What it produced, in order:

| step | result |
|---|---|
| `keygen` | three files, and a fingerprint to write down |
| `sign` | credential plus an entry in the transparency log |
| `verify` **without** an anchor | refuses, listing the three ways to supply the key |
| `trust --add` | prints the fingerprint, then pins |
| `verify` again | six checks pass: structure, cryptosuite, signature, validity, report hash, log inclusion |

The throwaway key was discarded. Nothing about the rehearsal is committed: bundling a test anchor would make `verify` accept credentials signed with a key that is not the production one, which is worse than having no anchor at all.

## Rotation and revocation

- A planned rotation adds `#key-2` to the DID document and keeps `#key-1` published for as long as live credentials signed with it exist: withdrawing it would invalidate verification of legitimate credentials that have not yet expired.
- On suspicion of compromise: publish the new key, mark the previous one as revoked in the DID document with the date, and reissue the credentials that have not expired.
- The credentials' 90-day expiry (D28) limits the reach of a compromise on its own.

## Decided custody (D65)

The three questions left open here were settled in [D65](decisions.md#d65--an-offline-root-on-two-tokens-delegates-to-an-issuing-key-issuances-stay-on-the-published-hash-chain), which also records what each answer loses.

**Key separation.** An offline root signs *delegations* to an issuing key, and only the issuing key signs credentials. A delegation states the issuing key's public key, its verification-method id and its validity window, and is signed with `eddsa-jcs-2022` over its JCS bytes. Depth is fixed at root → issuing key → credential. Delegations last months, never less than a credential's 90 days (D28); the exact length is chosen at the ceremony.

**Where each key lives.**

| Key | Held in | Used for | Backup |
|---|---|---|---|
| root `#key-1` | hardware token A, generated on it, never exported | signing delegations, a few times a year | token B |
| root `#key-2` | hardware token B, generated on it, never exported | the same | token A |
| issuing key | PKCS#8 file encrypted with a passphrase, on the signing machine | signing every credential | two encrypted copies on different media, passphrase kept apart (rule 3 above) |

Both root keys are published in `did.json` and both are in the bundled anchor. The two tokens are kept in different places, each with its own PIN, and the PIN is never kept next to its token. The custody rules above apply to the issuing key unchanged. For the root they become: generated on the token, used only on an offline machine, and never brought online to sign a credential.

**What `verify` does.** It takes the root from the same three places as today, never from the credential. It then checks, in order:

1. the delegation's signature under that root;
2. that the delegation's validity covers the credential's `proof.created`;
3. that the issuing key is not on the revocation list;
4. that `proof.verificationMethod` names the delegated key;
5. the credential's signature under the issuing key.

A delegation may travel with the credential, because it is believed only after it verifies under an anchor. A key in the credential that no anchor delegates is refused, as now. `--key` still means *trust this key directly*.

**Rotation under D65.** A planned rotation of the issuing key is a new delegation signed by the root, published before the old one expires. A compromised issuing key goes on the revocation list and is replaced by a new delegation. Neither touches the anchor. Losing one token leaves the other root key working; the lost key is revoked in `did.json` and a replacement token gets a new key, which needs a new anchor in the next release. Losing both means a new root.

### The commands (D66)

The root signs from its token through PKCS#11 (`uv sync --extra token`), on the offline machine. The PIN is read from `OPAAS_TOKEN_PIN` or asked, never passed as an argument. `--root-key <file.pem>` signs from a file instead, which is what the rehearsal uses.

```bash
# On the offline machine, with token A (or B): delegate to the issuing key.
opaas delegate --issuing-key issuing/opaas-root-ed25519.pub.pem \
  --root-did-document root/did.json \
  --pkcs11-module /usr/lib/libykcs11.so --key-label "root key-1" \
  --id did:web:onpremialabs.com#issuing-2026-10 --valid-days 180 --out delegation.json

# On the signing machine: every credential carries the delegation.
opaas sign --input report.json --key issuing/opaas-root-ed25519.pem --delegation delegation.json
```

`root/did.json` lists both token keys as `#key-1` and `#key-2` (`credential.build_root_did_document` assembles it from the two public keys). The issuing key is generated with `opaas keygen` as any other key, and its own `did.json` is not used: with `--delegation`, the credential is issued under the root's name. `delegate` refuses a root key its document does not publish and a window of 90 days or less, and re-verifies what the token signed. `sign` refuses a delegation that has expired, has not started, or names another key.

### Rotation and revocation under D65, step by step

**Planned rotation of the issuing key** (before its delegation ends):

1. Generate the new issuing key on the signing machine (`opaas keygen --out issuing-2027-04`), with a passphrase. Copy only its `.pub.pem` to the offline machine.
2. On the offline machine, with either token: `opaas delegate --issuing-key …pub.pem --id did:web:onpremialabs.com#issuing-2027-04 …`. The new window starts before the old one ends, so there is no gap.
3. Bring `delegation.json` back and sign with the new key and `--delegation`. Credentials signed with the old key stay valid: their delegation covered their `proof.created`.
4. Once the old delegation has ended, the old key cannot sign anything that verifies. Destroy it and its backups.

**Compromised issuing key:**

1. On the offline machine: `opaas revoke --delegation <its delegation.json> --reason compromise --list revocations.json --root-did-document root/did.json --pkcs11-module … --key-label …`. The list is extended and re-signed. The first time, omit `--list` and a new one is written.
2. Publish `revocations.json` beside `did.json` and in the public repository with the issuance log. Verifiers install it with `opaas trust --add revocations.json`, which checks it against the pinned root and refuses an older list than the one installed. The site carries it with `publish --revocations`.
3. Every credential signed by that key now fails `revocation`, whatever its date: after a compromise, the proof date cannot be trusted. Rotate as above and reissue the credentials that had not expired, checking the issuance log for any that are not yours.

**One token lost:** `opaas revoke --id did:web:onpremialabs.com#key-2 --reason "lost token" …`, signed with the remaining token. Delegations the lost key signed stop verifying, so any issuing key it delegated needs a new delegation from the remaining token. A replacement token gets a new key, which goes into `did.json` as `#key-3` and into the bundled anchor of the next release.

**Both tokens lost:** there is no root left to sign a revocation. A new root is generated, published and shipped as a new anchor, and every live credential is reissued under it.

The revocation list is read offline: a verifier who never installs a new list never learns of a revocation. What bounds that is the delegation's end date, which is why delegations are kept to months.

**Issuance log.** The local hash chain of D21 stays the issuance log, and it is published in a public git repository, so every clone is a dated copy held by someone else. A public log, Certificate Transparency style, is not adopted: it would need the network at signing time and a third-party operator, and it would publish when credentials are issued and how many. What is given up is detecting a credential issued with a valid key and kept out of the published chain.
