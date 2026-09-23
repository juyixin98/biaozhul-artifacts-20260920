//! Merkle 树：分块、域分离哈希、奇数提升、范围证明与验证。
//!
//! # 哈希规范（所有参与方必须一致）
//!
//! - 底层哈希：SHA-256，输出 32 字节。
//! - **空文件**（0 个块）的根固定为 `SHA-256("MERKLE_EMPTY_ROOT_v1" || u64-le(chunk_size))`。
//! - **叶节点**（对应一个数据块）：`SHA-256(0x00 || u64-le(chunk_index) || u64-le(chunk_size) || chunk_bytes)`。
//!   块序号绑定进叶摘要，防止块在证明中被重排。
//! - **内部节点**：
//!   - 普通双亲：`SHA-256(0x01 || u64-le(level) || left_hash || right_hash)`，level 从 1 开始（level0 为叶层）。
//!   - **奇数提升**：某层节点数为奇数时，最后一个节点不复制、不哈希，原样提升到上一层同位置。
//! - 块大小是协议参数（默认 4096），参与叶/空根哈希，防止跨参数混用。
//!
//! 域前缀（0x00 / 0x01）使叶与内部节点不可能互相伪造；level 绑定使同值在不同层产生不同摘要。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::hex_codec;

/// 默认块大小（字节）。
pub const DEFAULT_CHUNK_SIZE: u64 = 4096;

/// SHA-256 摘要。
pub type Hash32 = [u8; 32];

/// 计算文件需要的块数。空文件为 0，否则 `ceil(len / chunk_size)`。
pub fn chunk_count(file_len: u64, chunk_size: u64) -> u64 {
    assert!(chunk_size > 0, "chunk_size must be > 0");
    file_len.div_ceil(chunk_size)
}

/// 块 `index` 在文件中的字节偏移。
pub fn chunk_offset(index: u64, chunk_size: u64) -> u64 {
    index.checked_mul(chunk_size).expect("offset overflow")
}

fn hash_empty_root(chunk_size: u64) -> Hash32 {
    let mut h = Sha256::new();
    h.update(b"MERKLE_EMPTY_ROOT_v1");
    h.update(chunk_size.to_le_bytes());
    h.finalize().into()
}

/// 空文件（0 个块）的协议根：`SHA-256("MERKLE_EMPTY_ROOT_v1" || u64-le(chunk_size))`。
pub fn hash_empty_root_pub(chunk_size: u64) -> Hash32 {
    hash_empty_root(chunk_size)
}

/// 计算叶节点摘要：`SHA-256(0x00 || u64-le(index) || u64-le(chunk_size) || bytes)`。
pub fn hash_leaf(index: u64, chunk_size: u64, bytes: &[u8]) -> Hash32 {
    let mut h = Sha256::new();
    h.update([0x00u8]);
    h.update(index.to_le_bytes());
    h.update(chunk_size.to_le_bytes());
    h.update(bytes);
    h.finalize().into()
}

/// 计算内部双亲摘要：`SHA-256(0x01 || u64-le(level) || left || right)`。
pub fn hash_pair(level: u64, left: &Hash32, right: &Hash32) -> Hash32 {
    let mut h = Sha256::new();
    h.update([0x01u8]);
    h.update(level.to_le_bytes());
    h.update(left);
    h.update(right);
    h.finalize().into()
}

/// 一层节点的奇偶归约：两两哈希，奇数个时末尾节点原样提升。
///
/// 返回新一层（长度 `ceil(n/2)`）。
pub fn reduce_level(level_no: u64, nodes: &[Hash32]) -> Vec<Hash32> {
    let mut out = Vec::with_capacity(nodes.len().div_ceil(2));
    let mut i = 0;
    while i + 1 < nodes.len() {
        out.push(hash_pair(level_no, &nodes[i], &nodes[i + 1]));
        i += 2;
    }
    if i < nodes.len() {
        // 奇数提升：最后一个节点不哈希直接上移
        out.push(nodes[i]);
    }
    out
}

