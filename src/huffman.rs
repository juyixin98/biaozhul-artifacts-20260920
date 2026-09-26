//! 霍夫曼核心：确定性建树 → 码长 → 规范码；以及解码前缀树。
//!
//! # 设计要点
//!
//! - **只保留码长**：建树仅用于得到每个符号的码长；实际码字一律由规范
//!   （canonical）算法重新生成，因此输出不依赖左右孩子约定这类偶然因素。
//! - **确定性平局规则**：优先队列按 `(权值, 次级键)` 排序；叶子的次级键
//!   为符号值 `0..=255`，内部节点为创建序号加 `256`。每个节点键唯一，
//!   所以等频率输入的码长完全确定。
//! - **大长度支持**：256 个符号的二叉树深度至多为 255，码长用 `u8`；
//!   规范码用位向量表示，无需大整数即可支持最深 255 位并精确检测
//!   Kraft 过度订阅 / 不完整。
//! - **单符号**：频率表只有一个符号时约定码长为 1、码字为 `0`；解码端
//!   每读到一个 `0` 位就输出该符号（类似 DEFLATE 的单符号处理方式）。

use crate::error::{Error, Result};
use std::cmp::Reverse;
use std::collections::BinaryHeap;

/// 字母表大小（字节）。
pub const ALPHABET: usize = 256;
/// 支持的最大码长（256 叶二叉树最坏深度 255）。
pub const MAX_CODE_LEN: u8 = 255;

/// 码长表：下标为符号，0 表示该符号不出现，1..=255 为码长。
pub type LengthTable = [u8; ALPHABET];

/// 统计频率。
pub fn frequencies(input: &[u8]) -> [u64; ALPHABET] {
    let mut freq = [0u64; ALPHABET];
    for &b in input {
        freq[b as usize] += 1;
    }
    freq
}

/// 堆节点：权值主序、次级键兜底，全序唯一。
#[derive(Eq, PartialEq, Ord, PartialOrd, Clone)]
struct Node {
    weight: u64,
    /// 叶子=符号值 (0..256)；内部节点=256+创建序号。
    tie: u32,
    kind: NodeKind,
}

#[derive(Clone)]
enum NodeKind {
    Leaf(u8),
    Internal(Box<Node>, Box<Node>),
}

// Node 的排序完全由 (weight, tie) 决定；kind 只提供恒等序。
impl PartialEq for NodeKind {
    fn eq(&self, other: &Self) -> bool {
        std::mem::discriminant(self) == std::mem::discriminant(other)
    }
}
impl Eq for NodeKind {}
impl PartialOrd for NodeKind {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for NodeKind {
    fn cmp(&self, _other: &Self) -> std::cmp::Ordering {
        std::cmp::Ordering::Equal
    }
}

/// 由频率表构造码长表（确定性）。
///
/// 空频率表返回全 0；单符号返回该符号码长 1。
pub fn build_lengths(freq: &[u64; ALPHABET]) -> LengthTable {
    let mut lengths = [0u8; ALPHABET];
    let mut heap: BinaryHeap<Reverse<Node>> = BinaryHeap::new();
    let mut present = 0usize;

    for (sym, &f) in freq.iter().enumerate() {
        if f > 0 {
            heap.push(Reverse(Node {
                weight: f,
                tie: sym as u32,
                kind: NodeKind::Leaf(sym as u8),
            }));
            present += 1;
        }
    }

    if present == 0 {
        return lengths;
    }
    if present == 1 {
        for (sym, &f) in freq.iter().enumerate() {
            if f > 0 {
                lengths[sym] = 1;
            }
        }
        return lengths;
    }

    let mut seq: u32 = 0;
    while heap.len() > 1 {
        let Reverse(a) = heap.pop().unwrap();
        let Reverse(b) = heap.pop().unwrap();
        // 确定性合并：较小权为左孩子；权同则次级键小者为左。
        let (left, right) = if (a.weight, a.tie) <= (b.weight, b.tie) {
            (a, b)
        } else {
            (b, a)
        };
        heap.push(Reverse(Node {
            weight: left.weight + right.weight,
            tie: 256 + seq,
            kind: NodeKind::Internal(Box::new(left), Box::new(right)),
        }));
        seq += 1;
    }

    let Reverse(root) = heap.pop().unwrap();
    assign_depths(&root, 0, &mut lengths);
    lengths
}

fn assign_depths(node: &Node, depth: u16, lengths: &mut LengthTable) {
    debug_assert!(depth <= MAX_CODE_LEN as u16, "tree deeper than 255");
    match &node.kind {
        NodeKind::Leaf(sym) => lengths[*sym as usize] = depth as u8,
        NodeKind::Internal(l, r) => {
            assign_depths(l, depth + 1, lengths);
            assign_depths(r, depth + 1, lengths);
        }
    }
}

/// 规范码字（位向量，下标 0 为码字最高位）。
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CodeBits(Vec<u8>);

impl CodeBits {
    /// 码长。
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// 码长是否为 0。
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// 迭代码字的每一位（高位在前）。
    pub fn bits(&self) -> impl Iterator<Item = u8> + '_ {
        self.0.iter().copied()
    }
}

