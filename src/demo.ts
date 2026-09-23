/**
 * End-to-end local demo of the anonymous-eligibility protocol.
 *
 * Run `npm run setup` first, then `npm run demo`.
 *
 * Scenario:
 *  - An issuer builds a depth-8 Merkle tree of committed leaves
 *    leaf = Poseidon(secret, age).
 *  - Event 42 accepts members of that tree aged 18..120.
 *  - Alice (25) proves anonymously, is accepted, her replay is rejected,
 *    but the SAME proof identity works at a different event (7).
 *  - Underage/over-age users (17/121) cannot even construct a witness.
 *  - A tampered public input fails verification; a wrong Merkle path fails
 *    witness construction.
 */
import { mkdir, writeFile } from "node:fs/promises";
import { randomSecret, leafHash, nullifierHash } from "./hash.js";
import { PoseidonMerkleTree, TREE_DEPTH } from "./tree.js";
import { proveEligibility, EventVerifier, registerUser } from "./protocol.js";
import { generateProof, loadVerificationKey, verifyProof } from "./prover.js";

function hr(title: string) {
  console.log(`\n──────── ${title} ${"─".repeat(Math.max(0, 40 - title.length))}`);
}

async function expectWitnessFailure(label: string, fn: () => Promise<unknown>) {
  try {
    await fn();
    console.log(`  ✗ ${label}: EXPECTED FAILURE BUT PROOF WAS BUILT`);
    process.exitCode = 1;
  } catch (err) {
    const msg = err instanceof Error ? err.message.split("\n")[0] : String(err);
    console.log(`  ✓ ${label}: rejected by circuit — ${msg.slice(0, 90)}`);
  }
}

async function expectVerifyFailure(label: string, fn: () => Promise<unknown>) {
  try {
    await fn();
    console.log(`  ✗ ${label}: EXPECTED REJECTION BUT VERIFIER ACCEPTED`);
    process.exitCode = 1;
  } catch (err) {
    const msg = err instanceof Error ? err.message.split("\n")[0] : String(err);
    console.log(`  ✓ ${label}: ${msg.slice(0, 90)}`);
  }
}

