/* Credential verification in the browser.
 *
 * Everything happens client-side: the page never asks a server whether the
 * certificate is good. If it did, verifying would mean trusting the issuer,
 * which is exactly what the signature exists to avoid.
 *
 * It reimplements the same algorithm as `opaas verify`: JCS
 * canonicalization (RFC 8785), SHA-256 of the proof configuration and of the
 * document, and Ed25519 over the concatenation.
 */

/* JCS in JavaScript is almost free: Array.sort() already sorts by UTF-16 code
 * units, and JSON.stringify serializes numbers and strings exactly as the
 * specification requires. */
function canonicalize(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalize).join(",") + "]";
  return (
    "{" +
    Object.keys(value)
      .sort()
      .map((key) => JSON.stringify(key) + ":" + canonicalize(value[key]))
      .join(",") +
    "}"
  );
}

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function b58decode(text) {
  let number = 0n;
  for (const char of text) {
    const index = B58.indexOf(char);
    if (index < 0) throw new Error(`invalid character in base58: ${char}`);
    number = number * 58n + BigInt(index);
  }
  const bytes = [];
  while (number > 0n) {
    bytes.unshift(Number(number & 0xffn));
    number >>= 8n;
  }
  for (const char of text) {
    if (char !== "1") break;
    bytes.unshift(0);
  }
  return new Uint8Array(bytes);
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

function hex(bytes) {
  return Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function utf8(text) {
  return new TextEncoder().encode(text);
}

function base64ToBytes(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function publicKeyFromMultibase(multibase) {
  if (!multibase.startsWith("z")) throw new Error("expected base58btc multibase");
  const raw = b58decode(multibase.slice(1));
  if (raw[0] !== 0xed || raw[1] !== 0x01) throw new Error("the prefix is not ed25519-pub");
  return raw.slice(2);
}

async function verifyCredential(credential, didDocument, reportBytes) {
  const checks = [];
  const add = (name, ok, detail) => checks.push({ name, ok, detail });

  const types = credential.type || [];
  add(
    "structure",
    types.includes("VerifiableCredential") && types.includes("OpasModelCertificate"),
    types.join(", "),
  );

  const proof = credential.proof;
  if (!proof) {
    add("proof", false, "the credential is not signed");
    return checks;
  }
  const suiteOk =
    proof.type === "DataIntegrityProof" && proof.cryptosuite === "eddsa-jcs-2022";
  add("cryptosuite", suiteOk, `${proof.type} / ${proof.cryptosuite}`);
  if (!suiteOk) return checks;

  /* The key comes from the published DID document, never from the credential:
   * a credential that carries its own key verifies itself and proves nothing. */
  const method = (didDocument.verificationMethod || []).find(
    (entry) => entry.id === proof.verificationMethod,
  ) || (didDocument.verificationMethod || [])[0];
  if (!method) {
    add("key", false, "the DID document exposes no key");
    return checks;
  }

  try {
    const rawKey = publicKeyFromMultibase(method.publicKeyMultibase);
    const key = await crypto.subtle.importKey("raw", rawKey, { name: "Ed25519" }, false, [
      "verify",
    ]);

    const unsecured = { ...credential };
    delete unsecured.proof;
    const proofConfig = { ...proof, "@context": credential["@context"] };
    delete proofConfig.proofValue;

    const signingInput = new Uint8Array(64);
    signingInput.set(await sha256(utf8(canonicalize(proofConfig))), 0);
    signingInput.set(await sha256(utf8(canonicalize(unsecured))), 32);

    const signature = b58decode(proof.proofValue.slice(1));
    const valid = await crypto.subtle.verify({ name: "Ed25519" }, key, signature, signingInput);
    add("signature", valid, valid ? `valid Ed25519 (${method.id})` : "the signature does NOT match");
  } catch (error) {
    add("signature", false, `could not verify: ${error.message}`);
    return checks;
  }

  const now = new Date();
  const expires = new Date(credential.expirationDate);
  const issued = new Date(credential.issuanceDate);
  const current = now >= issued && now < expires;
  add(
    "validity",
    current,
    current
      ? `valid until ${expires.toISOString().slice(0, 10)}`
      : `EXPIRED on ${expires.toISOString().slice(0, 10)}: requires re-certification`,
  );

  if (reportBytes) {
    const declared =
      credential.credentialSubject?.evalRun?.reportHash ??
      credential.credentialSubject?.system?.reportHash;
    const actual = "sha256:" + hex(await sha256(reportBytes));
    add(
      "report",
      actual === declared,
      actual === declared
        ? `the report on this page is the certified one (${declared})`
        : `the report does NOT match: ${actual}`,
    );
  }

  return checks;
}

async function run() {
  const panel = document.getElementById("verification");
  const button = document.getElementById("verify");
  button.disabled = true;
  panel.hidden = false;
  panel.innerHTML = '<p class="pending">Verifying in your browser…</p>';

  if (!crypto.subtle || !window.isSecureContext) {
    panel.innerHTML =
      '<p class="fail">This browser does not expose WebCrypto in a secure context. ' +
      "Serve the page over HTTPS or verify with <code>opaas verify</code>.</p>";
    button.disabled = false;
    return;
  }

  try {
    const credential = JSON.parse(document.getElementById("credential-data").textContent);
    const didDocument = JSON.parse(document.getElementById("did-data").textContent);
    const reportNode = document.getElementById("report-data");
    const reportBytes = reportNode ? base64ToBytes(reportNode.textContent.trim()) : null;

    const checks = await verifyCredential(credential, didDocument, reportBytes);
    const valid = checks.every((check) => check.ok);
    panel.innerHTML =
      `<p class="${valid ? "ok" : "fail"}">${
        valid ? "Credential valid." : "Credential NOT valid."
      }</p><ul>` +
      checks
        .map(
          (check) =>
            `<li class="${check.ok ? "ok" : "fail"}"><strong>${check.name}</strong>: ${
              check.detail
            }</li>`,
        )
        .join("") +
      "</ul><p class=\"note\">Verified entirely in your browser, without querying onpremIALabs.</p>";
  } catch (error) {
    panel.innerHTML = `<p class="fail">Error while verifying: ${error.message}</p>`;
  }
  button.disabled = false;
}

document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("verify");
  if (button) button.addEventListener("click", run);
});
