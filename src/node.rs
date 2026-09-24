//! B+ 树节点的内存表示与定长页编解码。
//!
//! 页布局（小端）：
//! ```text
//! 偏移 0: u8   kind   1=叶 2=内部
//! 偏移 1: u8   保留
//! 偏移 2: u16  n      叶=键值对数；内部=分隔键数（孩子数 = n+1）
//! 叶:   [i64 key; i64 value] × n, u32 prev, u32 next
//! 内部: u32 child0, [i64 sep; u32 child] × n
//! ```
//! 所有字段定长，容量可由页大小直接算出；写页前会断言编码不超过页大小。

use std::io;

/// 页号类型；0 同时是元数据页与“空指针”（节点页从 1 开始分配）。
pub type PageId = u32;

/// 空页指针。
pub const NULL_PAGE: PageId = 0;

const KIND_LEAF: u8 = 1;
const KIND_INTERNAL: u8 = 2;

const HEADER_LEN: usize = 4;
const LEAF_TAIL_LEN: usize = 8; // prev + next
const PAIR_LEN: usize = 16; // key + value
const INTERNAL_CHILD0_LEN: usize = 4;
const INTERNAL_CELL_LEN: usize = 12; // sep + child

/// 整数键。
pub type Key = i64;
/// 载荷同样使用整数（题目要求整数键索引；值也用 i64）。
pub type Value = i64;

/// 叶节点。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Leaf {
    pub keys: Vec<Key>,
    pub values: Vec<Value>,
    pub prev: PageId,
    pub next: PageId,
}

/// 内部节点。`children.len() == separators.len() + 1`，
/// 分隔键 `separators[i]` 满足：
/// `children[i]` 子树所有键 `< separators[i]`，
/// 且 `separators[i] <= children[i+1]` 子树最小键（允许相等——分隔键是右子树最小键的副本）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Internal {
    pub separators: Vec<Key>,
    pub children: Vec<PageId>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    Leaf(Leaf),
    Internal(Internal),
}

/// 给定页大小下的节点容量参数。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 叶节点最多键值对数。
    pub leaf_max: usize,
    /// 内部节点最多分隔键数（强制为奇数，保证分裂后两侧都不少于半满）。
    pub internal_max: usize,
}

impl Limits {
    /// 由页大小推导容量。
    ///
    /// 采用经典 B 树 t 阶约定：内部最大分隔键数 = `2t-1`（强制奇数）。
    /// - 内部半满下限：`t-1` 个分隔键（t 个孩子）
    /// - 叶半满下限：`floor(leaf_max/2)` 项（奇数容量时取下界，
    ///   否则两个半满叶合并会达到 `2·ceil(L/2) = L+1` 而溢出）
    /// - 满节点分裂为两个半满节点；根节点不受半满限制
    pub fn for_page_size(page_size: u16) -> Self {
        let ps = page_size as usize;
        let leaf_max = (ps.saturating_sub(HEADER_LEN + LEAF_TAIL_LEN)) / PAIR_LEN;
        let raw = (ps.saturating_sub(HEADER_LEN + INTERNAL_CHILD0_LEN)) / INTERNAL_CELL_LEN;
        // 向下取奇数：raw 为偶数时减 1，使 max = 2t-1 且满节点编码绝不超出页大小。
        let internal_max = if raw.is_multiple_of(2) {
            raw.saturating_sub(1).max(1)
        } else {
            raw
        };
        Limits {
            leaf_max,
            internal_max,
        }
    }

    /// 非根叶节点最少键数 = floor(leaf_max/2)。
    pub fn leaf_min(&self) -> usize {
        self.leaf_max / 2
    }

    /// 内部节点阶 t：满时 2t-1 个分隔键、2t 个孩子。
    pub fn t(&self) -> usize {
        self.internal_max.div_ceil(2)
    }

    /// 非根内部节点最少分隔键数 = t-1（t 个孩子）。
    /// 合并时 (t-1)+1+(t-1) = 2t-1 个分隔键，恰好满，不溢出。
    pub fn internal_min_seps(&self) -> usize {
        self.t() - 1
    }
}

impl Leaf {
    pub fn new() -> Self {
        Leaf {
            keys: Vec::new(),
            values: Vec::new(),
            prev: NULL_PAGE,
            next: NULL_PAGE,
        }
    }

    /// 二分查找键；返回 Ok(位置) 或 Err(插入位置)。
    pub fn position(&self, key: Key) -> Result<usize, usize> {
        self.keys.binary_search(&key)
    }

