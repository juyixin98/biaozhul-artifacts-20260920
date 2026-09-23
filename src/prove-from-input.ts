/**
 * Prove (and verify) directly from a circuit-input JSON file.
 *
 *   npx tsx src/prove-from-input.ts examples/sample-input.json \
 *       [--out examples/sample-proof.json]
 *
 * The input JSON contains the PRIVATE inputs too (secret, age, path...) — this
 * is a local test utility, never ship real secrets in such a file.
 */
import { readFile, writeFile } from "node:fs/promises";
import { generateProof, loadVerificationKey, verifyProof } from "./prover.js";

function toBigints(v: unknown): bigint[] {
  if (!Array.isArray(v)) throw new Error("expected array of decimal strings");
  return v.map((x) => BigInt(String(x)));
}

async function main() {
  const [inputPath, outFlag, outPath] = process.argv.slice(2);
  if (!inputPath) {
    console.error("usage: tsx src/prove-from-input.ts <input.json> [--out proof.json]");
    process.exit(2);
  }
  const raw = JSON.parse(await readFile(inputPath, "utf8"));
  const inputs = {
    secret: BigInt(raw.secret),
    age: BigInt(raw.age),
    pathElements: toBigints(raw.pathElements),
    pathIndices: toBigints(raw.pathIndices),
    root: BigInt(raw.root),
    eventId: BigInt(raw.eventId),
    nullifier: BigInt(raw.nullifier),
  };

  console.log(`generating Groth16 proof from ${inputPath} (eventId=${inputs.eventId})...`);
  const bundle = await generateProof(inputs);

  const vkey = loadVerificationKey();
  const ok = await verifyProof(vkey, bundle.publicSignals, bundle.proof);
  console.log(`verified against verification_key.json: ${ok}`);
  if (!ok) process.exit(1);

  const out = {
    publicSignals: bundle.publicSignals,
    proof: bundle.proof,
  };
  const target = outFlag === "--out" && outPath ? outPath : "examples/sample-proof.json";
  await writeFile(target, JSON.stringify(out, null, 2) + "\n");
  console.log(`proof written to ${target}`);

  // ffjavascript may leave worker handles cached on this path; the CLI is done.
  process.exit(0);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