/// 一个符号的码长与规范码。
#[derive(Clone, Debug)]
pub struct CodeEntry {
    pub symbol: u8,
    pub length: u8,
    pub code: CodeBits,
}

/// 完整码本。
#[derive(Clone, Debug)]
pub struct CodeBook {
    pub entries: Vec<CodeEntry>,
    /// 单符号模式（码长 1、码字 0）。
    pub single_symbol: bool,
}

/// 位向量加一；返回 true 表示全 1 溢出回绕（当前长度码空间耗尽）。
fn increment(code: &mut [u8]) -> bool {
    for bit in code.iter_mut().rev() {
        if *bit == 0 {
            *bit = 1;
            return false;
        }
        *bit = 0;
    }
    true
}

/// 码本生成的严格程度。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Strictness {
    /// 必须 Kraft 完备（编码端、以及解码端 Reject 策略）。
    Complete,
    /// 允许 Kraft 和 < 1（解码端 AllowIncomplete 策略）。
    AllowIncomplete,
}

/// 由码长表生成规范码（内部实现）。
///
/// 过度订阅在两种严格程度下都报错；不完整仅在 `Complete` 下报错。
fn generate_book(lengths: &LengthTable, mode: Strictness) -> Result<CodeBook> {
    let mut pairs: Vec<(u8, u8)> = lengths
        .iter()
        .enumerate()
        .filter(|(_, &l)| l != 0)
        .map(|(s, &l)| (s as u8, l))
        .collect();

    if pairs.is_empty() {
        return Err(Error::InvalidLengthTable("no symbols in table"));
    }
    if pairs.len() == 1 {
        let (symbol, length) = pairs.pop().unwrap();
        if length != 1 {
            return Err(Error::InvalidLengthTable(
                "single-symbol table must use length 1",
            ));
        }
        return Ok(CodeBook {
            entries: vec![CodeEntry {
                symbol,
                length: 1,
                code: CodeBits(vec![0]),
            }],
            single_symbol: true,
        });
    }

    // pairs 中的长度来自 u8 且已过滤 0，故必在 1..=255（=MAX_CODE_LEN）。

    // 规范排序：码长升序，同长按符号升序。
    pairs.sort_unstable_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)));

    let mut entries = Vec::with_capacity(pairs.len());
    let mut code: Vec<u8> = Vec::new();
    // 上一次自增是否把 code 推到了 2^len（向量内表现为全 0 + 该标记）。
    let mut at_power_of_two = false;
    for (symbol, length) in pairs.iter() {
        // code <<= (length - code.len())
        while code.len() < *length as usize {
            code.push(0);
        }
        // 移位后若 code == 2^length，说明当前长度码空间已耗尽 → 过度订阅。
        if at_power_of_two {
            return Err(Error::Oversubscribed);
        }
        entries.push(CodeEntry {
            symbol: *symbol,
            length: *length,
            code: CodeBits(code.clone()),
        });
        at_power_of_two = increment(&mut code);
    }

    // Kraft 完备性：最终 code == 2^max_len（恰好回绕）才完备；
    // 否则最后一个码之后仍有未覆盖位串 ⇔ Kraft 和 < 1。
    if mode == Strictness::Complete && !at_power_of_two {
        return Err(Error::IncompleteCode);
    }

    Ok(CodeBook {
        entries,
        single_symbol: false,
    })
}

/// 由码长表生成规范码并要求 Kraft 完备。
pub fn build_canonical_codes(lengths: &LengthTable) -> Result<CodeBook> {
    generate_book(lengths, Strictness::Complete)
}

