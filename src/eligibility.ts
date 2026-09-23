import { MerkleTree } from "./merkle";
import { poseidonHash } from "./poseidon";

export interface Member {
  age: number;
  secret: string;
}

/** 电路完整输入 (公开 + 私密) */
export interface CircuitInput {
  // 私密
  age: string;
  secret: string;
  pathElements: string[];
  pathIndices: number[];
  // 公开
  root: string;
  activityId: string;
  nullifier: string;
}

/** 私密叶 = Poseidon(age, secret), 与电路 leafHash 一致 */
export async function computeLeaf(member: Member): Promise<string> {
  return poseidonHash([member.age, member.secret]);
}

/** nullifier = Poseidon(secret, activityId), 与电路 nf 一致 */
export async function computeNullifier(
  secret: string,
  activityId: string
): Promise<string> {
  return poseidonHash([secret, activityId]);
}

/** 由成员名单构建深度 8 的 Merkle 树 */
export async function buildTree(members: Member[]): Promise<MerkleTree> {
  const leaves: string[] = [];
  for (const m of members) {
    leaves.push(await computeLeaf(m));
  }
  return MerkleTree.create(8, leaves);
}

/** 为 members[leafIndex] 生成完整电路输入 */
export async function buildCircuitInput(
  members: Member[],
  leafIndex: number,
  activityId: string
): Promise<CircuitInput> {
  if (leafIndex < 0 || leafIndex >= members.length) {
    throw new Error(`prover index out of range: ${leafIndex}`);
  }
  const member = members[leafIndex];
  const tree = await buildTree(members);
  const proof = tree.proof(leafIndex);
  const nullifier = await computeNullifier(member.secret, activityId);
  return {
    age: String(member.age),
    secret: member.secret,
    pathElements: proof.pathElements,
    pathIndices: proof.pathIndices,
    root: tree.root,
    activityId,
    nullifier,
  };
}