/// 由叶层自底向上构建完整树，返回各层；`levels[0]` 为叶层。
pub fn build_levels(leaves: &[Hash32]) -> Vec<Vec<Hash32>> {
    let mut levels = Vec::new();
    levels.push(leaves.to_vec());
    let mut level_no = 1u64;
    while levels.last().unwrap().len() > 1 {
        let next = reduce_level(level_no, levels.last().unwrap());
        levels.push(next);
        level_no += 1;
    }
    levels
}

/// 由所有叶哈希求根。空树（无块）使用 [`hash_empty_root`]。
pub fn root_from_leaves(leaves: &[Hash32], chunk_size: u64) -> Hash32 {
    if leaves.is_empty() {
        return hash_empty_root(chunk_size);
    }
    let levels = build_levels(leaves);
    levels.last().unwrap()[0]
}

/// 由全部块数据全量重建根（验收基线：与增量更新后的根比较）。
pub fn rebuild_root_from_chunks(chunks: &[Vec<u8>], chunk_size: u64) -> Hash32 {
    let leaves: Vec<Hash32> = chunks
        .iter()
        .enumerate()
        .map(|(i, c)| hash_leaf(i as u64, chunk_size, c))
        .collect();
    root_from_leaves(&leaves, chunk_size)
}

/// 按块大小切分一段文件字节。
pub fn split_chunks(data: &[u8], chunk_size: u64) -> Vec<Vec<u8>> {
    let cs = chunk_size as usize;
    if data.is_empty() {
        return Vec::new();
    }
    let n = data.len().div_ceil(cs);
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let end = (i * cs + cs).min(data.len());
        out.push(data[i * cs..end].to_vec());
    }
    out
}

/// 把叶层上的若干修改（块序号 -> 新叶哈希）增量合并进既有各层。
///
/// 仅重算受影响路径：每层先写入所有脏孩子，再只对其覆盖到的双亲调用 [`hash_pair`]；
/// 奇数提升位与未涉及的子树保持不动。返回新的各层。
///
/// 关键不变量：同一层中两个相邻脏孩子（如同时更新块 0 和块 1）必须**都写入后**
/// 才计算它们的双亲，因此每层先统一写入、再统一收集脏双亲。
pub fn update_levels(prev: &[Vec<Hash32>], changes: &[(u64, Hash32)]) -> Vec<Vec<Hash32>> {
    let mut levels: Vec<Vec<Hash32>> = prev.to_vec();
    if levels.is_empty() {
        return levels;
    }
    let mut dirty: Vec<(u64, Hash32)> = changes.to_vec();
    // 叶层（第 0 层）始终直接写入：单块树没有更高层，叶即根。
    for &(idx, h) in &dirty {
        levels[0][idx as usize] = h;
    }
    for level_no in 1u64..(levels.len() as u64) {
        // dirty 是孩子层（level_no-1）已更新节点；叶层在循环前已写入，
        // 更高层在上一轮的 levels[level_no] 写入中就位。
        // 收集受影响双亲的位置（排序去重），统一计算（两个脏兄弟此时都已就位）
        let width = levels[(level_no - 1) as usize].len();
        let mut parents: Vec<u64> = dirty.iter().map(|(idx, _)| idx / 2).collect();
        parents.sort_unstable();
        parents.dedup();

        let mut next_dirty = Vec::with_capacity(parents.len());
        for parent in parents {
            let left_idx = parent * 2;
            let right_idx = parent * 2 + 1;
            let new_h = if (right_idx as usize) < width {
                hash_pair(
                    level_no,
                    &levels[(level_no - 1) as usize][left_idx as usize],
                    &levels[(level_no - 1) as usize][right_idx as usize],
                )
            } else {
                // 奇数提升：该“双亲位”实际承载被提升的左孩子，原样上移
                levels[(level_no - 1) as usize][left_idx as usize]
            };
            levels[level_no as usize][parent as usize] = new_h;
            next_dirty.push((parent, new_h));
        }
        dirty = next_dirty;
    }
    levels
}

