import * as snarkjs from "snarkjs";

export interface Groth16Proof {
  pi_a: string[];
  pi_b: string[][];
  pi_c: string[];
  protocol: string;
  curve: string;
}

export interface ProofBundle {
  proof: Groth16Proof;
  publicSignals: string[]; // [root, activityId, nullifier]
}

/**
 * 真实执行 Groth16 证明生成: 见证计算 (wasm) + 证明 (zkey)。
 * 若见证不满足电路约束 (如年龄越界、Merkle 路径错误), snarkjs 会抛错。
 */
export async function generateProof(
  circuitInput: Record<string, unknown>,
  wasmPath: string,
  zkeyPath: string
): Promise<ProofBundle> {
  const { proof, publicSignals } = await snarkjs.groth16.fullProve(
    circuitInput,
    wasmPath,
    zkeyPath
  );
  return { proof, publicSignals };
}