    pub fn len(&self) -> usize {
        self.keys.len()
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }
}

impl Default for Leaf {
    fn default() -> Self {
        Self::new()
    }
}

impl Internal {
    pub fn new(first_child: PageId) -> Self {
        Internal {
            separators: Vec::new(),
            children: vec![first_child],
        }
    }

    /// 返回下行搜索应进入的孩子下标：第一个满足 `key < sep` 的位置，
    /// 若都不小于则为最后一个孩子。
    pub fn child_index(&self, key: Key) -> usize {
        self.separators
            .partition_point(|sep| !(key < *sep))
    }

    /// 在孩子 `child_idx` 右侧插入“分隔键 + 新右孩子”（分裂上推用）。
    pub fn insert_split(&mut self, child_idx: usize, sep: Key, right_child: PageId) {
        self.separators.insert(child_idx, sep);
        self.children.insert(child_idx + 1, right_child);
    }
}

impl Node {
    pub fn as_leaf(&self) -> &Leaf {
        match self {
            Node::Leaf(l) => l,
            _ => panic!("不是叶节点"),
        }
    }

    pub fn as_leaf_mut(&mut self) -> &mut Leaf {
        match self {
            Node::Leaf(l) => l,
            _ => panic!("不是叶节点"),
        }
    }

    pub fn as_internal(&self) -> &Internal {
        match self {
            Node::Internal(i) => i,
            _ => panic!("不是内部节点"),
        }
    }

