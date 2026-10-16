# anchors/ — Bundled trust anchors

`did:web` documents (`did.json`) of issuers the tool trusts by default, so that `opaas verify` works without a network.

**Empty on purpose.** The DID document for `did:web:onpremialabs.com` will go here once the production root key exists and is published at `https://onpremialabs.com/.well-known/did.json`. Bundling a test key before then would be worse than having none: it would make `verify` accept credentials signed with a key that is not the production one.

In the meantime, `verify` resolves the key in this order:

1. `--key <file.pub.pem>` or `--did-document <did.json>`
2. anchors installed by the user in `~/.opaas/anchors/` (or `$OPAAS_ANCHOR_DIR`)
3. this directory

If it finds none, it fails saying exactly what is missing. It never uses the key that comes inside the credential itself.

An anchor is a root (D65): every key its document lists can sign directly, and can delegate to an issuing key. A credential signed by an issuing key verifies only through a delegation one of those keys signed.

Revocation lists signed by a root go in a `revocations/` directory beside the anchors (`~/.opaas/anchors/revocations/`, or `revocations/` here). `opaas trust --add revocations.json` installs one after it verifies. Like the anchor, none ships until the production root exists.
