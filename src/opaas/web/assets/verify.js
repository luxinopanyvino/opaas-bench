/* Credential verification in the browser.
 *
 * Everything happens client-side: the page never asks a server whether the
 * certificate is good. If it did, verifying would mean trusting the issuer,
 * which is exactly what the signature exists to avoid.
 *
 * It reimplements the same algorithm as `opaas verify`: JCS
 * canonicalization (RFC 8785), SHA-256 of the proof configuration and of the
 * document, and Ed25519 over the concatenation. Since D65 it also walks the
 * same chain: the root comes from the published DID document, never from the
 * credential; a credential signed by an issuing key verifies only through a
 * delegation the root signed, whose window covers the proof, for a key no
 * revocation list withdraws. The check names and their order are the ones
 * `credential/vc.py` produces, and tests/test_verify_parity.py pins that both
 * verifiers reach the same verdict on the same credentials.
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
const PROOF_TYPE = "DataIntegrityProof";
const CRYPTOSUITE = "eddsa-jcs-2022";
const DELEGATION_PURPOSE = "capabilityDelegation";
const DELEGATION_FIELD = "issuerDelegation";
const DELEGATION_TYPE = "OpaasKeyDelegation";

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
  if (typeof multibase !== "string" || !multibase.startsWith("z")) {
    throw new Error("expected base58btc multibase");
  }
  const raw = b58decode(multibase.slice(1));
  if (raw[0] !== 0xed || raw[1] !== 0x01) throw new Error("the prefix is not ed25519-pub");
  return raw.slice(2);
}

function parseTime(value) {
  if (typeof value !== "string") return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function isoDay(moment) {
  return moment ? moment.toISOString().slice(0, 10) : "an unreadable date";
}

/* The same proof check as `proof.check_proof`: suite, purpose, then Ed25519
 * over H(proof config) || H(document without proof). */
async function checkProof(document, rawKey, purpose) {
  const proof = document.proof;
  if (!proof || typeof proof !== "object") return [false, "no proof: it is not signed"];
  if (proof.type !== PROOF_TYPE || proof.cryptosuite !== CRYPTOSUITE) {
    return [false, `${proof.type} / ${proof.cryptosuite}`];
  }
  if (purpose && proof.proofPurpose !== purpose) {
    return [false, `signed for ${proof.proofPurpose}, not ${purpose}`];
  }
  try {
    const key = await crypto.subtle.importKey("raw", rawKey, { name: "Ed25519" }, false, [
      "verify",
    ]);
    const unsecured = { ...document };
    delete unsecured.proof;
    const proofConfig = { ...proof, "@context": document["@context"] };
    delete proofConfig.proofValue;

    const signingInput = new Uint8Array(64);
    signingInput.set(await sha256(utf8(canonicalize(proofConfig))), 0);
    signingInput.set(await sha256(utf8(canonicalize(unsecured))), 32);

    const value = String(proof.proofValue || "");
    if (!value.startsWith("z")) return [false, "proofValue is not base58btc multibase"];
    const valid = await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      b58decode(value.slice(1)),
      signingInput,
    );
    return valid
      ? [true, `valid Ed25519 (${proof.verificationMethod})`]
      : [false, "the signature does NOT match this key or the document was altered"];
  } catch (error) {
    return [false, `could not verify: ${error.message}`];
  }
}

/* The root: every key the published DID document lists. */
function rootKeys(didDocument) {
  const keys = [];
  for (const method of didDocument.verificationMethod || []) {
    if (method.publicKeyMultibase) {
      keys.push({ id: method.id, raw: publicKeyFromMultibase(method.publicKeyMultibase) });
    }
  }
  return keys;
}

function exactKey(keys, id) {
  return keys.find((key) => key.id === id) || null;
}

function delegationProblem(delegation) {
  if (!delegation || typeof delegation !== "object" || Array.isArray(delegation)) {
    return "the delegation is not a JSON object";
  }
  if (!(delegation.type || []).includes(DELEGATION_TYPE)) {
    return `type is not ${DELEGATION_TYPE}`;
  }
  if (DELEGATION_FIELD in delegation || DELEGATION_FIELD in (delegation.delegate || {})) {
    return "it nests another delegation; depth is fixed at root → issuing key → credential";
  }
  const delegate = delegation.delegate;
  if (!delegate || typeof delegate !== "object" || !delegate.id) {
    return "it names no delegated key id";
  }
  if (typeof delegate.publicKeyMultibase !== "string") {
    return "it carries no publicKeyMultibase for the delegated key";
  }
  if (typeof delegation.issuer !== "string") return "it names no issuer";
  if (!parseTime(delegation.validFrom) || !parseTime(delegation.validUntil)) {
    return "unreadable validity window";
  }
  return null;
}

