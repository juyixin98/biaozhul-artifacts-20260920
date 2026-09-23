/**
 * High-level user / event flow:
 *
 *   User (prover)                    Verifier / event backend
 *   -----------                      -------------------------
 *   secret, age
 *   leaf = H(secret, age)  ----->    registered in Merkle tree (issuance)
 *                                    publishes root
 *   nullifier = H(secret,eventId)
 *   proves: 18<=age<=120,
 *           leaf under root,
 *           nullifier = H(secret,eventId)
 *                          ----->   verifies zk proof + checks nullifier
 *                                   not seen before for this eventId
 */
import { leafHash, nullifierHash, randomSecret, eventIdToField } from "./hash.js";
import { PoseidonMerkleTree, TREE_DEPTH } from "./tree.js";
import {
  generateProof,
  verifyProof,
  loadVerificationKey,
  type CircuitInputs,
  type Groth16Proof,
} from "./prover.js";

export const MIN_AGE = 18;
export const MAX_AGE = 120;

export interface Identity {
  secret: bigint;
  age: bigint;
  leaf: bigint;
}

export interface RegisteredUser {
  identity: Identity;
  leafIndex: number;
}

/**
 * Create a user identity and commit its leaf into the tree at leafIndex.
 * No Merkle proof is returned: the tree may gain more leaves afterwards, so
 * callers fetch a fresh proof (against the current root) at prove time.
 */
export async function registerUser(
  tree: PoseidonMerkleTree,
  leafIndex: number,
  age: number | bigint,
  secret?: bigint,
): Promise<RegisteredUser> {
  const ageN = BigInt(age);
  if (ageN < BigInt(MIN_AGE) || ageN > BigInt(MAX_AGE)) {
    throw new Error(`age ${ageN} is outside the circuit-enforced range [${MIN_AGE}, ${MAX_AGE}] (off-chain pre-check)`);
  }
  const s = secret ?? randomSecret();
  const leaf = await leafHash(s, ageN);
  tree.setLeaf(leafIndex, leaf);
  return { identity: { secret: s, age: ageN, leaf }, leafIndex };
}

export interface ProveOptions {
  eventId: number | bigint | string;
  root?: bigint;
}

/**
 * Assemble circuit inputs for one event and produce a real Groth16 proof.
 * The proof/public signals depend on the (possibly fresh) tree root, so the
 * Merkle proof is read from the tree at call time.
 */
export async function proveEligibility(
  user: RegisteredUser,
  tree: PoseidonMerkleTree,
  opts: ProveOptions,
): Promise<{ result: Groth16Proof; root: bigint; nullifier: bigint; eventId: bigint }> {
  const eventId = eventIdToField(opts.eventId);
  const root = opts.root ?? await tree.root;
  const nullifier = await nullifierHash(user.identity.secret, eventId);
  const proof = await tree.getProof(user.leafIndex);

  const inputs: CircuitInputs = {
    secret: user.identity.secret,
    age: user.identity.age,
    pathElements: proof.pathElements,
    pathIndices: proof.pathIndices,
    root,
    eventId,
    nullifier,
  };
  const result = await generateProof(inputs);
  return { result, root, nullifier, eventId };
}

/**
 * Event-side verifier with replay protection: a nullifier is accepted at most
 * once per eventId. Cross-event reuse is fine because nullifiers are
 * domain-separated by eventId inside the circuit.
 */
export class EventVerifier {
  private readonly used = new Map<bigint, Set<string>>();
  private readonly vkey: object;

  constructor(vkey?: object) {
    this.vkey = vkey ?? loadVerificationKey();
  }

  /**
   * @param expectedRoot root (or one of recent roots) the event accepts
   * @returns nullifier on success; throws on cryptographic or replay failure
   */
  async verify(
    proofBundle: Groth16Proof,
    params: { eventId: bigint; expectedRoot: bigint; nullifier: bigint },
  ): Promise<bigint> {
    const { eventId, expectedRoot, nullifier } = params;
    const [rootP, eventP, nullP] = proofBundle.publicSignals;
    if (BigInt(rootP) !== BigInt(expectedRoot)) {
      throw new Error("rejected: public root does not match the accepted tree root");
    }
    if (BigInt(eventP) !== BigInt(eventId)) {
      throw new Error("rejected: public eventId mismatch");
    }
    if (BigInt(nullP) !== BigInt(nullifier)) {
      throw new Error("rejected: public nullifier mismatch");
    }

    const ok = await verifyProof(this.vkey, proofBundle.publicSignals, proofBundle.proof);
    if (!ok) throw new Error("rejected: Groth16 proof verification failed");

    let set = this.used.get(eventId);
    if (!set) {
      set = new Set();
      this.used.set(eventId, set);
    }
    const key = nullifier.toString();
    if (set.has(key)) {
      throw new Error(`rejected: nullifier ${key} already spent in event ${eventId}`);
    }
    set.add(key);
    return nullifier;
  }

  spent(eventId: bigint, nullifier: bigint): boolean {
    return this.used.get(eventId)?.has(nullifier.toString()) ?? false;
  }
}

export { TREE_DEPTH };