/// 解码前缀树节点索引。
type NodeIdx = u32;
const NONE: NodeIdx = u32::MAX;

struct TrieNode {
    /// [bit 0 子节点, bit 1 子节点]。
    children: [NodeIdx; 2],
    /// 叶子时为符号。
    symbol: Option<u8>,
}

/// 解码表。
pub struct DecodeTable {
    nodes: Vec<TrieNode>,
    single_symbol: Option<u8>,
}

impl DecodeTable {
    /// 树根（多符号模式）。
    pub const ROOT: NodeIdx = 0;

    fn new() -> Self {
        Self {
            nodes: Vec::new(),
            single_symbol: None,
        }
    }

    fn new_node(&mut self) -> NodeIdx {
        let idx = self.nodes.len() as NodeIdx;
        self.nodes.push(TrieNode {
            children: [NONE, NONE],
            symbol: None,
        });
        idx
    }

    /// 按一位下降；未定义分支返回 `None`（仅不完整码表可能发生）。
    pub fn descend(&self, node: NodeIdx, bit: u8) -> Option<NodeIdx> {
        let next = self.nodes[node as usize].children[bit as usize];
        (next != NONE).then_some(next)
    }

    /// 节点为叶子时返回符号。
    pub fn leaf_symbol(&self, node: NodeIdx) -> Option<u8> {
        self.nodes[node as usize].symbol
    }

    /// 单符号模式的符号。
    pub fn single_symbol(&self) -> Option<u8> {
        self.single_symbol
    }
}

