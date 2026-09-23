import { buildPoseidon } from "circomlibjs";

// Poseidon 单例: buildPoseidon 是异步初始化, 全局复用同一个实例
let poseidonPromise: Promise<any> | null = null;

export async function getPoseidon(): Promise<any> {
  if (!poseidonPromise) {
    poseidonPromise = buildPoseidon();
  }
  return poseidonPromise;
}

/**
 * 将输入转为 BN128 域元素 (bigint)。
 * 只接受非负整数的十进制字符串 / number / bigint;
 * 活动 ID 等业务标识必须是纯数字 (如 "2026092201"), 否则抛出明确错误。
 */
export function toField(x: bigint | number | string): bigint {
  if (typeof x === "string" && !/^\d+$/.test(x)) {
    throw new Error(
      `域元素必须是纯数字十进制字符串, 收到: ${JSON.stringify(x)}`
    );
  }
  return BigInt(x);
}

/**
 * 计算 Poseidon 哈希, 返回 BN128 域内十进制字符串。
 * 与电路中 circomlib 的 Poseidon(n) 模板使用相同参数与域。
 */
export async function poseidonHash(
  inputs: Array<bigint | number | string>
): Promise<string> {
  const poseidon = await getPoseidon();
  const hash = poseidon(inputs.map(toField));
  return poseidon.F.toString(hash);
}