async function checkRevocation(lists, keys, issuer, candidates, add, warnings) {
  if (!lists.length) {
    add("revocation", true, `no revocation list held for ${issuer}: a revoked key would go undetected`);
    warnings.push(`No revocation list for ${issuer} was published with this page, so revocation was not checked.`);
    return true;
  }
  const revoked = new Map();
  for (const listed of lists) {
    const method = (listed.proof || {}).verificationMethod;
    const key = exactKey(keys, method);
    if (listed.issuer !== issuer || !key) {
      add("revocation", false, `a revocation list (updated ${listed.updated}) is not signed by a key of the anchor`);
      return false;
    }
    const [ok, detail] = await checkProof(listed, key.raw, DELEGATION_PURPOSE);
    if (!ok) {
      add("revocation", false, `the revocation list updated ${listed.updated} does not verify: ${detail}`);
      return false;
    }
    for (const entry of listed.revoked || []) {
      if (entry && typeof entry.publicKeyMultibase === "string") {
        try {
          revoked.set(hex(publicKeyFromMultibase(entry.publicKeyMultibase)), entry);
        } catch (error) {
          /* An entry that names no readable key cannot match one. */
        }
      }
    }
  }
  for (const candidate of candidates) {
    const entry = revoked.get(hex(candidate));
    if (entry) {
      add("revocation", false, `${entry.id} was REVOKED on ${entry.revokedAt} (${entry.reason || "no reason given"})`);
      return false;
    }
  }
  add("revocation", true, `not revoked (list updated ${lists.map((l) => l.updated).join(", ")})`);
  return true;
}

/* From the root to the raw key that signed the credential, or null after a
 * failed step, which ends the walk (`vc._walk`). */
async function walk(credential, proof, keys, didDocument, lists, add, warnings) {
  const issuer = credential.issuer;
  const anchorOk = didDocument.id === issuer && keys.length > 0;
  add(
    "anchor",
    anchorOk,
    anchorOk
      ? `${didDocument.id} (published DID document)`
      : `the anchor is ${didDocument.id}, the credential names ${issuer}`,
  );
  if (!anchorOk) return null;

  const delegation = credential[DELEGATION_FIELD];
  if (delegation === undefined || delegation === null) {
    const key = exactKey(keys, proof.verificationMethod) || keys[0];
    if (lists.length && !(await checkRevocation(lists, keys, issuer, [key.raw], add, warnings))) {
      return null;
    }
    return key.raw;
  }

  const problem = delegationProblem(delegation);
  if (problem) {
    add("delegation", false, `malformed delegation: ${problem}`);
    return null;
  }
  const delegate = delegation.delegate;
  let delegatedKey;
  try {
    delegatedKey = publicKeyFromMultibase(delegate.publicKeyMultibase);
  } catch (error) {
    add("delegation", false, `unreadable delegated key: ${error.message}`);
    return null;
  }
  if (delegation.issuer !== issuer) {
    add("delegation", false, `the delegation is from ${delegation.issuer}, the credential names ${issuer}`);
    return null;
  }
  const rootMethod = (delegation.proof || {}).verificationMethod;
  const rootKey = exactKey(keys, rootMethod);
  if (!rootKey) {
    add("delegation", false, `signed by ${rootMethod}, which is not a key of the anchor`);
    return null;
  }
  const [delegationOk, delegationDetail] = await checkProof(
    delegation,
    rootKey.raw,
    DELEGATION_PURPOSE,
  );
  add("delegation", delegationOk, `${delegate.id} delegated by ${rootMethod}: ${delegationDetail}`);
  if (!delegationOk) return null;

  const created = parseTime(proof.created);
  const start = parseTime(delegation.validFrom);
  const end = parseTime(delegation.validUntil);
  const covered = created !== null && created >= start && created < end;
  add(
    "delegation-validity",
    covered,
    covered
      ? `${proof.created} is within ${delegation.validFrom} → ${delegation.validUntil}`
      : `${proof.created} is outside ${delegation.validFrom} → ${delegation.validUntil}`,
  );
  if (!covered) return null;

  if (!(await checkRevocation(lists, keys, issuer, [delegatedKey, rootKey.raw], add, warnings))) {
    return null;
  }
  const named = proof.verificationMethod === delegate.id;
  add(
    "delegated-key",
    named,
    named
      ? `signed by ${proof.verificationMethod}, the key the delegation names`
      : `signed by ${proof.verificationMethod}, but the delegation names ${delegate.id}`,
  );
  return named ? delegatedKey : null;
}