    /// 编码到定长缓冲（缓冲长度必须等于页大小）。
    pub fn encode(&self, out: &mut [u8]) -> io::Result<()> {
        if out.len() < HEADER_LEN {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "页太小"));
        }
        out.fill(0);
        match self {
            Node::Leaf(leaf) => {
                let need = HEADER_LEN + leaf.len() * PAIR_LEN + LEAF_TAIL_LEN;
                if need > out.len() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("叶节点编码 {need} 字节超过页大小 {}", out.len()),
                    ));
                }
                out[0] = KIND_LEAF;
                out[2..4].copy_from_slice(&(leaf.len() as u16).to_le_bytes());
                let mut p = HEADER_LEN;
                for (k, v) in leaf.keys.iter().zip(&leaf.values) {
                    out[p..p + 8].copy_from_slice(&k.to_le_bytes());
                    out[p + 8..p + 16].copy_from_slice(&v.to_le_bytes());
                    p += PAIR_LEN;
                }
                out[p..p + 4].copy_from_slice(&leaf.prev.to_le_bytes());
                out[p + 4..p + 8].copy_from_slice(&leaf.next.to_le_bytes());
            }
            Node::Internal(internal) => {
                let n = internal.separators.len();
                if internal.children.len() != n + 1 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "内部节点孩子数必须等于分隔键数 + 1",
                    ));
                }
                let need = HEADER_LEN + INTERNAL_CHILD0_LEN + n * INTERNAL_CELL_LEN;
                if need > out.len() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("内部节点编码 {need} 字节超过页大小 {}", out.len()),
                    ));
                }
                out[0] = KIND_INTERNAL;
                out[2..4].copy_from_slice(&(n as u16).to_le_bytes());
                out[4..8].copy_from_slice(&internal.children[0].to_le_bytes());
                let mut p = HEADER_LEN + INTERNAL_CHILD0_LEN;
                for (sep, child) in internal.separators.iter().zip(&internal.children[1..]) {
                    out[p..p + 8].copy_from_slice(&sep.to_le_bytes());
                    out[p + 8..p + 12].copy_from_slice(&child.to_le_bytes());
                    p += INTERNAL_CELL_LEN;
                }
            }
        }
        Ok(())
    }

    /// 从一页解码。
    pub fn decode(buf: &[u8]) -> io::Result<Self> {
        if buf.len() < HEADER_LEN {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "页太小"));
        }
        let kind = buf[0];
        let n = u16::from_le_bytes(buf[2..4].try_into().unwrap()) as usize;
        match kind {
            KIND_LEAF => {
                let end = HEADER_LEN + n * PAIR_LEN + LEAF_TAIL_LEN;
                if end > buf.len() {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "叶页数据越界"));
                }
                let mut keys = Vec::with_capacity(n);
                let mut values = Vec::with_capacity(n);
                let mut p = HEADER_LEN;
                for _ in 0..n {
                    let k = i64::from_le_bytes(buf[p..p + 8].try_into().unwrap());
                    let v = i64::from_le_bytes(buf[p + 8..p + 16].try_into().unwrap());
                    keys.push(k);
                    values.push(v);
                    p += PAIR_LEN;
                }
                let prev = u32::from_le_bytes(buf[p..p + 4].try_into().unwrap());
                let next = u32::from_le_bytes(buf[p + 4..p + 8].try_into().unwrap());
                Ok(Node::Leaf(Leaf {
                    keys,
                    values,
                    prev,
                    next,
                }))
            }
            KIND_INTERNAL => {
                let end = HEADER_LEN + INTERNAL_CHILD0_LEN + n * INTERNAL_CELL_LEN;
                if end > buf.len() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "内部页数据越界",
                    ));
                }
                let first = u32::from_le_bytes(buf[4..8].try_into().unwrap());
                let mut separators = Vec::with_capacity(n);
                let mut children = Vec::with_capacity(n + 1);
                children.push(first);
                let mut p = HEADER_LEN + INTERNAL_CHILD0_LEN;
                for _ in 0..n {
                    let sep = i64::from_le_bytes(buf[p..p + 8].try_into().unwrap());
                    let child = u32::from_le_bytes(buf[p + 8..p + 12].try_into().unwrap());
                    separators.push(sep);
                    children.push(child);
                    p += INTERNAL_CELL_LEN;
                }
                Ok(Node::Internal(Internal {
                    separators,
                    children,
                }))
            }
            other => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("未知节点类型 {other}"),
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_tiny_page() {
        // 64 字节页：叶 3 项；内部 raw=4 → 强制 3 个分隔键（t=2，最少 1 个分隔键）
        let lim = Limits::for_page_size(64);
        assert_eq!(lim.leaf_max, 3);
        assert_eq!(lim.internal_max, 3);
        assert_eq!(lim.leaf_min(), 1);
        assert_eq!(lim.t(), 2);
        assert_eq!(lim.internal_min_seps(), 1);

        // 67 字节：叶 3 项；raw=4 → 向下取 3 个分隔键（5 个会超出 67 字节页）
        assert_eq!(Limits::for_page_size(67).leaf_max, 3);
        assert_eq!(Limits::for_page_size(67).internal_max, 3);

        // 68 字节：叶 3 项；raw=5
        assert_eq!(Limits::for_page_size(68).leaf_max, 3);
        assert_eq!(Limits::for_page_size(68).internal_max, 5);

        // 256 字节常规页
        let lim = Limits::for_page_size(256);
        assert_eq!(lim.leaf_max, 15);
        assert_eq!(lim.internal_max, 19); // raw=20 下调为 19
        assert_eq!(lim.leaf_min(), 7);
        assert_eq!(lim.t(), 10);
    }

    #[test]
    fn leaf_roundtrip() {
        let leaf = Node::Leaf(Leaf {
            keys: vec![-3, 7, 99],
            values: vec![30, 70, 990],
            prev: 11,
            next: 22,
        });
        let mut buf = vec![0u8; 64];
        leaf.encode(&mut buf).unwrap();
        let decoded = Node::decode(&buf).unwrap();
        assert_eq!(decoded, leaf);
    }

    #[test]
    fn internal_roundtrip() {
        let node = Node::Internal(Internal {
            separators: vec![10, 20],
            children: vec![1, 2, 3],
        });
        let mut buf = vec![0u8; 64];
        node.encode(&mut buf).unwrap();
        let decoded = Node::decode(&buf).unwrap();
        assert_eq!(decoded, node);
    }

    #[test]
    fn overflow_rejected() {
        let leaf = Node::Leaf(Leaf {
            keys: vec![1, 2, 3, 4],
            values: vec![1, 2, 3, 4],
            prev: 0,
            next: 0,
        });
        let mut buf = vec![0u8; 64];
        assert!(leaf.encode(&mut buf).is_err());
    }

    #[test]
    fn child_index_routing() {
        let i = Internal {
            separators: vec![10, 20, 30],
            children: vec![1, 2, 3, 4],
        };
        assert_eq!(i.child_index(5), 0);
        assert_eq!(i.child_index(10), 1); // 分隔键副本进右子树
        assert_eq!(i.child_index(19), 1);
        assert_eq!(i.child_index(20), 2);
        assert_eq!(i.child_index(100), 3);
    }
}