/// 范围证明中的一层“邻居”。
///
/// - `left`：当前区间左侧相邻兄弟（当区间左边界是父节点右孩子时存在）
/// - `right`：当前区间右侧相邻兄弟（当区间右边界是父节点左孩子且未落在提升位时存在）
/// - `promoted`：本步切片末尾是否含**整层奇数提升位**（该节点不参与配对，原样上移）。
///   提升状态会随区间向上传播（如 n=3 的提升叶在第 1 层仍是提升节点，但其所在层宽度为偶），
///   验证者无法仅凭宽度推断，故由证明者显式给出；伪造该位只会导致最终根不匹配。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProofStep {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        with = "opt_hash_hex"
    )]
    pub left: Option<Hash32>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        with = "opt_hash_hex"
    )]
    pub right: Option<Hash32>,
    #[serde(default)]
    pub promoted: bool,
}

/// 一个连续块区间 `[start, end)` 的 Merkle 包含证明。
///
/// 证明自带块数据（仅区间内块，**不读取完整文件**），验证者逐块重算叶哈希后自底向上校验到根。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RangeProof {
    /// 协议块大小（字节）。
    pub chunk_size: u64,
    /// 文件总长度（字节）。
    pub file_length: u64,
    /// 文件总块数。
    pub total_chunks: u64,
    /// 区间起始块序号（含）。
    pub start_chunk: u64,
    /// 区间结束块序号（不含）。
    pub end_chunk: u64,
    /// 承诺根（32 字节，序列化为 hex 字符串）。
    #[serde(with = "hash_hex")]
    pub root: Hash32,
    /// 区间内块的原始数据（十六进制字符串），长度必须等于 `end_chunk - start_chunk`。
    #[serde(with = "vec_hex")]
    pub chunks: Vec<Vec<u8>>,
    /// 自叶层向上的归约步骤；空向量表示该树只有一层（单块文件且区间即整树）。
    #[serde(default)]
    pub steps: Vec<ProofStep>,
}

/// 证明校验失败的原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifyError {
    /// 块大小为 0。
    BadChunkSize,
    /// 区间非法（start > end，或越界，或空区间）。
    BadRange,
    /// `total_chunks` 与 `file_length/chunk_size` 不一致（典型的伪造长度攻击）。
    LengthMismatch,
    /// 携带的块数与区间长度不符。
    ChunkCountMismatch,
    /// 某步需要的兄弟哈希在证明中缺失。
    MissingSibling,
    /// 证明步骤数量与树高不符。
    TooManySteps,
    /// 重算根与承诺根不一致（数据/位置/根任一被篡改）。
    RootMismatch,
    /// 编码/结构错误。
    Malformed(&'static str),
}

impl std::fmt::Display for VerifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            VerifyError::BadChunkSize => write!(f, "chunk_size must be > 0"),
            VerifyError::BadRange => write!(f, "invalid chunk range"),
            VerifyError::LengthMismatch => {
                write!(f, "total_chunks inconsistent with file_length/chunk_size")
            }
            VerifyError::ChunkCountMismatch => write!(f, "chunks length does not match range"),
            VerifyError::MissingSibling => write!(f, "proof step missing required sibling"),
            VerifyError::TooManySteps => write!(f, "proof has more steps than tree height"),
            VerifyError::RootMismatch => write!(f, "recomputed root does not match committed root"),
            VerifyError::Malformed(m) => write!(f, "malformed proof: {m}"),
        }
    }
}

impl std::error::Error for VerifyError {}

fn levels_height(mut n: u64) -> usize {
    if n == 0 {
        return 0;
    }
    let mut h = 0usize;
    while n > 1 {
        n = n.div_ceil(2);
        h += 1;
    }
    h
}