/* Returns {checks, warnings}. `options.now` and `options.revocations` exist
 * so the same fixtures can be replayed here and in Python. */
async function verifyCredential(credential, didDocument, reportBytes, options = {}) {
  const checks = [];
  const warnings = [];
  const add = (name, ok, detail) => checks.push({ name, ok, detail });
  const now = options.now ? new Date(options.now) : new Date();
  const lists = options.revocations || [];

  const types = credential.type || [];
  const isEvidence = types.includes("OpaasEvidence");
  const isSystem = types.includes("OpaasSystemCertificate");
  add(
    "structure",
    types.includes("VerifiableCredential") &&
      (types.includes("OpaasModelCertificate") || isEvidence || isSystem),
    types.join(", "),
  );

  const proof = credential.proof;
  if (!proof || typeof proof !== "object") {
    add("proof", false, "the credential is not signed");
    return { checks, warnings };
  }
  const suiteOk = proof.type === PROOF_TYPE && proof.cryptosuite === CRYPTOSUITE;
  add("cryptosuite", suiteOk, `${proof.type} / ${proof.cryptosuite}`);
  if (!suiteOk) return { checks, warnings };

  let keys;
  try {
    keys = rootKeys(didDocument);
  } catch (error) {
    add("anchor", false, `unreadable DID document: ${error.message}`);
    return { checks, warnings };
  }

  /* The key comes from the published DID document, never from the credential:
   * a credential that carries its own key verifies itself and proves nothing. */
  const signingKey = await walk(credential, proof, keys, didDocument, lists, add, warnings);
  if (!signingKey) return { checks, warnings };

  const [signatureOk, signatureDetail] = await checkProof(credential, signingKey, null);
  add("signature", signatureOk, signatureDetail);

  const issued = parseTime(credential.issuanceDate);
  const expires = parseTime(credential.expirationDate);
  const current = issued !== null && expires !== null && now >= issued && now < expires;
  add(
    "validity",
    current,
    current
      ? `valid until ${isoDay(expires)}`
      : issued && now < issued
        ? `not valid yet (issued on ${isoDay(issued)})`
        : `EXPIRED on ${isoDay(expires)}: requires re-certification`,
  );

  if (reportBytes) {
    const subject = credential.credentialSubject || {};
    const declared =
      subject.evalRun?.reportHash ?? subject.system?.reportHash ?? subject.evidence?.documentHash;
    const actual = "sha256:" + hex(await sha256(reportBytes));
    add(
      "report",
      Boolean(declared) && actual === declared,
      actual === declared
        ? `the report on this page is the certified one (${declared})`
        : `the report does NOT match: ${actual}`,
    );
  }

  return { checks, warnings };
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
    const revocationsNode = document.getElementById("revocations-data");
    const revocations = revocationsNode ? JSON.parse(revocationsNode.textContent) : [];

    const { checks, warnings } = await verifyCredential(credential, didDocument, reportBytes, {
      revocations,
    });
    const valid = checks.every((check) => check.ok);
    const escape = (text) =>
      String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
    panel.innerHTML =
      `<p class="${valid ? "ok" : "fail"}">${
        valid ? "Credential valid." : "Credential NOT valid."
      }</p><ul>` +
      checks
        .map(
          (check) =>
            `<li class="${check.ok ? "ok" : "fail"}"><strong>${check.name}</strong>: ${escape(
              check.detail,
            )}</li>`,
        )
        .join("") +
      "</ul>" +
      warnings.map((warning) => `<p class="note">${escape(warning)}</p>`).join("") +
      '<p class="note">Verified entirely in your browser, without querying onpremIALabs.</p>';
  } catch (error) {
    panel.innerHTML = `<p class="fail">Error while verifying: ${error.message}</p>`;
  }
  button.disabled = false;
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("verify");
    if (button) button.addEventListener("click", run);
  });
}

/* Under Node, for tests/test_verify_parity.py. A browser has no `module`. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { canonicalize, verifyCredential };
}
