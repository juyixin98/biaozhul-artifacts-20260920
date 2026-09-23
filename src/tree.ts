/**
 * Depth-8 fixed-size Poseidon Merkle tree.
 *
 * Layout mirrors the in-circuit MerkleProof(depth) template exactly:
 *   parent = Poseidon(left, right)
 * where left/right are chosen by the per-level bit in the leaf index:
 *   bit 0 -> leaf is the LEFT  child, sibling is right -> (node,   sibling)
 *   bit 1 -> leaf is the RIGHT child, sibling is left  -> (sibling, node)
 *
 * Unoccupied leaves hold the shared "zero leaf" 0; each level has a
 * deterministic "zero node" z_i = Poseidon(z_{i-1}, z_{i-1}), so an empty
 * tree still has a well-defined root (sparse-tree convention).
 */
import { poseidon } from "./hash.js";

export const TREE_DEPTH = 8;
export const LEAF_COUNT = 1 << TREE_DEPTH; // 256 leaves
export const ZERO_LEAF = 0n;

export interface MerkleProof {
  leaf: bigint;
  pathElements: bigint[]; // depth siblings, bottom-up
  pathIndices: bigint[];  // depth bits of the leaf index, bottom-up (0/1)
  leafIndex: number;
}

export class PoseidonMerkleTree {
  readonly depth: number;
  /** zeroes[0] = zero leaf; zeroes[i] = hash of two zeroes[i-1]. */
  readonly zeroes: bigint[];
  /** Dense leaf storage, length 2^depth; 0 marks an empty slot. */
  private readonly leaves: bigint[];

  private constructor(depth: number, zeroes: bigint[], leaves: bigint[]) {
    this.depth = depth;
    this.zeroes = zeroes;
    this.leaves = leaves;
  }

  static async create(depth: number = TREE_DEPTH): Promise<PoseidonMerkleTree> {
    const zeroes: bigint[] = [ZERO_LEAF];
    for (let i = 1; i <= depth; i++) {
      zeroes.push(await poseidon([zeroes[i - 1], zeroes[i - 1]]));
    }
    return new PoseidonMerkleTree(depth, zeroes, new Array(1 << depth).fill(ZERO_LEAF));
  }

  get root(): Promise<bigint> {
    return this.computeRoot();
  }

  setLeaf(index: number, leaf: bigint): void {
    if (index < 0 || index >= this.leaves.length) {
      throw new Error(`leaf index ${index} out of range [0, ${this.leaves.length})`);
    }
    this.leaves[index] = leaf;
  }

  /**
   * Root recomputed from the full dense level. Kept as a dense array (256
   * leaves) rather than a sparse map so the code is obviously correct; a
   * production tree would store only non-empty subtrees.
   */
  private async computeRoot(): Promise<bigint> {
    let level = this.leaves.slice();
    for (let d = 0; d < this.depth; d++) {
      const next: bigint[] = [];
      for (let i = 0; i < level.length; i += 2) {
        next.push(await poseidon([level[i], level[i + 1]]));
      }
      level = next;
    }
    return level[0];
  }

  /**
   * Membership proof for `index`. Siblings of empty subtrees are the
   * level zero nodes, matching the dense tree where empty leaves are 0.
   */
  async getProof(index: number): Promise<MerkleProof> {
    if (index < 0 || index >= this.leaves.length) {
      throw new Error(`leaf index ${index} out of range [0, ${this.leaves.length})`);
    }
    const pathElements: bigint[] = [];
    const pathIndices: bigint[] = [];

    let level = this.leaves.slice();
    let idx = index;
    for (let d = 0; d < this.depth; d++) {
      const siblingIndex = idx ^ 1;
      pathElements.push(level[siblingIndex]);
      pathIndices.push(BigInt(idx & 1));
      const next: bigint[] = [];
      for (let i = 0; i < level.length; i += 2) {
        next.push(await poseidon([level[i], level[i + 1]]));
      }
      level = next;
      idx >>= 1;
    }

    return {
      leaf: this.leaves[index] === ZERO_LEAF ? ZERO_LEAF : this.leaves[index],
      pathElements,
      pathIndices,
      leafIndex: index,
    };
  }

  /** Stand-alone verification of an off-chain proof (test/demo utility). */
  async verifyProof(proof: MerkleProof, expectedRoot?: bigint): Promise<boolean> {
    let node = proof.leaf;
    for (let d = 0; d < this.depth; d++) {
      const sibling = proof.pathElements[d];
      node = proof.pathIndices[d] === 0n
        ? await poseidon([node, sibling])
        : await poseidon([sibling, node]);
    }
    if (expectedRoot !== undefined) return node === expectedRoot;
    return node === await this.root;
  }
}
