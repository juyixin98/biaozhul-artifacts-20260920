/**
 * Proof generation / verification wrapper around snarkjs Groth16.
 *
 * Witness construction runs with sanityCheck = true: the circom WASM runtime
 * then asserts *every* R1CS constraint while solving, so any statement the
 * circuit does not actually hold (bad age range, wrong Merkle path, mismatched
 * nullifier, ...) throws here instead of producing a proof — the prover never
 * falls back to hash matching or any non-ZK shortcut.
 */
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import * as snarkjs from "snarkjs";
import { WitnessCalculatorBuilder } from "circom_runtime";
import { readFileSync } from "node:fs";

export const BUILD_DIR = "build";
export const WASM_PATH = path.join(BUILD_DIR, "eligibility_js", "eligibility.wasm");
export const ZKEY_PATH = path.join(BUILD_DIR, "eligibility.zkey");
export const VKEY_PATH = path.join(BUILD_DIR, "verification_key.json");

/** Circuit private + public inputs, in the exact signal names of eligibility.circom. */
export interface CircuitInputs {
  // private
  secret: bigint;
  age: bigint;
  pathElements: bigint[];
  pathIndices: bigint[];
  // public
  root: bigint;
  eventId: bigint;
  nullifier: bigint;
}

export interface Groth16Proof {
  proof: object;
  publicSignals: string[];
}

/**
 * Compute the binary witness (.wtns) with R1CS sanity assertions enabled.
 * Throws if any circuit constraint is violated.
 */
export async function computeWitness(inputs: CircuitInputs): Promise<Buffer> {
  const wasm = readFileSync(WASM_PATH);
  const wc = await WitnessCalculatorBuilder(wasm, { sanityCheck: true });
  // calculateWTNSBin(input, sanityCheck): second arg also flips init(1) in wasm.
  const wtns: Uint8Array = await wc.calculateWTNSBin({
    secret: inputs.secret,
    age: inputs.age,
    pathElements: inputs.pathElements,
    pathIndices: inputs.pathIndices,
    root: inputs.root,
    eventId: inputs.eventId,
    nullifier: inputs.nullifier,
  }, true);
  return Buffer.from(wtns);
}

/** Build a witness and run the real Groth16 prover against the phase-2 zkey. */
export async function generateProof(inputs: CircuitInputs): Promise<Groth16Proof> {
  const wtns = await computeWitness(inputs);

  // snarkjs groth16 prove reads wtns/zkey from files; stage the witness in a temp dir.
  const dir = await mkdtemp(path.join(tmpdir(), "anon-elig-"));
  const wtnsPath = path.join(dir, "witness.wtns");
  try {
    await writeFile(wtnsPath, wtns);
    const { proof, publicSignals } = await snarkjs.groth16.prove(ZKEY_PATH, wtnsPath);
    return { proof, publicSignals: publicSignals.map(String) };
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/**
 * Verify a Groth16 proof against the exported verification key.
 * `publicSignals` must be in circuit declaration order: [root, eventId, nullifier].
 */
export async function verifyProof(
  vkey: object,
  publicSignals: (string | bigint)[],
  proof: object,
): Promise<boolean> {
  return snarkjs.groth16.verify(vkey, publicSignals.map(String), proof);
}

export function loadVerificationKey(): object {
  return JSON.parse(readFileSync(VKEY_PATH, "utf8"));
}