async function main() {
  console.log("Anonymous eligibility — local Groth16 demo (Circom + snarkjs)");
  console.log(`Merkle depth = ${TREE_DEPTH} (${1 << TREE_DEPTH} leaves), age range enforced in-circuit = [18,120]`);

  const tree = await PoseidonMerkleTree.create(TREE_DEPTH);

  // Fixed-ish demo identities: random secrets, fixed ages.
  const alice  = await registerUser(tree, 3, 25);
  const bob    = await registerUser(tree, 10, 18);   // lower bound
  const carol  = await registerUser(tree, 200, 120); // upper bound
  const dave   = await registerUser(tree, 7, 42);
  const root = await tree.root;
  console.log(`\nregistered 4 eligible users; tree root = ${root}`);

  await mkdir("examples", { recursive: true });

  // Emit a reproducible sample circuit-input file (Alice, event 42).
  const aliceProof = await tree.getProof(alice.leafIndex);
  const EVENT_A = 42n;
  const aliceNullifierA = await nullifierHash(alice.identity.secret, EVENT_A);
  const sampleInput = {
    secret: alice.identity.secret.toString(),
    age: alice.identity.age.toString(),
    pathElements: aliceProof.pathElements.map(String),
    pathIndices: aliceProof.pathIndices.map(String),
    root: root.toString(),
    eventId: EVENT_A.toString(),
    nullifier: aliceNullifierA.toString(),
  };
  await writeFile("examples/sample-input.json", JSON.stringify(sampleInput, null, 2) + "\n");

  const verifier = new EventVerifier();

  hr("1. valid proof — Alice (25) joins event 42");
  const bundle = await proveEligibility(alice, tree, { eventId: EVENT_A });
  console.log(`  root      = ${bundle.root}`);
  console.log(`  nullifier = ${bundle.nullifier}  (secret never revealed)`);
  await verifier.verify(bundle.result, { eventId: EVENT_A, expectedRoot: root, nullifier: bundle.nullifier });
  console.log("  ✓ Groth16 proof verified and nullifier recorded for event 42");

  hr("2. replay in the SAME event — rejected");
  const replay = await proveEligibility(alice, tree, { eventId: EVENT_A });
  await expectVerifyFailure("same nullifier twice in event 42", () =>
    verifier.verify(replay.result, { eventId: EVENT_A, expectedRoot: root, nullifier: replay.nullifier }),
  );

  hr("3. same identity, DIFFERENT event 7 — accepted (nullifier is domain-separated)");
  const otherEvent = await proveEligibility(alice, tree, { eventId: 7 });
  console.log(`  event 7 nullifier = ${otherEvent.nullifier}`);
  console.log(`  differs from event-42 nullifier: ${otherEvent.nullifier !== bundle.nullifier}`);
  await verifier.verify(otherEvent.result, { eventId: 7n, expectedRoot: root, nullifier: otherEvent.nullifier });
  console.log("  ✓ accepted at event 7");

  hr("4. boundary ages — 18 and 120 accepted");
  for (const [name, u] of [["Bob(18)", bob], ["Carol(120)", carol]] as const) {
    const b = await proveEligibility(u, tree, { eventId: 99 });
    await verifier.verify(b.result, { eventId: 99n, expectedRoot: root, nullifier: b.nullifier });
    console.log(`  ✓ ${name} verified at event 99`);
  }

  hr("5. age out of range — witness construction fails (no proof exists)");
  const underSecret = randomSecret();
  const overSecret = randomSecret();
  const underLeaf = await leafHash(underSecret, 17n);
  const overLeaf = await leafHash(overSecret, 121n);
  tree.setLeaf(50, underLeaf);
  tree.setLeaf(51, overLeaf);
  const rootAfterBad = await tree.root;
  const underPath = await tree.getProof(50);
  const overPath = await tree.getProof(51);
  await expectWitnessFailure("age = 17", async () =>
    generateProof({
      secret: underSecret, age: 17n,
      pathElements: underPath.pathElements, pathIndices: underPath.pathIndices,
      root: rootAfterBad, eventId: 42n, nullifier: await nullifierHash(underSecret, 42n),
    }),
  );
  await expectWitnessFailure("age = 121", async () =>
    generateProof({
      secret: overSecret, age: 121n,
      pathElements: overPath.pathElements, pathIndices: overPath.pathIndices,
      root: rootAfterBad, eventId: 42n, nullifier: await nullifierHash(overSecret, 42n),
    }),
  );
  // restore tree state for the remaining checks
  tree.setLeaf(50, 0n);
  tree.setLeaf(51, 0n);

  hr("6. wrong Merkle path — witness construction fails");
  const wrongPath = await tree.getProof(dave.leafIndex ^ 1);
  await expectWitnessFailure("valid leaf, sibling path of a different index", () =>
    generateProof({
      secret: alice.identity.secret, age: alice.identity.age,
      pathElements: wrongPath.pathElements, pathIndices: wrongPath.pathIndices,
      root, eventId: 42n, nullifier: aliceNullifierA,
    }),
  );

  hr("7. tampered public inputs — verifier rejects");
  const good = await proveEligibility(dave, tree, { eventId: 42 });
  const vkey = loadVerificationKey();
  const [pRoot, pEvent, pNull] = good.result.publicSignals;

  await expectVerifyFailure("root swapped to another value", async () => {
    const tampered = [BigInt(pRoot) + 1n, pEvent, pNull].map(String);
    const ok = await verifyProof(vkey, tampered, good.result.proof);
    if (ok) throw new Error("verifier accepted wrong root");
    throw new Error("Groth16 verification failed for tampered root");
  });
  await expectVerifyFailure("eventId swapped (42 -> 43)", async () => {
    const tampered = [pRoot, "43", pNull];
    const ok = await verifyProof(vkey, tampered, good.result.proof);
    if (ok) throw new Error("verifier accepted wrong eventId");
    throw new Error("Groth16 verification failed for tampered eventId");
  });
  await expectVerifyFailure("nullifier swapped", async () => {
    const tampered = [pRoot, pEvent, BigInt(pNull) + 1n].map(String);
    const ok = await verifyProof(vkey, tampered, good.result.proof);
    if (ok) throw new Error("verifier accepted wrong nullifier");
    throw new Error("Groth16 verification failed for tampered nullifier");
  });

  hr("8. forged nullifier for a different secret — witness construction fails");
  await expectWitnessFailure("nullifier claims a secret that does not own the leaf", async () =>
    generateProof({
      secret: alice.identity.secret, age: alice.identity.age,
      pathElements: aliceProof.pathElements, pathIndices: aliceProof.pathIndices,
      root, eventId: EVENT_A,
      nullifier: await nullifierHash(overSecret, EVENT_A), // unrelated secret
    }),
  );

  console.log("\nDemo finished. Sample circuit inputs written to examples/sample-input.json");
  if (process.exitCode) {
    console.error("\nSOME CHECKS FAILED");
    process.exit(1);
  }
  console.log("ALL CHECKS PASSED");
  // ffjavascript may keep worker handles open after the last await; the demo is done.
  process.exit(0);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
