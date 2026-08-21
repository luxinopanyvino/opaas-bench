# Root key custody — onpremIALabs

The root Ed25519 private key is the system's only secret. Every credential is signed with it; with the public one, published in the DID document, anyone verifies a credential **without depending on onpremIALabs infrastructure**. If the key is lost, no new credentials can be issued and nothing can be re-certified; if it leaks, anyone can issue certificates that look legitimate.

## Generation

```bash
opaas keygen --out keys --passphrase "<long passphrase>"
```

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
2. **Installed anchor**: the verifier copies `did.json` into `~/.opaas/anchors/` once.
3. **Explicit**: `--did-document did.json` or `--key opaas-root-ed25519.pub.pem` on every verification.

The key is never distributed inside the credential: a credential that carries its own key verifies itself.

## Rotation and revocation

- A planned rotation adds `#key-2` to the DID document and keeps `#key-1` published for as long as live credentials signed with it exist: withdrawing it would invalidate verification of legitimate credentials that have not yet expired.
- On suspicion of compromise: publish the new key, mark the previous one as revoked in the DID document with the date, and reissue the credentials that have not expired.
- The credentials' 6-month expiry limits the reach of a compromise on its own.

## Open decisions

- **Hardware token** (YubiKey / HSM) versus an encrypted file. A token removes the exportable copy of the key, at the cost of complicating automated signing and creating a single physical point of failure that demands a backup plan of its own.
- **Anchoring in a public log** Certificate Transparency style (Phase 6): it would allow detecting unauthorized issuances even with a compromised key.
- **Key separation**: an offline root key that only signs intermediate issuing keys, instead of signing credentials directly.