/// 生成范围证明。
///
/// `read_chunk(idx)` 只需返回区间内的块数据；`levels` 为仓库当前各层（用于取兄弟哈希）。
pub fn prove_range<F>(
    levels: &[Vec<Hash32>],
    chunk_size: u64,
    file_length: u64,
    total_chunks: u64,
    start: u64,
    end: u64,
    mut read_chunk: F,
    root: Hash32,
) -> Result<RangeProof, VerifyError>
where
    F: FnMut(u64) -> Option<Vec<u8>>,
{
    if chunk_size == 0 {
        return Err(VerifyError::BadChunkSize);
    }
    if start >= end || start > total_chunks || end > total_chunks {
        return Err(VerifyError::BadRange);
    }
    if levels.is_empty() || levels[0].len() as u64 != total_chunks {
        return Err(VerifyError::Malformed(
            "tree levels do not match total_chunks",
        ));
    }
    let mut chunks = Vec::with_capacity((end - start) as usize);
    for i in start..end {
        chunks.push(read_chunk(i).ok_or(VerifyError::Malformed("chunk read failed"))?);
    }

    let mut steps: Vec<ProofStep> = Vec::new();
    let (mut lo, mut hi) = (start, end); // 当前层覆盖区间 [lo, hi)
    for level_no in 1u64..(levels.len() as u64) {
        let level = &levels[(level_no - 1) as usize];
        let width = level.len() as u64;

        // 左邻居：lo 是某双亲的右孩子（lo 奇）
        let left = if lo % 2 == 1 {
            Some(level[(lo - 1) as usize])
        } else {
            None
        };
        // 右邻居：紧邻区间右端的区间外节点（索引 hi）是某对的右（奇）孩子，且存在。
        // 一对为 (左=偶,右=奇)；此时区间最后一个节点是其左（偶）孩子。
        // 奇数提升位：width 奇时末节点索引 width-1 为偶，不配对，自然不满足 hi 奇。
        let right = if hi % 2 == 1 && hi < width {
            Some(level[hi as usize])
        } else {
            None
        };
        // 本步切片（扩展前的 [lo,hi)）是否包含整层奇数提升位：
        // 整层宽度为奇且切片覆盖到层末（hi==width）。
        let promoted = width % 2 == 1 && hi == width;
        steps.push(ProofStep {
            left,
            right,
            promoted,
        });

        // 推进到上一层
        lo = if left.is_some() { lo - 1 } else { lo } / 2;
        // 含右兄弟则扩展到 hi+1（偶），否则提升位按 width 上取整推进
        hi = if right.is_some() {
            (hi + 1) / 2
        } else {
            hi.div_ceil(2)
        };
    }

    Ok(RangeProof {
        chunk_size,
        file_length,
        total_chunks,
        start_chunk: start,
        end_chunk: end,
        root,
        chunks,
        steps,
    })
}

