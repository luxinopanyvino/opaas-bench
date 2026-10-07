# Root key custody — onpremIALabs

The root Ed25519 private key is the system's only secret. Every credential is signed with it; with the public one, published in the DID document, anyone verifies a credential **without depending on onpremIALabs infrastructure**. If the key is lost, no new credentials can be issued and nothing can be re-certified; if it leaks, anyone can issue certificates that look legitimate.

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

## Open decisions

- **Hardware token** (YubiKey / HSM) versus an encrypted file. A token removes the exportable copy of the key, at the cost of complicating automated signing and creating a single physical point of failure that demands a backup plan of its own.
- **Anchoring in a public log** Certificate Transparency style (Phase 6): it would allow detecting unauthorized issuances even with a compromised key.
- **Key separation**: an offline root key that only signs intermediate issuing keys, instead of signing credentials directly.
