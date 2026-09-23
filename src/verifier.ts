import * as fs from "fs";
import * as snarkjs from "snarkjs";
import { Groth16Proof } from "./prover";

/**
 * nullifier 注册表: 记录已使用的 nullifier, 防止同一活动重复提交。
 * 生产环境应放在链上合约或中心化数据库; 本地样例用 JSON 文件持久化。
 */
export class NullifierRegistry {
  private seen = new Set<string>();
  constructor(private filePath?: string) {
    if (filePath && fs.existsSync(filePath)) {
      const data = JSON.parse(fs.readFileSync(filePath, "utf8"));
      for (const n of data.nullifiers ?? []) this.seen.add(String(n));
    }
  }

  has(nullifier: string): boolean {
    return this.seen.has(nullifier);
  }

  /** 登记新的 nullifier; 已存在则返回 false (拒绝重放) */
  register(nullifier: string): boolean {
    if (this.seen.has(nullifier)) return false;
    this.seen.add(nullifier);
    if (this.filePath) {
      fs.writeFileSync(
        this.filePath,
        JSON.stringify({ nullifiers: [...this.seen] }, null, 2)
      );
    }
    return true;
  }
}

/** 纯密码学验证: 校验 Groth16 证明与公开信号是否匹配 */
export async function verifyProof(
  vkey: unknown,
  publicSignals: string[],
  proof: Groth16Proof
): Promise<boolean> {
  return snarkjs.groth16.verify(vkey, publicSignals, proof);
}

export interface AcceptResult {
  ok: boolean;
  reason: string;
  nullifier?: string;
}

/**
 * 应用层验证器: 密码学验证 + 业务规则
 *  (公开输入必须与当前根/活动一致, nullifier 不得重复)。
 */
export class Verifier {
  constructor(
    private vkey: unknown,
    private registry: NullifierRegistry = new NullifierRegistry()
  ) {}

  async accept(
    proof: Groth16Proof,
    publicSignals: string[],
    expectedRoot: string,
    expectedActivityId: string
  ): Promise<AcceptResult> {
    const [root, activityId, nullifier] = publicSignals;

    if (root !== expectedRoot) {
      return { ok: false, reason: "root mismatch: 证明未绑定当前成员根" };
    }
    if (activityId !== expectedActivityId) {
      return { ok: false, reason: "activityId mismatch: 证明未绑定当前活动" };
    }
    const valid = await verifyProof(this.vkey, publicSignals, proof);
    if (!valid) {
      return { ok: false, reason: "groth16 verify failed: 密码学验证不通过" };
    }
    if (!this.registry.register(nullifier)) {
      return {
        ok: false,
        reason: "nullifier already used: 同一活动不可重复提交",
        nullifier,
      };
    }
    return { ok: true, reason: "accepted", nullifier };
  }
}
