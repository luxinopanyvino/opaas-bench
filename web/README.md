# web/ — Publication

**Implemented.** The generator lives in [`src/opaas/web/`](../src/opaas/web) and is invoked with `opaas publish`.

```bash
opaas publish --report runs/20260806-001/report.json --credential credential.json --did-document keys/did.json --out site
```

It produces a **static site**: no server, no database, no API.

```
site/
├── index.html                    public certificate registry
├── .well-known/did.json          issuer public key
├── assets/{opaas.css,verify.js}
└── cert/<id>/
    ├── index.html                certificate page
    ├── report.json               report; its SHA-256 is the certified hash
    ├── credential.json           signed credential
    └── badge.svg                 embeddable badge
```

## Why static

Verification cannot depend on the infrastructure of whoever issues the certificate. A static site can be copied to any hosting, audited with a `git diff`, and has no backend that could lie.

The page verifies the signature **in the browser**, with the credential and the DID document embedded in the HTML itself: it works without calling onpremIALabs and without a connection to the issuer. It needs a secure context (HTTPS or localhost) because it uses WebCrypto.

## What is missing

- **Deploying** is a separate step: `rsync`, GitHub Pages or whatever you use. The tool generates, it does not upload.
- Resolving `onpremialabs:cert:<id>` → `/cert/<id>` requires configuring the hosting.
- Anchoring in a public Certificate Transparency-style log is still pending (Phase 6).
