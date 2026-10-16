/* Runs the browser verifier (src/opaas/web/assets/verify.js) under Node on
 * the fixtures tests/test_verify_parity.py writes, and prints each verdict as
 * JSON. Node 20+ exposes the same WebCrypto (Ed25519 included) a browser does.
 *
 *   node verify_parity.cjs <verify.js> <fixtures.json>
 */
const fs = require("fs");
const path = require("path");

async function main() {
  const [verifyPath, fixturesPath] = process.argv.slice(2);
  const { verifyCredential } = require(path.resolve(verifyPath));
  const fixtures = JSON.parse(fs.readFileSync(fixturesPath, "utf-8"));
  const out = {};
  for (const fixture of fixtures) {
    const reportBytes = fixture.reportB64 ? Buffer.from(fixture.reportB64, "base64") : null;
    const { checks } = await verifyCredential(fixture.credential, fixture.didDocument, reportBytes, {
      now: fixture.now,
      revocations: fixture.revocations,
    });
    out[fixture.name] = checks.map((check) => [check.name, check.ok]);
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
