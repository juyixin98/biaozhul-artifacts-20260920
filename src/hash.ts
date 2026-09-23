/**
 * Field-element and Poseidon helpers shared by the off-chain code.
 *
 * circomlibjs's `poseidon` uses exactly the same Poseidon constants and
 * parameter selection (rate / rounds / S-box by input length) as circomlib's
 * `Poseidon(n)` circuit component, so hashes computed here satisfy the
 * constraints of the in-circuit Poseidon(2).
 */
import { webcrypto } from "node:crypto";
import { buildPoseidon } from "circomlibjs";

export type Poseidon = Awaited<ReturnType<typeof buildPoseidon>>;

let poseidonPromise: Promise<Poseidon> | null = null;

/** Singleton Poseidon instance (initialising twice also works, just slower). */
export function poseidonInstance(): Promise<Poseidon> {
  const p = poseidonPromise ?? buildPoseidon();
  poseidonPromise = p;
  return p;
}
/** Poseidon hash over the inputs, returned as a native bigint. */
export async function poseidon(inputs: (bigint | number | string)[]): Promise<bigint> {
  const p = await poseidonInstance();
  return p.F.toObject(p(inputs.map((x) => BigInt(x))));
}

/** Leaf commitment: Poseidon(secret, age). */
export function leafHash(secret: bigint, age: bigint): Promise<bigint> {
  return poseidon([secret, age]);
}

/** Nullifier: Poseidon(secret, eventId) — domain-separated by eventId. */
export function nullifierHash(secret: bigint, eventId: bigint): Promise<bigint> {
  return poseidon([secret, eventId]);
}

/**
 * Pick a uniformly random secret valid as a circuit field input
 * (< BN254 scalar field modulus). 250 bits leaves ample headroom below p.
 */
export function randomSecret(): bigint {
  const bytes = new Uint8Array(32);
  webcrypto.getRandomValues(bytes);
  // Clear the top 6 bits -> 250-bit value, always < p (~254 bits).
  bytes[0] &= 0x03;
  let out = 0n;
  for (const b of bytes) out = (out << 8n) | BigInt(b);
  return out;
}

/** Convert an event identifier (number or numeric string) to a field element. */
export function eventIdToField(eventId: number | bigint | string): bigint {
  const v = BigInt(eventId);
  if (v < 0n) throw new Error("eventId must be non-negative");
  return v;
}