impl RangeProof {
    /// 独立验证范围证明。不需要除证明以外的任何数据，因此验证者无需读取完整文件。
    ///
    /// 校验：长度一致性（防伪造长度）、区间合法性、块数一致性、逐块叶重算、逐层归约到根。
    pub fn verify(&self) -> Result<(), VerifyError> {
        if self.chunk_size == 0 {
            return Err(VerifyError::BadChunkSize);
        }
        if self.start_chunk >= self.end_chunk
            || self.start_chunk > self.total_chunks
            || self.end_chunk > self.total_chunks
        {
            return Err(VerifyError::BadRange);
        }
        if chunk_count(self.file_length, self.chunk_size) != self.total_chunks {
            return Err(VerifyError::LengthMismatch);
        }
        if self.chunks.len() as u64 != self.end_chunk - self.start_chunk {
            return Err(VerifyError::ChunkCountMismatch);
        }

        // 空文件不可能有非空区间（上面 BadRange 已挡住 total_chunks==0 的情况）
        let height = levels_height(self.total_chunks);
        if self.steps.len() != height {
            // 合法证明的步数恒等于树高（奇数提升链也要逐层与上层兄弟归约到根）
            return Err(VerifyError::MissingSibling);
        }

        // 逐块重算叶哈希（带位置绑定）
        let mut cur: Vec<Hash32> = Vec::with_capacity(self.chunks.len());
        for (k, bytes) in self.chunks.iter().enumerate() {
            let idx = self.start_chunk + k as u64;
            cur.push(hash_leaf(idx, self.chunk_size, bytes));
        }

        // 当前层已知节点（即上一步归约结果），占据整层的 [lo, hi)
        let mut width = self.total_chunks;
        let (mut lo, mut hi) = (self.start_chunk, self.end_chunk);

        for level_no in 1u64..=(self.steps.len() as u64) {
            let step = &self.steps[(level_no - 1) as usize];

            // 与证明者完全相同的结构判定
            let want_left = lo % 2 == 1;
            let want_right = hi % 2 == 1 && hi < width;
            if step.left.is_some() != want_left {
                return Err(if want_left {
                    VerifyError::MissingSibling
                } else {
                    VerifyError::Malformed("unexpected left sibling")
                });
            }
            if step.right.is_some() != want_right {
                return Err(if want_right {
                    VerifyError::MissingSibling
                } else {
                    VerifyError::Malformed("unexpected right sibling")
                });
            }

            // 组装扩展切片，占整层 [slice_lo, slice_hi)，索引连续
            let slice_lo = if step.left.is_some() { lo - 1 } else { lo };
            let slice_hi = if step.right.is_some() { hi + 1 } else { hi };
            let mut layer: Vec<Hash32> = Vec::with_capacity((slice_hi - slice_lo) as usize);
            if let Some(lh) = step.left {
                layer.push(lh);
            }
            layer.extend_from_slice(&cur);
            if let Some(rh) = step.right {
                layer.push(rh);
            }
            if layer.len() as u64 != slice_hi - slice_lo {
                return Err(VerifyError::MissingSibling);
            }

            // 按全局奇偶对齐归约（slice_lo 已被左兄弟补成偶数）。
            let mut next: Vec<Hash32> =
                Vec::with_capacity((slice_hi - slice_lo).div_ceil(2) as usize);
            let mut gi = slice_lo;
            let mut k = 0u64;
            while gi + 1 < slice_hi {
                next.push(hash_pair(
                    level_no,
                    &layer[k as usize],
                    &layer[(k + 1) as usize],
                ));
                gi += 2;
                k += 2;
            }
            if gi < slice_hi {
                // 剩一个节点：必须由证明者的 promoted 位声明为整层奇数提升位，原样上移
                if step.promoted && width % 2 == 1 && gi == width - 1 {
                    next.push(layer[k as usize]);
                } else {
                    return Err(VerifyError::MissingSibling);
                }
            } else if step.promoted {
                // 声明了提升位但配对没有剩余节点，结构不自洽
                return Err(VerifyError::Malformed(
                    "promoted flag without leftover node",
                ));
            }

            lo = slice_lo / 2;
            // 上取整：含奇数提升位时切片末节点位于 (slice_hi-1)/2，新区间右端要包住它
            hi = slice_hi.div_ceil(2);
            width = width.div_ceil(2);
            cur = next;

            if cur.len() as u64 != hi - lo {
                return Err(VerifyError::MissingSibling);
            }
        }

        if cur.len() != 1 || lo != 0 || hi != 1 {
            return Err(VerifyError::MissingSibling);
        }
        if cur[0] != self.root {
            return Err(VerifyError::RootMismatch);
        }
        Ok(())
    }
}

// ---- serde 适配器：Hash32 / Option<Hash32> / Vec<Vec<u8>> 以 hex 表示 ----

pub mod hash_hex {
    use super::*;
    use serde::de::Error as _;
    use serde::{Deserializer, Serializer};

    pub fn serialize<S: Serializer>(h: &Hash32, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&hex_codec::to_hex(h))
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Hash32, D::Error> {
        let v = String::deserialize(d)?;
        let b = hex_codec::from_hex(&v).ok_or_else(|| D::Error::custom("bad hex"))?;
        b.try_into()
            .map_err(|_| D::Error::custom("hash must be 32 bytes"))
    }
}

mod opt_hash_hex {
    use super::*;
    use serde::de::Error as _;
    use serde::{Deserializer, Serializer};

    pub fn serialize<S: Serializer>(o: &Option<Hash32>, s: S) -> Result<S::Ok, S::Error> {
        match o {
            Some(h) => s.serialize_str(&hex_codec::to_hex(h)),
            None => s.serialize_none(),
        }
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Hash32>, D::Error> {
        let v: Option<String> = Option::deserialize(d)?;
        match v {
            None => Ok(None),
            Some(s) => {
                let b = hex_codec::from_hex(&s).ok_or_else(|| D::Error::custom("bad hex"))?;
                let arr: Hash32 = b
                    .try_into()
                    .map_err(|_| D::Error::custom("hash must be 32 bytes"))?;
                Ok(Some(arr))
            }
        }
    }
}

pub mod vec_hex {
    use super::*;
    use serde::de::Error as _;
    use serde::ser::SerializeSeq;
    use serde::{Deserializer, Serializer};

