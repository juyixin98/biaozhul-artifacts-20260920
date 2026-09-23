import { poseidonHash } from "./poseidon";

export const TREE_DEPTH = 8;
export const TREE_CAPACITY = 2 ** TREE_DEPTH; // 256 个叶位

export interface MerkleProof {
  leafIndex: number;
  pathElements: string[];
  pathIndices: number[];
}

/**
 * 深度 8 的 Poseidon Merkle 树。
 * 空子树用零值填充: zero[0] = 0, zero[i] = Poseidon(zero[i-1], zero[i-1]),
 * 与电路中 MerkleRoot 模板的双输入 Poseidon 逐层一致。
 */
export class MerkleTree {
  readonly depth: number;
  private zeros: string[] = [];
  private levels: string[][] = [];

  private constructor(depth: number) {
    this.depth = depth;
  }

  static async create(depth: number, leaves: string[]): Promise<MerkleTree> {
    const capacity = 2 ** depth;
    if (leaves.length > capacity) {
      throw new Error(`too many leaves: ${leaves.length} > ${capacity}`);
    }
    const tree = new MerkleTree(depth);

    tree.zeros[0] = "0";
    for (let i = 1; i <= depth; i++) {
      tree.zeros[i] = await poseidonHash([tree.zeros[i - 1], tree.zeros[i - 1]]);
    }

    const padded = [...leaves];
    while (padded.length < capacity) padded.push(tree.zeros[0]);
    tree.levels[0] = padded;

    for (let l = 0; l < depth; l++) {
      const next: string[] = [];
      for (let i = 0; i < tree.levels[l].length; i += 2) {
        next.push(await poseidonHash([tree.levels[l][i], tree.levels[l][i + 1]]));
      }
      tree.levels[l + 1] = next;
    }
    return tree;
  }

  get root(): string {
    return this.levels[this.depth][0];
  }

  /** 生成指定叶索引的 Merkle 包含证明 */
  proof(leafIndex: number): MerkleProof {
    if (leafIndex < 0 || leafIndex >= 2 ** this.depth) {
      throw new Error(`leaf index out of range: ${leafIndex}`);
    }
    const pathElements: string[] = [];
    const pathIndices: number[] = [];
    let idx = leafIndex;
    for (let l = 0; l < this.depth; l++) {
      const sibling = idx % 2 === 0 ? idx + 1 : idx - 1;
      pathElements.push(this.levels[l][sibling]);
      pathIndices.push(idx % 2); // 0 = 当前节点在左, 1 = 在右
      idx = Math.floor(idx / 2);
    }
    return { leafIndex, pathElements, pathIndices };
  }
}