/// 由码长表构造解码前缀树。
///
/// `allow_incomplete=false`（默认策略）：Kraft 和 < 1 的码表直接报
/// [`Error::IncompleteCode`]；为 `true` 时允许不完整码表，但位流一旦
/// 走到未定义分支，解码层会报 [`Error::UndefinedCodeword`]。
/// 过度订阅在任何策略下都拒绝。
pub fn build_decode_trie(lengths: &LengthTable, allow_incomplete: bool) -> Result<DecodeTable> {
    let present = lengths.iter().filter(|&&l| l != 0).count();
    if present == 1 {
        let pos = lengths.iter().position(|&l| l != 0).unwrap();
        if lengths[pos] != 1 {
            return Err(Error::InvalidLengthTable(
                "single-symbol table must use length 1",
            ));
        }
        let mut table = DecodeTable::new();
        table.single_symbol = Some(pos as u8);
        // 单符号模式不使用 nodes，但保留一个根节点统一内存表示。
        table.new_node();
        return Ok(table);
    }

    let mode = if allow_incomplete {
        Strictness::AllowIncomplete
    } else {
        Strictness::Complete
    };
    let book = generate_book(lengths, mode)?;

    let mut table = DecodeTable::new();
    let root = table.new_node();
    debug_assert_eq!(root, DecodeTable::ROOT);

    for entry in &book.entries {
        let mut cur = root;
        let bits: Vec<u8> = entry.code.bits().collect();
        for (depth, &bit) in bits.iter().enumerate() {
            let last = depth + 1 == bits.len();
            if table.nodes[cur as usize].symbol.is_some() {
                return Err(Error::InvalidLengthTable(
                    "codeword passes through an existing leaf",
                ));
            }
            let next = match table.nodes[cur as usize].children[bit as usize] {
                NONE => {
                    let next = table.new_node();
                    table.nodes[cur as usize].children[bit as usize] = next;
                    next
                }
                existing => existing,
            };
            cur = next;
            if last {
                if table.nodes[cur as usize].symbol.is_some() {
                    return Err(Error::InvalidLengthTable("duplicate codeword"));
                }
                table.nodes[cur as usize].symbol = Some(entry.symbol);
            }
        }
    }

    Ok(table)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lengths_of(syms: &[(u8, u8)]) -> LengthTable {
        let mut t = [0u8; 256];
        for &(s, l) in syms {
            t[s as usize] = l;
        }
        t
    }

    fn codes_vec(book: &CodeBook) -> Vec<(u8, String)> {
        book.entries
            .iter()
            .map(|e| {
                (
                    e.symbol,
                    e.code.bits().map(|b| (b'0' + b) as char).collect(),
                )
            })
            .collect()
    }

    #[test]
    fn canonical_two_symbols() {
        let t = lengths_of(&[(0, 1), (1, 1)]);
        let book = build_canonical_codes(&t).unwrap();
        assert_eq!(
            codes_vec(&book),
            vec![(0u8, "0".to_string()), (1, "1".to_string())]
        );
    }

    #[test]
    fn canonical_known_vector() {
        // 经典例子：A=2,B=1,C=3,D=1 → 码长 (1,2,3,3)
        let t = lengths_of(&[
            ('A' as u8, 2),
            ('B' as u8, 3),
            ('C' as u8, 1),
            ('D' as u8, 3),
        ]);
        let book = build_canonical_codes(&t).unwrap();
        assert_eq!(
            codes_vec(&book),
            vec![
                ('C' as u8, "0".to_string()),
                ('A' as u8, "10".to_string()),
                ('B' as u8, "110".to_string()),
                ('D' as u8, "111".to_string()),
            ]
        );
    }

    #[test]
    fn equal_frequencies_are_deterministic() {
        // 4 个等频率符号：码长必须全部为 2，且码本确定。
        let mut freq = [0u64; 256];
        for s in 0..4u16 {
            freq[s as usize] = 100;
        }
        let l1 = build_lengths(&freq);
        let l2 = build_lengths(&freq);
        assert_eq!(l1, l2);
        assert_eq!((0..4).map(|s| l1[s]).collect::<Vec<_>>(), vec![2, 2, 2, 2]);
        let b1 = codes_vec(&build_canonical_codes(&l1).unwrap());
        let b2 = codes_vec(&build_canonical_codes(&l2).unwrap());
        assert_eq!(b1, b2);
    }

    #[test]
    fn equal_frequencies_eight_symbols() {
        let mut freq = [0u64; 256];
        for s in 0..8u16 {
            freq[s as usize] = 7;
        }
        let l = build_lengths(&freq);
        for s in 0..8 {
            assert_eq!(l[s], 3);
        }
    }

    #[test]
    fn single_symbol_length_one() {
        let mut freq = [0u64; 256];
        freq[42] = 999;
        let l = build_lengths(&freq);
        assert_eq!(l[42], 1);
        let book = build_canonical_codes(&l).unwrap();
        assert!(book.single_symbol);
        assert_eq!(codes_vec(&book), vec![(42u8, "0".to_string())]);
    }

    #[test]
    fn empty_frequency_table() {
        let l = build_lengths(&[0u64; 256]);
        assert!(l.iter().all(|&x| x == 0));
        assert_eq!(
            build_canonical_codes(&l).unwrap_err(),
            Error::InvalidLengthTable("no symbols in table")
        );
    }

    #[test]
    fn oversubscribed_table_rejected() {
        // 两个长度 1 的码再加一个长度 2：Kraft = 1.25
        let t = lengths_of(&[(0, 1), (1, 1), (2, 2)]);
        assert_eq!(
            build_canonical_codes(&t).unwrap_err(),
            Error::Oversubscribed
        );
    }

    #[test]
    fn incomplete_table_rejected_in_strict_mode() {
        // 只有 0 和 10，Kraft = 0.75
        let t = lengths_of(&[(0, 1), (1, 2)]);
        assert_eq!(
            build_canonical_codes(&t).unwrap_err(),
            Error::IncompleteCode
        );
        // 宽松模式可建树。
        let table = build_decode_trie(&t, true).unwrap();
        assert_eq!(
            table.leaf_symbol(table.descend(DecodeTable::ROOT, 0).unwrap()),
            Some(0)
        );
        // 走到未定义分支 11。
        let n = table.descend(DecodeTable::ROOT, 1).unwrap();
        let n0 = table.descend(n, 0).unwrap();
        assert_eq!(table.leaf_symbol(n0), Some(1));
        assert_eq!(table.descend(n, 1), None);
    }

    #[test]
    fn skew_tree_depths() {
        // 斐波那契式权值产生接近最坏情况的偏斜树。
        let mut freq = [0u64; 256];
        let (mut a, mut b) = (1u64, 1u64);
        for s in 0..20 {
            freq[s] = a;
            let next = a + b;
            a = b;
            b = next;
        }
        let l = build_lengths(&freq);
        let max = *l.iter().max().unwrap();
        assert!(max >= 10, "expected skewed tree, got max depth {max}");
        assert!(max <= MAX_CODE_LEN);
        // 自身生成的码长必须 Kraft 完备。
        build_canonical_codes(&l).unwrap();
    }
}