    pub fn serialize<S: Serializer>(v: &Vec<Vec<u8>>, s: S) -> Result<S::Ok, S::Error> {
        let mut seq = s.serialize_seq(Some(v.len()))?;
        for c in v {
            seq.serialize_element(&hex_codec::to_hex(c))?;
        }
        seq.end()
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Vec<u8>>, D::Error> {
        let raw: Vec<String> = Vec::deserialize(d)?;
        raw.into_iter()
            .map(|s| hex_codec::from_hex(&s).ok_or_else(|| D::Error::custom("bad hex")))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data_chunks(n: u64, size: usize) -> Vec<Vec<u8>> {
        (0..n)
            .map(|i| {
                let mut v = vec![0u8; size];
                v[0] = i as u8;
                v.push((i % 251) as u8);
                v
            })
            .collect()
    }

    fn leaves_of(chunks: &[Vec<u8>], cs: u64) -> Vec<Hash32> {
        chunks
            .iter()
            .enumerate()
            .map(|(i, c)| hash_leaf(i as u64, cs, c))
            .collect()
    }

    #[test]
    fn count_and_split() {
        let cs = DEFAULT_CHUNK_SIZE;
        assert_eq!(chunk_count(0, cs), 0);
        assert_eq!(chunk_count(1, cs), 1);
        assert_eq!(chunk_count(cs, cs), 1);
        assert_eq!(chunk_count(cs + 1, cs), 2);
        assert_eq!(split_chunks(&[], cs).len(), 0);
        let parts = split_chunks(&vec![7u8; (cs + 3) as usize], cs);
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0].len(), cs as usize);
        assert_eq!(parts[1].len(), 3);
    }

    #[test]
    fn empty_root_is_stable() {
        let a = root_from_leaves(&[], 1024);
        let b = root_from_leaves(&[], 1024);
        assert_eq!(a, b);
        assert_ne!(a, root_from_leaves(&[], 2048)); // chunk_size 绑定
    }

    #[test]
    fn domain_separation_leaf_vs_node() {
        // 叶与内部不可能因为输入巧合而碰撞：不同前缀
        let l = hash_leaf(0, 16, &[1]);
        let n = hash_pair(1, &[9u8; 32], &[9u8; 32]);
        assert_ne!(l, n);
        // 同对节点不同 level 摘要不同
        let (x, y) = ([1u8; 32], [2u8; 32]);
        assert_ne!(hash_pair(1, &x, &y), hash_pair(2, &x, &y));
    }

    #[test]
    fn single_chunk_tree() {
        let chunks = data_chunks(1, 10);
        let leaves = leaves_of(&chunks, 16);
        let levels = build_levels(&leaves);
        assert_eq!(levels.len(), 1);
        assert_eq!(root_from_leaves(&leaves, 16), leaves[0]);
    }

    #[test]
    fn odd_promotion_shapes() {
        // 5 个叶：层宽序列 5 -> 3 -> 2 -> 1，末位逐层提升
        let chunks = data_chunks(5, 10);
        let leaves = leaves_of(&chunks, 16);
        let levels = build_levels(&leaves);
        assert_eq!(levels.len(), 4);
        assert_eq!(levels[0].len(), 5);
        assert_eq!(levels[1].len(), 3);
        assert_eq!(levels[2].len(), 2);
        assert_eq!(levels[3].len(), 1);
        // 提升链：叶 [4] 原样提升到第一层 [2]
        assert_eq!(levels[1][2], leaves[4]);
        let l1 = reduce_level(1, &levels[0]);
        assert_eq!(l1[0], hash_pair(1, &leaves[0], &leaves[1]));
        assert_eq!(l1[1], hash_pair(1, &leaves[2], &leaves[3]));
        assert_eq!(l1[2], leaves[4]);
        // 第一层 3 个 -> 第二层 2 个：前两个哈希，末位提升
        let l2 = reduce_level(2, &l1);
        assert_eq!(l2.len(), 2);
        assert_eq!(l2[0], hash_pair(2, &l1[0], &l1[1]));
        assert_eq!(l2[1], l1[2]);
    }

