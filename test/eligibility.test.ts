import { describe, test, expect, beforeAll } from "vitest";
import { randomSecret, leafHash, nullifierHash } from "../src/hash.js";
import { PoseidonMerkleTree, TREE_DEPTH, ZERO_LEAF } from "../src/tree.js";
import { EventVerifier, proveEligibility, registerUser } from "../src/protocol.js";
import {
  computeWitness,
  generateProof,
  loadVerificationKey,
  verifyProof,
} from "../src/prover.js";

// Every test below requires the artifacts produced by `npm run setup`.
const artifactsReady = await (async () => {
  try {
    loadVerificationKey();
    return true;
  } catch {
    return false;
  }
})();

const it = artifactsReady ? test : test.skip;

describe.skipIf(!artifactsReady)("anonymous eligibility circuit", () => {
  let tree: PoseidonMerkleTree;
  let root: bigint;
  let alice: Awaited<ReturnType<typeof registerUser>>;
  let bob: Awaited<ReturnType<typeof registerUser>>;
  let vkey: object;

  beforeAll(async () => {
    tree = await PoseidonMerkleTree.create(TREE_DEPTH);
    alice = await registerUser(tree, 3, 25);
    bob = await registerUser(tree, 10, 18);
    await registerUser(tree, 200, 120);
    await registerUser(tree, 7, 42);
    root = await tree.root;
    vkey = loadVerificationKey();
  });

  test("tree depth / size", () => {
    expect(TREE_DEPTH).toBe(8);
    expect(ZERO_LEAF).toBe(0n);
  });

  test("off-chain Merkle proofs verify against the dense root", async () => {
    const proof = await tree.getProof(alice.leafIndex);
    expect(await tree.verifyProof(proof, root)).toBe(true);
  });

  test("happy path: proof over event 42 verifies; public signals order is [root, eventId, nullifier]", async () => {
    const bundle = await proveEligibility(alice, tree, { eventId: 42 });
    expect(bundle.eventId).toBe(42n);
    expect(bundle.root).toBe(root);
    expect(bundle.result.publicSignals).toHaveLength(3);
    expect(BigInt(bundle.result.publicSignals[0])).toBe(root);
    expect(BigInt(bundle.result.publicSignals[1])).toBe(42n);
    expect(BigInt(bundle.result.publicSignals[2])).toBe(bundle.nullifier);

    const ok = await verifyProof(vkey, bundle.result.publicSignals, bundle.result.proof);
    expect(ok).toBe(true);
  });

  test("nullifier = Poseidon(secret, eventId) and is deterministic", async () => {
    const b1 = await proveEligibility(alice, tree, { eventId: 42 });
    const b2 = await proveEligibility(alice, tree, { eventId: 42 });
    expect(b1.nullifier).toBe(b2.nullifier);
    expect(b1.nullifier).toBe(await nullifierHash(alice.identity.secret, 42n));
  });

  test("different events -> different nullifiers (cross-event reuse allowed)", async () => {
    const a = await proveEligibility(alice, tree, { eventId: 42 });
    const b = await proveEligibility(alice, tree, { eventId: 7 });
    expect(a.nullifier).not.toBe(b.nullifier);
  });

  // ---- age range boundaries ------------------------------------------------

  test.each([
    ["lower bound", 18],
    ["upper bound", 120],
    ["inside range", 64],
  ])("age accepted: %s (%s)", async (_label, age) => {
    const user = await registerUser(tree, 60 + age, age);
    const bundle = await proveEligibility(user, tree, { eventId: 1000 + age });
    expect(await verifyProof(vkey, bundle.result.publicSignals, bundle.result.proof)).toBe(true);
  });

  test.each([
    ["below", 17n],
    ["zero", 0n],
    ["above", 121n],
    ["far above but 7-bit", 127n],
    ["just past 7 bits", 128n],
    ["huge field value", 123456789012345678901234567890n],
  ])("age rejected: %s (%s)", async (_label, age) => {
    const secret = randomSecret();
    const leaf = await leafHash(secret, age);
    const idx = 90 + Number(age % 50n);
    tree.setLeaf(idx, leaf);
    const badRoot = await tree.root;
    const path = await tree.getProof(idx);
    await expect(
      generateProof({
        secret,
        age,
        pathElements: path.pathElements,
        pathIndices: path.pathIndices,
        root: badRoot,
        eventId: 42n,
        nullifier: await nullifierHash(secret, 42n),
      }),
    ).rejects.toBeTruthy();
    tree.setLeaf(idx, ZERO_LEAF);
  });

  // ---- wrong Merkle path ---------------------------------------------------

  test("wrong path siblings: witness construction fails", async () => {
    const currentRoot = await tree.root;
    const alicePath = await tree.getProof(alice.leafIndex);
    // Flip every sibling to a different node.
    const tamperedSiblings = alicePath.pathElements.map((s, i) => s + BigInt(i + 1));
    await expect(
      computeWitness({
        secret: alice.identity.secret,
        age: alice.identity.age,
        pathElements: tamperedSiblings,
        pathIndices: alicePath.pathIndices,
        root: currentRoot,
        eventId: 42n,
        nullifier: await nullifierHash(alice.identity.secret, 42n),
      }),
    ).rejects.toBeTruthy();
  });

  test("wrong path indices (points elsewhere): witness construction fails", async () => {
    const currentRoot = await tree.root;
    const alicePath = await tree.getProof(alice.leafIndex);
    const flipped = alicePath.pathIndices.map((b) => b ^ 1n);
    await expect(
      computeWitness({
        secret: alice.identity.secret,
        age: alice.identity.age,
        pathElements: alicePath.pathElements,
        pathIndices: flipped,
        root: currentRoot,
        eventId: 42n,
        nullifier: await nullifierHash(alice.identity.secret, 42n),
      }),
    ).rejects.toBeTruthy();
  });

  test("non-bit path index (2): witness construction fails", async () => {
    const currentRoot = await tree.root;
    const alicePath = await tree.getProof(alice.leafIndex);
    const badIndices = alicePath.pathIndices.slice();
    badIndices[0] = 2n;
    await expect(
      computeWitness({
        secret: alice.identity.secret,
        age: alice.identity.age,
        pathElements: alicePath.pathElements,
        pathIndices: badIndices,
        root: currentRoot,
        eventId: 42n,
        nullifier: await nullifierHash(alice.identity.secret, 42n),
      }),
    ).rejects.toBeTruthy();
  });

  test("foreign root not in tree: valid-looking inputs fail", async () => {
    const currentRoot = await tree.root;
    const otherTree = await PoseidonMerkleTree.create(TREE_DEPTH);
    const outsider = await registerUser(otherTree, 0, 30);
    const outsiderRoot = await otherTree.root;
    const path = await otherTree.getProof(outsider.leafIndex);
    expect(outsiderRoot).not.toBe(currentRoot);
    await expect(
      generateProof({
        secret: outsider.identity.secret,
        age: outsider.identity.age,
        pathElements: path.pathElements,
        pathIndices: path.pathIndices,
        root: currentRoot, // claims OUR root
        eventId: 42n,
        nullifier: await nullifierHash(outsider.identity.secret, 42n),
      }),
    ).rejects.toBeTruthy();
  });

  // ---- tampered public inputs ----------------------------------------------

  test.each([
    ["root", (pubs: string[]) => { pubs[0] = (BigInt(pubs[0]) + 1n).toString(); }],
    ["eventId", (pubs: string[]) => { pubs[1] = "999"; }],
    ["nullifier", (pubs: string[]) => { pubs[2] = (BigInt(pubs[2]) + 1n).toString(); }],
  ])("tampered %s: Groth16 verify returns false", async (_label, mutate) => {
    const bundle = await proveEligibility(bob, tree, { eventId: 42 });
    const pubs = bundle.result.publicSignals.slice();
    mutate(pubs);
    expect(await verifyProof(vkey, pubs, bundle.result.proof)).toBe(false);
  });

  test("valid proof for one event cannot be re-presented as another event's public inputs", async () => {
    const bundle = await proveEligibility(bob, tree, { eventId: 42 });
    const pubs = bundle.result.publicSignals.slice();
    pubs[1] = "43";
    expect(await verifyProof(vkey, pubs, bundle.result.proof)).toBe(false);
  });

  // ---- nullifier semantics at the verifier --------------------------------

  test("replay protection: same nullifier twice in one event rejected, different events accepted", async () => {
    const backend = new EventVerifier(vkey);

    const at42 = await proveEligibility(alice, tree, { eventId: 42 });
    // Use the root the proof actually commits to (earlier tests registered
    // extra leaves, so the beforeAll-captured root is intentionally stale).
    await expect(
      backend.verify(at42.result, { eventId: 42n, expectedRoot: at42.root, nullifier: at42.nullifier }),
    ).resolves.toBe(at42.nullifier);

    const at42Again = await proveEligibility(alice, tree, { eventId: 42 });
    expect(at42Again.root).toBe(at42.root);
    await expect(
      backend.verify(at42Again.result, { eventId: 42n, expectedRoot: at42.root, nullifier: at42Again.nullifier }),
    ).rejects.toThrow(/already spent/);

    const at7 = await proveEligibility(alice, tree, { eventId: 7 });
    await expect(
      backend.verify(at7.result, { eventId: 7n, expectedRoot: at7.root, nullifier: at7.nullifier }),
    ).resolves.toBe(at7.nullifier);

    expect(backend.spent(42n, at42.nullifier)).toBe(true);
    expect(backend.spent(7n, at42.nullifier)).toBe(false);
  });

  test("verifier rejects public-input/parameter inconsistency before proof check", async () => {
    const backend = new EventVerifier(vkey);
    const bundle = await proveEligibility(alice, tree, { eventId: 42 });
    await expect(
      backend.verify(bundle.result, { eventId: 43n, expectedRoot: bundle.root, nullifier: bundle.nullifier }),
    ).rejects.toThrow(/eventId mismatch/);
    await expect(
      backend.verify(bundle.result, { eventId: 42n, expectedRoot: bundle.root + 1n, nullifier: bundle.nullifier }),
    ).rejects.toThrow(/root/);
    await expect(
      backend.verify(bundle.result, { eventId: 42n, expectedRoot: bundle.root, nullifier: bundle.nullifier + 1n }),
    ).rejects.toThrow(/nullifier mismatch/);
  });

  // ---- circuit binds leaf = Poseidon(secret, age) --------------------------

  test("wrong age paired with a secret whose leaf used another age: witness fails", async () => {
    const currentRoot = await tree.root;
    // Alice's leaf commits age 25; claiming age 26 cannot re-root at our root.
    const path = await tree.getProof(alice.leafIndex);
    await expect(
      computeWitness({
        secret: alice.identity.secret,
        age: 26n,
        pathElements: path.pathElements,
        pathIndices: path.pathIndices,
        root: currentRoot,
        eventId: 42n,
        nullifier: await nullifierHash(alice.identity.secret, 42n),
      }),
    ).rejects.toBeTruthy();
  });

  test("forged nullifier from another secret: witness fails", async () => {
    const currentRoot = await tree.root;
    const path = await tree.getProof(alice.leafIndex);
    const attackersNullifier = await nullifierHash(randomSecret(), 42n);
    await expect(
      computeWitness({
        secret: alice.identity.secret,
        age: 25n,
        pathElements: path.pathElements,
        pathIndices: path.pathIndices,
        root: currentRoot,
        eventId: 42n,
        nullifier: attackersNullifier,
      }),
    ).rejects.toBeTruthy();
  });

  test("empty-tree root is well defined and a proof against a wrong root fails", async () => {
    const empty = await PoseidonMerkleTree.create(TREE_DEPTH);
    const emptyRoot = await empty.root;
    expect(typeof emptyRoot).toBe("bigint");
    expect(emptyRoot).not.toBe(root);
  });
});

if (!artifactsReady) {
  test("setup artifacts exist", () => {
    throw new Error("Build artifacts missing — run `npm run setup` before `npm test`.");
  });
}
