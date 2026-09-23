//! 节点页的内存表示与定长二进制编解码。
//!
//! 页布局（小端）：
//! - 叶节点：`[kind:1][count:u32][next:u64]` 后接 `count` 个 `(key:i64, value:u64)`，每项 16 字节。
//! - 内部节点：`[kind:1][child_count:u32]` 后接
//!   `child0, sep0, child1, sep1, ..., child_{n-1}`，其中 `sep_i` 是 child_i 与 child_{i+1} 之间的分隔键。
//!
//! 路由约定：child_i 容纳所有满足 `seps[i-1] <= key < seps[i]` 的键
//! （即 child_i 中的键严格小于 `seps[i]`，且大于等于 `seps[ii-1]`）。

use crate::pager::{PageId, Pager, NIL};

pub const KIND_LEAF: u8 = 1;
pub const KIND_INTERNAL: u8 = 2;

const LEAF_HEADER: usize = 13; // kind(1) + count(4) + next(8)
const INTERNAL_HEADER: usize = 5; // kind(1) + count(4)
pub const LEAF_ENTRY: usize = 16; // key(8) + value(8)

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    Leaf(LeafNode),
    Internal(InternalNode),
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct LeafNode {
    /// 键严格升序、唯一。
    pub keys: Vec<i64>,
    pub values: Vec<u64>,
    pub next: PageId,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct InternalNode {
    /// children.len() == seps.len() + 1
    pub children: Vec<PageId>,
    pub seps: Vec<i64>,
}

impl Node {
    pub fn leaf() -> Self {
        Node::Leaf(LeafNode::default())
    }

    pub fn kind(&self) -> u8 {
        match self {
            Node::Leaf(_) => KIND_LEAF,
            Node::Internal(_) => KIND_INTERNAL,
        }
    }

    pub fn as_leaf(&self) -> &LeafNode {
        match self {
            Node::Leaf(l) => l,
            _ => panic!("不是叶节点"),
        }
    }

    pub fn as_leaf_mut(&mut self) -> &mut LeafNode {
        match self {
            Node::Leaf(l) => l,
            _ => panic!("不是叶节点"),
        }
    }

    pub fn as_internal(&self) -> &InternalNode {
        match self {
            Node::Internal(i) => i,
            _ => panic!("不是内部节点"),
        }
    }

    pub fn as_internal_mut(&mut self) -> &mut InternalNode {
        match self {
            Node::Internal(i) => i,
            _ => panic!("不是内部节点"),
        }
    }

    /// 叶节点最多容纳的键值对数。
    pub fn leaf_capacity(page_size: usize) -> usize {
        page_size.saturating_sub(LEAF_HEADER) / LEAF_ENTRY
    }

    /// 内部节点最多容纳的子指针数。
    pub fn internal_capacity(page_size: usize) -> usize {
        // 5 + (2n-1)*8 <= page_size  =>  n <= (slots+1)/2，其中 slots = (page_size-5)/8
        let slots = page_size.saturating_sub(INTERNAL_HEADER) / 8;
        slots.div_ceil(2)
    }

    /// 叶节点最少应保留的项数（根除外）。
    pub fn leaf_min_fill(page_size: usize) -> usize {
        Self::leaf_capacity(page_size).div_ceil(2)
    }

    /// 内部非根节点最少应保留的子指针数。
    pub fn internal_min_children(page_size: usize) -> usize {
        Self::internal_capacity(page_size).div_ceil(2)
    }

    pub fn encode(&self, buf: &mut [u8]) {
        buf.fill(0);
        match self {
            Node::Leaf(l) => {
                assert_eq!(l.keys.len(), l.values.len());
                buf[0] = KIND_LEAF;
                buf[1..5].copy_from_slice(&(l.keys.len() as u32).to_le_bytes());
                buf[5..13].copy_from_slice(&l.next.to_le_bytes());
                let mut off = LEAF_HEADER;
                for (k, v) in l.keys.iter().zip(&l.values) {
                    buf[off..off + 8].copy_from_slice(&k.to_le_bytes());
                    buf[off + 8..off + 16].copy_from_slice(&v.to_le_bytes());
                    off += LEAF_ENTRY;
                }
                assert!(off <= buf.len(), "叶节点超出页容量");
            }
            Node::Internal(i) => {
                assert_eq!(i.children.len(), i.seps.len() + 1);
                buf[0] = KIND_INTERNAL;
                buf[1..5].copy_from_slice(&(i.children.len() as u32).to_le_bytes());
                let mut off = INTERNAL_HEADER;
                for (idx, child) in i.children.iter().enumerate() {
                    buf[off..off + 8].copy_from_slice(&child.to_le_bytes());
                    off += 8;
                    if let Some(sep) = i.seps.get(idx) {
                        buf[off..off + 8].copy_from_slice(&sep.to_le_bytes());
                        off += 8;
                    }
                }
                assert!(off <= buf.len(), "内部节点超出页容量");
            }
        }
    }

    pub fn decode(buf: &[u8]) -> Self {
        let kind = buf[0];
        match kind {
            KIND_LEAF => {
                let n = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
                let next = u64::from_le_bytes(buf[5..13].try_into().unwrap());
                let mut keys = Vec::with_capacity(n);
                let mut values = Vec::with_capacity(n);
                let mut off = LEAF_HEADER;
                for _ in 0..n {
                    let k = i64::from_le_bytes(buf[off..off + 8].try_into().unwrap());
                    let v = u64::from_le_bytes(buf[off + 8..off + 16].try_into().unwrap());
                    keys.push(k);
                    values.push(v);
                    off += LEAF_ENTRY;
                }
                Node::Leaf(LeafNode { keys, values, next })
            }
            KIND_INTERNAL => {
                let n = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
                let mut children = Vec::with_capacity(n);
                let mut seps = Vec::with_capacity(n.saturating_sub(1));
                let mut off = INTERNAL_HEADER;
                for idx in 0..n {
                    let child = u64::from_le_bytes(buf[off..off + 8].try_into().unwrap());
                    children.push(child);
                    off += 8;
                    if idx + 1 < n {
                        let sep = i64::from_le_bytes(buf[off..off + 8].try_into().unwrap());
                        seps.push(sep);
                        off += 8;
                    }
                }
                Node::Internal(InternalNode { children, seps })
            }
            other => panic!("未知节点类型字节 {other}"),
        }
    }
}

impl LeafNode {
    /// 返回可插入 `key` 的位置；若键已存在，返回 `Err(pos)`。
    pub fn locate(&self, key: i64) -> Result<usize, usize> {
        let pos = self.keys.partition_point(|k| *k < key);
        if pos < self.keys.len() && self.keys[pos] == key {
            Err(pos)
        } else {
            Ok(pos)
        }
    }
}

impl InternalNode {
    /// 路由：返回查找/插入键应进入的子节点下标。
    pub fn child_index(&self, key: i64) -> usize {
        self.seps.partition_point(|s| *s <= key)
    }
}

/// 从 pager 读取一页并解码。
pub fn load_node(pager: &dyn Pager, id: PageId) -> Node {
    let mut buf = vec![0u8; pager.page_size()];
    pager.read_page(id, &mut buf);
    Node::decode(&buf)
}

/// 编码并写回一页。
pub fn store_node(pager: &mut dyn Pager, id: PageId, node: &Node) {
    let mut buf = vec![0u8; pager.page_size()];
    node.encode(&mut buf);
    pager.write_page(id, &buf);
}

/// 新建并落盘一个空叶页，返回页号。
pub fn alloc_leaf(pager: &mut dyn Pager) -> PageId {
    let id = pager.alloc_page();
    let mut buf = vec![0u8; pager.page_size()];
    buf[0] = KIND_LEAF;
    buf[5..13].copy_from_slice(&NIL.to_le_bytes());
    pager.write_page(id, &buf);
    id
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capacities_for_tiny_pages() {
        // page=64：叶 3 项，内部 4 子指针
        assert_eq!(Node::leaf_capacity(64), 3);
        assert_eq!(Node::internal_capacity(64), 4);
        assert_eq!(Node::leaf_min_fill(64), 2);
        assert_eq!(Node::internal_min_children(64), 2);
        // page=128：叶 7 项，内部 8 子指针
        assert_eq!(Node::leaf_capacity(128), 7);
        assert_eq!(Node::internal_capacity(128), 8);
        assert_eq!(Node::leaf_min_fill(128), 4);
        assert_eq!(Node::internal_min_children(128), 4);
    }

    #[test]
    fn leaf_roundtrip_and_locate() {
        let ps = 64;
        let mut l = LeafNode {
            keys: vec![1, 5, 9],
            values: vec![10, 50, 90],
            next: NIL,
        };
        let _ = &mut l;
        let mut buf = vec![0u8; ps];
        Node::Leaf(l.clone()).encode(&mut buf);
        let decoded = Node::decode(&buf);
        assert_eq!(decoded, Node::Leaf(l.clone()));
        assert_eq!(l.locate(0), Ok(0));
        assert_eq!(l.locate(5), Err(1));
        assert_eq!(l.locate(6), Ok(2));
        assert_eq!(l.locate(100), Ok(3));
    }

    #[test]
    fn internal_routing() {
        let i = InternalNode {
            children: vec![10, 11, 12],
            seps: vec![100, 200],
        };
        assert_eq!(i.child_index(-5), 0);
        assert_eq!(i.child_index(100), 1);
        assert_eq!(i.child_index(150), 1);
        assert_eq!(i.child_index(200), 2);
        assert_eq!(i.child_index(999), 2);
    }
}