    #[test]
    fn incremental_matches_full_rebuild() {
        let cs = 16u64;
        for n in 1u64..=33 {
            let chunks = data_chunks(n, 10);
            let leaves = leaves_of(&chunks, cs);
            let base = build_levels(&leaves);

            // 更新块 0 与块 n-1（若不同）
            let mut updated = chunks.clone();
            let mut new0 = updated[0].clone();
            new0[1] ^= 0xff;
            updated[0] = new0;
            let mut changes = vec![(0u64, hash_leaf(0, cs, &updated[0]))];
            if n > 1 {
                let mut last = updated[(n - 1) as usize].clone();
                last[2] ^= 0x0f;
                updated[(n - 1) as usize] = last;
                changes.push((n - 1, hash_leaf(n - 1, cs, &updated[(n - 1) as usize])));
            }

            let inc = update_levels(&base, &changes);
            let full = build_levels(&leaves_of(&updated, cs));
            assert_eq!(inc.len(), full.len(), "n={n} height differs");
            for (li, (a, b)) in inc.iter().zip(full.iter()).enumerate() {
                assert_eq!(a, b, "n={n} level {li} differs (incremental vs rebuild)");
            }
            assert_eq!(
                inc.last().unwrap()[0],
                rebuild_root_from_chunks(&updated, cs),
                "n={n} root mismatch"
            );
        }
    }

    #[test]
    fn all_range_proofs_verify_head_and_tail() {
        let cs = 16u64;
        for n in 1u64..=17 {
            let chunks = data_chunks(n, 12);
            let leaves = leaves_of(&chunks, cs);
            let levels = build_levels(&leaves);
            let root = root_from_leaves(&leaves, cs);
            let len = n * cs; // 每块填满，长度 = n*cs
            let tail_lo = n.saturating_sub(3).min(n - 1);
            let ranges = [(0, 1), (n - 1, n), (0, n.min(3)), (tail_lo, n), (0, n)];
            for (s, e) in ranges {
                if s >= e {
                    continue;
                }
                let p = prove_range(
                    &levels,
                    cs,
                    len,
                    n,
                    s,
                    e,
                    |i| Some(chunks[i as usize].clone()),
                    root,
                )
                .unwrap();
                p.verify()
                    .unwrap_or_else(|err| panic!("n={n} range [{s},{e}) verify failed: {err}"));
            }
        }
    }

    #[test]
    fn empty_file_proof_rules() {
        let cs = 8u64;
        let root = root_from_leaves(&[], cs);
        // total_chunks=0 时任何区间都非法
        let p = RangeProof {
            chunk_size: cs,
            file_length: 0,
            total_chunks: 0,
            start_chunk: 0,
            end_chunk: 1,
            root,
            chunks: vec![vec![]],
            steps: vec![],
        };
        assert_eq!(p.verify(), Err(VerifyError::BadRange));
        // 长度一致但区间为空
        let p2 = RangeProof {
            start_chunk: 1,
            end_chunk: 1,
            ..p.clone()
        };
        assert_eq!(p2.verify(), Err(VerifyError::BadRange));
    }

    #[test]
    fn forged_length_rejected() {
        let cs = 16u64;
        let chunks = data_chunks(4, 12);
        let leaves = leaves_of(&chunks, cs);
        let levels = build_levels(&leaves);
        let root = root_from_leaves(&leaves, cs);
        let mut p = prove_range(
            &levels,
            cs,
            64,
            4,
            0,
            1,
            |i| Some(chunks[i as usize].clone()),
            root,
        )
        .unwrap();
        assert!(p.verify().is_ok());

        // 攻击 1：把文件长度改大（声称多出不存在的块）
        p.file_length = 100;
        assert_eq!(p.verify(), Err(VerifyError::LengthMismatch));
        p.file_length = 64;

        // 攻击 2：篡改 total_chunks（与长度/块大小不符）
        p.total_chunks = 5;
        assert_eq!(p.verify(), Err(VerifyError::LengthMismatch));
        p.total_chunks = 4;

        // 攻击 3：声明的 total_chunks 与长度自洽，但与树不符 -> 归约路径/根失败
        // (4 块, len=64) -> (8 块, len=128) 自洽，但只有 1 块数据，steps 走不到根
        p.total_chunks = 8;
        p.file_length = 128;
        assert!(p.verify().is_err());
    }

    #[test]
    fn misplaced_and_tampered_proofs_rejected() {
        let cs = 16u64;
        let chunks = data_chunks(8, 12);
        let leaves = leaves_of(&chunks, cs);
        let levels = build_levels(&leaves);
        let root = root_from_leaves(&leaves, cs);

        // 错位：证明声称块 3，实际携带块 0 的数据（叶哈希带位置，必失败）
        let p = prove_range(
            &levels,
            cs,
            128,
            8,
            3,
            4,
            |_| Some(chunks[0].clone()), // 故意给错块
            root,
        )
        .unwrap();
        assert_eq!(p.verify(), Err(VerifyError::RootMismatch));

        // 正确证明，篡改一个字节
        let mut p = prove_range(
            &levels,
            cs,
            128,
            8,
            2,
            3,
            |i| Some(chunks[i as usize].clone()),
            root,
        )
        .unwrap();
        assert!(p.verify().is_ok());
        p.chunks[0][0] ^= 0x01;
        assert_eq!(p.verify(), Err(VerifyError::RootMismatch));

        // 错位证明：把块2的有效证明声称成块3（兄弟路径不再匹配）
        let p2 = RangeProof {
            start_chunk: 3,
            end_chunk: 4,
            ..prove_range(
                &levels,
                cs,
                128,
                8,
                2,
                3,
                |i| Some(chunks[i as usize].clone()),
                root,
            )
            .unwrap()
        };
        assert!(
            matches!(
                p2.verify(),
                Err(VerifyError::RootMismatch) | Err(VerifyError::MissingSibling)
            ),
            "misplaced proof must fail, got {:?}",
            p2.verify()
        );

        // 截断步骤：缺兄弟 -> MissingSibling
        let mut p3 = prove_range(
            &levels,
            cs,
            128,
            8,
            0,
            1,
            |i| Some(chunks[i as usize].clone()),
            root,
        )
        .unwrap();
        p3.steps.pop();
        assert_eq!(p3.verify(), Err(VerifyError::MissingSibling));

        // 伪造根
        let mut p4 = prove_range(
            &levels,
            cs,
            128,
            8,
            0,
            8,
            |i| Some(chunks[i as usize].clone()),
            root,
        )
        .unwrap();
        p4.root = [0u8; 32];
        assert_eq!(p4.verify(), Err(VerifyError::RootMismatch));
    }

    #[test]
    fn exhaustive_all_ranges_all_shapes_verify() {
        // 覆盖 n=1..=30 棵树的每一个连续区间（含全部奇数提升形状）
        let cs = 16u64;
        for n in 1u64..=30 {
            let chunks: Vec<Vec<u8>> = (0..n).map(|i| vec![((i * 7) % 251) as u8; 12]).collect();
            let leaves = leaves_of(&chunks, cs);
            let levels = build_levels(&leaves);
            let root = root_from_leaves(&leaves, cs);
            for s in 0..n {
                for e in s + 1..=n {
                    let p = prove_range(
                        &levels,
                        cs,
                        n * cs,
                        n,
                        s,
                        e,
                        |i| Some(chunks[i as usize].clone()),
                        root,
                    )
                    .unwrap();
                    p.verify()
                        .unwrap_or_else(|err| panic!("n={n} [{s},{e}) -> {err}"));
                }
            }
        }
    }

    #[test]
    fn proof_json_roundtrip() {
        let cs = 16u64;
        let chunks = data_chunks(6, 9);
        let leaves = leaves_of(&chunks, cs);
        let levels = build_levels(&leaves);
        let root = root_from_leaves(&leaves, cs);
        let p = prove_range(
            &levels,
            cs,
            96,
            6,
            1,
            4,
            |i| Some(chunks[i as usize].clone()),
            root,
        )
        .unwrap();
        let json = serde_json::to_string_pretty(&p).unwrap();
        let back: RangeProof = serde_json::from_str(&json).unwrap();
        assert_eq!(back, p);
        back.verify().unwrap();
    }
}
