//! 磁盘格式定义。所有整数采用**小端序**。
//!
//! # 文件布局
//!
//! 一个存储目录包含两个文件：
//!
//! - `data.log`：仅追加的数据文件，存放 LEAF（键值）与 ROOT（根快照）记录；
//! - `super.db`：固定大小的双页超级块文件，两个 4 KiB 槽位交替覆写。
//!
//! # 数据记录格式（data.log，仅追加）
//!
//! ```text
//! 偏移  长度  字段
//! 0     4     magic  = 0x44_53_42_52 ("DSBR")
//! 4     1     kind   = 1: LEAF, 2: ROOT
//! 5     4     payload_len（小端 u32）
//! 9     4     crc32，覆盖 magic..payload 全部字节（含本字段时按 0 计）
//! 13    3     保留，必须为 0
//! 16    N     payload
//! ```
//!
//! LEAF payload：`[u32 key_len][key bytes][u32 val_len][value bytes]`
//! ROOT payload：
//! ```text
//! [u64 generation]
//! [u32 entry_count]
//! entry_count × {
//!     u32 key_len, key bytes,
//!     u64 leaf_offset, u64 leaf_len, u32 leaf_crc,
//! }
//! ```
//! ROOT 记录自身落在数据文件中；超级块只保存它的偏移/长度/CRC 与代次。
//!
//! # 超级块页格式（super.db，每页 PAGE_SIZE=4096 字节）
//!
//! ```text
//! 槽位 0：文件偏移 [0, 4096)；槽位 1：[4096, 8192)
//! 槽内偏移     长度  字段
//! 0            4     magic = 0x44_53_42_53 ("DSBS")
//! 4            2     format_version = 1
//! 6            8     generation（u64，单调递增；0 保留给 format 后的空快照）
//! 14           8     root_offset（u64；空根时为 0，root_len 也必须为 0）
//! 22           8     root_len（u64）
//! 30           4     root_crc（u32，即 ROOT 记录整段 16+N 字节的 CRC32）
//! 34           4     page_crc（u32，对**整页 4096 字节**计算，本字段按 0 计）
//! 38..4078     0     保留，填 0
//! 4078         4     尾戳 magic = 0x44_53_42_54 ("DSBT")
//! 4082         8     尾戳 generation（必须与偏移 6 处一致）
//! 4090         4     尾戳 root_crc（必须与偏移 30 处一致）
//! 4094         2     保留，填 0
//! 整页一次性写满 4096 字节。
//! ```
//!
//! # 为什么需要整页 CRC + 页尾冗余
//!
//! 介质可能在一次 4 KiB 页写中途断电（torn page）。两道防线缺一不可：
//!
//! 1. **整页 CRC**：任何“新内容只落盘一部分、剩余字节是旧页残留”的组合都无法
//!    匹配校验和。
//! 2. **页尾冗余（尾戳）**：若只在页首放元数据，那么当所有保留区都是确定性
//!    字节（例如全 0）时，“新页头 + 旧页零尾巴”可能与完整新页逐字节相同，
//!    位于头部之后的撕裂将无法被察觉。把 generation/root_crc 的副本放到**页
//!    最后 18 字节**后，有效信息跨越整页：只有恰好完整的 4096 字节写入才能
//!    同时满足 CRC 与首尾一致性，任何半页写都被拒绝，恢复改读另一槽位。
//!
//! # 同步边界（提交协议）
//!
//! 一次写入（可含多个键）严格按下列顺序执行，每步之间即为故障点：
//!
//! 1. 追加全部 LEAF 记录并 `fsync` 数据文件；
//! 2. 追加 ROOT 记录并 `fsync` 数据文件（先数据，后根）；
//! 3. 将新超级块整页（4096B，单次 write）写入交替槽位并 `fsync` 超级块文件。
//!
//! 只有第 3 步完成后，新代次才算“发布”。

use crate::crc::checksum;

pub const PAGE_SIZE: usize = 4096;
pub const DATA_FILE: &str = "data.log";
pub const SUPER_FILE: &str = "super.db";

pub const SUPER_SLOTS: usize = 2;
pub const SUPER_FILE_SIZE: usize = PAGE_SIZE * SUPER_SLOTS;

pub const REC_MAGIC: u32 = 0x44_53_42_52; // "DSBR"
pub const SUPER_MAGIC: u32 = 0x44_53_42_53; // "DSBS"
pub const FORMAT_VERSION: u16 = 1;

pub const KIND_LEAF: u8 = 1;
pub const KIND_ROOT: u8 = 2;

pub const REC_HEADER_LEN: usize = 16;
pub const SUPER_HEADER_LEN: usize = 38;

/// 页尾尾戳魔数 "DSBT"（Dual SuperBlock Trailer）。
pub const SUPER_TRAILER_MAGIC: u32 = 0x44_53_42_54;
/// 尾戳长度：magic(4) + generation(8) + root_crc(4) + reserved(2) = 18。
pub const SUPER_TRAILER_LEN: usize = 18;
/// 尾戳在页内的起始偏移。
pub const SUPER_TRAILER_OFFSET: usize = PAGE_SIZE - SUPER_TRAILER_LEN; // 4078

/// 指向数据文件中一条记录的指针（同时用于 LEAF 与 ROOT 引用）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Ptr {
    pub offset: u64,
    pub len: u64,
    pub crc: u32,
}

impl Ptr {
    pub fn empty() -> Self {
        Ptr {
            offset: 0,
            len: 0,
            crc: 0,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.offset == 0 && self.len == 0
    }

    /// 指针是否落在 `file_len` 范围内。offset==0 且 len==0（空指针）合法。
    pub fn is_in_bounds(&self, file_len: u64) -> bool {
        if self.is_empty() {
            return true;
        }
        // offset==0 且 len>0：数据记录从偏移 0 起可以存在，但本实现规定
        // 空指针以 (0,0) 表示；offset=0 仍允许指向真实记录（首条记录就在 0）。
        let end = match self.offset.checked_add(self.len) {
            Some(e) => e,
            None => return false,
        };
        self.len >= REC_HEADER_LEN as u64 && end <= file_len
    }
}

/// 解析后的超级块内容。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuperBlock {
    pub generation: u64,
    pub root: Ptr,
}

// ---------- 小端读写辅助 ----------

pub fn put_u16(b: &mut [u8], v: u16) {
    b[..2].copy_from_slice(&v.to_le_bytes());
}
pub fn put_u32(b: &mut [u8], v: u32) {
    b[..4].copy_from_slice(&v.to_le_bytes());
}
pub fn put_u64(b: &mut [u8], v: u64) {
    b[..8].copy_from_slice(&v.to_le_bytes());
}
pub fn get_u16(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}
pub fn get_u32(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}
pub fn get_u64(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

// ---------- 数据记录 ----------

/// 构造一条数据记录（16 字节头 + payload）。
pub fn encode_record(kind: u8, payload: &[u8]) -> Vec<u8> {
    let total = REC_HEADER_LEN + payload.len();
    assert!(total <= u32::MAX as usize, "record too large");
    let mut out = Vec::with_capacity(total);
    out.extend_from_slice(&REC_MAGIC.to_le_bytes()); // 0..4 magic
    out.push(kind); // 4 kind
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes()); // 5..9
    out.extend_from_slice(&0u32.to_le_bytes()); // 9..13 crc 占位
    out.extend_from_slice(&[0u8; 3]); // 13..16 reserved
    out.extend_from_slice(payload);
    let crc = checksum(&out);
    put_u32(&mut out[9..13], crc);
    out
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub kind: u8,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScanError {
    /// 偏移处 CRC/magic/长度不合法（半页写/垃圾尾巴的典型表现）。
    Corrupt(u64),
}

/// 从 `data` 的偏移 0 顺序扫描全部记录。
///
/// 遇到第一条损坏记录即停止（返回已读出的完整记录）。只追加协议保证
/// 任何已提交记录之后的损坏尾巴必然来自崩溃的半成品写入，可安全忽略。
pub fn scan_records(data: &[u8]) -> (Vec<(u64, Record)>, Option<ScanError>) {
    let mut recs = Vec::new();
    let mut pos = 0u64;
    while pos < data.len() as u64 {
        let available = data.len() as u64 - pos;
        if available < REC_HEADER_LEN as u64 {
            return (recs, Some(ScanError::Corrupt(pos)));
        }
        let p = pos as usize;
        let magic = get_u32(&data[p..p + 4]);
        if magic != REC_MAGIC {
            return (recs, Some(ScanError::Corrupt(pos)));
        }
        let kind = data[p + 4];
        if kind != KIND_LEAF && kind != KIND_ROOT {
            return (recs, Some(ScanError::Corrupt(pos)));
        }
        let payload_len = get_u32(&data[p + 5..p + 9]) as u64;
        let stored_crc = get_u32(&data[p + 9..p + 13]);
        let total = match (REC_HEADER_LEN as u64).checked_add(payload_len) {
            Some(t) => t,
            None => return (recs, Some(ScanError::Corrupt(pos))),
        };
        if total > available {
            return (recs, Some(ScanError::Corrupt(pos)));
        }
        let mut buf = data[p..p + total as usize].to_vec();
        put_u32(&mut buf[9..13], 0);
        if checksum(&buf) != stored_crc {
            return (recs, Some(ScanError::Corrupt(pos)));
        }
        let payload = buf.split_off(REC_HEADER_LEN);
        recs.push((pos, Record { kind, payload }));
        pos += total;
    }
    (recs, None)
}

// ---------- LEAF / ROOT payload ----------

pub fn encode_leaf(key: &[u8], value: &[u8]) -> Vec<u8> {
    let mut p = Vec::with_capacity(8 + key.len() + value.len());
    p.extend_from_slice(&(key.len() as u32).to_le_bytes());
    p.extend_from_slice(key);
    p.extend_from_slice(&(value.len() as u32).to_le_bytes());
    p.extend_from_slice(value);
    p
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Leaf {
    pub key: Vec<u8>,
    pub value: Vec<u8>,
}

pub fn decode_leaf(payload: &[u8]) -> Option<Leaf> {
    let mut cur = payload;
    let key = take_bytes(&mut cur)?;
    let value = take_bytes(&mut cur)?;
    if !cur.is_empty() {
        return None;
    }
    Some(Leaf { key, value })
}

/// ROOT 中的一项：键 + 指向 LEAF 记录的指针。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RootEntry {
    pub key: Vec<u8>,
    pub leaf: Ptr,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Root {
    pub generation: u64,
    pub entries: Vec<RootEntry>,
}

pub fn encode_root(root: &Root) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&root.generation.to_le_bytes());
    p.extend_from_slice(&(root.entries.len() as u32).to_le_bytes());
    for e in &root.entries {
        p.extend_from_slice(&(e.key.len() as u32).to_le_bytes());
        p.extend_from_slice(&e.key);
        p.extend_from_slice(&e.leaf.offset.to_le_bytes());
        p.extend_from_slice(&e.leaf.len.to_le_bytes());
        p.extend_from_slice(&e.leaf.crc.to_le_bytes());
    }
    p
}

pub fn decode_root(payload: &[u8]) -> Option<Root> {
    if payload.len() < 12 {
        return None;
    }
    let generation = get_u64(&payload[0..8]);
    let count = get_u32(&payload[8..12]) as usize;
    let mut cur = &payload[12..];
    let mut entries = Vec::with_capacity(count.min(1 << 20));
    for _ in 0..count {
        let key = take_bytes(&mut cur)?;
        if cur.len() < 20 {
            return None;
        }
        let offset = get_u64(&cur[0..8]);
        let len = get_u64(&cur[8..16]);
        let crc = get_u32(&cur[16..20]);
        cur = &cur[20..];
        entries.push(RootEntry {
            key,
            leaf: Ptr { offset, len, crc },
        });
    }
    if !cur.is_empty() {
        return None;
    }
    Some(Root {
        generation,
        entries,
    })
}

/// 从字节流前部取 `[u32 len][bytes]`，剩余部分通过切片缩短返回。
fn take_bytes(cur: &mut &[u8]) -> Option<Vec<u8>> {
    if cur.len() < 4 {
        return None;
    }
    let len = get_u32(&cur[0..4]) as usize;
    if cur.len() < 4 + len {
        return None;
    }
    let v = cur[4..4 + len].to_vec();
    *cur = &cur[4 + len..];
    Some(v)
}

// ---------- 超级块页 ----------

/// 把超级块编码为完整的一页（PAGE_SIZE 字节）。
pub fn encode_super_page(sb: &SuperBlock) -> Vec<u8> {
    let mut page = vec![0u8; PAGE_SIZE];
    put_u32(&mut page[0..4], SUPER_MAGIC);
    put_u16(&mut page[4..6], FORMAT_VERSION);
    put_u64(&mut page[6..14], sb.generation);
    put_u64(&mut page[14..22], sb.root.offset);
    put_u64(&mut page[22..30], sb.root.len);
    put_u32(&mut page[30..34], sb.root.crc);
    // 34..38 先保持 0，对整页求 CRC 后填入（CRC 覆盖完整 4096 字节）。
    // 页尾冗余尾戳：generation 与 root_crc 的副本跨越整页，保证半页写必被察觉。
    let t = SUPER_TRAILER_OFFSET;
    put_u32(&mut page[t..t + 4], SUPER_TRAILER_MAGIC);
    put_u64(&mut page[t + 4..t + 12], sb.generation);
    put_u32(&mut page[t + 12..t + 16], sb.root.crc);
    // t+16..t+18 保留为 0。
    let page_crc = checksum_full_page_zeroed(&page);
    put_u32(&mut page[34..38], page_crc);
    page
}

/// 对整页求 CRC，其中偏移 34..38 的 CRC 字段按 0 计。
fn checksum_full_page_zeroed(page: &[u8]) -> u32 {
    let mut c = crate::crc::Crc32::new();
    c.update(&page[..34]);
    c.update(&[0, 0, 0, 0]);
    c.update(&page[38..]);
    c.finalize()
}

/// 解析一页超级块。整页 CRC 不通过（含任何半页写）即返回 None，
/// 随后校验页尾尾戳与页首是否一致；任何一项失败都返回 None，
/// 调用方将尝试另一槽位。
pub fn decode_super_page(page: &[u8]) -> Option<SuperBlock> {
    if page.len() != PAGE_SIZE {
        return None;
    }
    let stored_crc = get_u32(&page[34..38]);
    if checksum_full_page_zeroed(page) != stored_crc {
        return None;
    }
    if get_u32(&page[0..4]) != SUPER_MAGIC {
        return None;
    }
    if get_u16(&page[4..6]) != FORMAT_VERSION {
        return None;
    }
    // 页尾冗余：魔数 + generation 副本 + root_crc 副本必须与页首一致。
    let t = SUPER_TRAILER_OFFSET;
    if get_u32(&page[t..t + 4]) != SUPER_TRAILER_MAGIC {
        return None;
    }
    let generation = get_u64(&page[6..14]);
    let root_crc = get_u32(&page[30..34]);
    if get_u64(&page[t + 4..t + 12]) != generation {
        return None;
    }
    if get_u32(&page[t + 12..t + 16]) != root_crc {
        return None;
    }
    let root = Ptr {
        offset: get_u64(&page[14..22]),
        len: get_u64(&page[22..30]),
        crc: root_crc,
    };
    // 一致性：空指针必须三项全零；非空指针长度至少容纳记录头。
    if root.is_empty() {
        if root.crc != 0 {
            return None;
        }
    } else if root.len < REC_HEADER_LEN as u64 {
        return None;
    }
    Some(SuperBlock { generation, root })
}

/// 代次 g 使用的槽位：偶数代次 → 0，奇数代次 → 1（交替）。
pub fn slot_for_generation(generation: u64) -> usize {
    (generation % 2) as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_roundtrip() {
        let bytes = encode_record(KIND_LEAF, &encode_leaf(b"k", b"v"));
        let (recs, err) = scan_records(&bytes);
        assert!(err.is_none());
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].0, 0);
        assert_eq!(recs[0].1.kind, KIND_LEAF);
        assert_eq!(
            decode_leaf(&recs[0].1.payload),
            Some(Leaf {
                key: b"k".to_vec(),
                value: b"v".to_vec()
            })
        );
    }

    #[test]
    fn scan_stops_at_torn_tail() {
        let mut bytes = encode_record(KIND_LEAF, &encode_leaf(b"a", b"1"));
        let good_len = bytes.len() as u64;
        bytes.extend_from_slice(&encode_record(KIND_LEAF, &encode_leaf(b"b", b"2")));
        bytes.truncate(bytes.len() - 3); // 撕掉最后 3 字节 → 半成品
        let (recs, err) = scan_records(&bytes);
        assert_eq!(recs.len(), 1);
        assert_eq!(err, Some(ScanError::Corrupt(good_len)));
    }

    #[test]
    fn root_roundtrip() {
        let r = Root {
            generation: 7,
            entries: vec![
                RootEntry {
                    key: b"a".to_vec(),
                    leaf: Ptr {
                        offset: 0,
                        len: 32,
                        crc: 111,
                    },
                },
                RootEntry {
                    key: b"bb".to_vec(),
                    leaf: Ptr {
                        offset: 64,
                        len: 40,
                        crc: 222,
                    },
                },
            ],
        };
        let decoded = decode_root(&encode_root(&r)).unwrap();
        assert_eq!(decoded, r);
    }

    #[test]
    fn super_page_roundtrip_and_corruption() {
        let sb = SuperBlock {
            generation: 9,
            root: Ptr {
                offset: 100,
                len: 60,
                crc: 0xDEAD_BEEF,
            },
        };
        let page = encode_super_page(&sb);
        assert_eq!(page.len(), PAGE_SIZE);
        assert_eq!(decode_super_page(&page), Some(sb));

        let mut bad = page.clone();
        bad[20] ^= 0xFF; // 翻转根偏移字段
        assert_eq!(decode_super_page(&bad), None);

        let zeros = vec![0u8; PAGE_SIZE];
        assert_eq!(decode_super_page(&zeros), None);

        let empty = SuperBlock {
            generation: 0,
            root: Ptr::empty(),
        };
        assert_eq!(decode_super_page(&encode_super_page(&empty)), Some(empty));
    }

    #[test]
    fn super_page_rejects_torn_writes() {
        let sb = SuperBlock {
            generation: 4,
            root: Ptr {
                offset: 100,
                len: 60,
                crc: 777,
            },
        };
        let page = encode_super_page(&sb);

        // 半页写：前 2048 字节是新页，后 2048 字节是另一页（模拟旧槽位残留）。
        let other = encode_super_page(&SuperBlock {
            generation: 2,
            root: Ptr {
                offset: 10,
                len: 40,
                crc: 55,
            },
        });
        let mut mixed = vec![0u8; PAGE_SIZE];
        mixed[..2048].copy_from_slice(&page[..2048]);
        mixed[2048..].copy_from_slice(&other[2048..]);
        // 混合页 CRC 必然不符。
        assert_eq!(decode_super_page(&mixed), None);

        // 即使有人重算 CRC 使混合页通过校验，页首代次(4)与页尾副本(2)不一致，
        // 尾戳仍会拒绝它。
        let mut forged = mixed.clone();
        let mut c = crate::crc::Crc32::new();
        c.update(&forged[..34]);
        c.update(&[0, 0, 0, 0]);
        c.update(&forged[38..]);
        put_u32(&mut forged[34..38], c.finalize());
        assert_eq!(decode_super_page(&forged), None);
    }

    #[test]
    fn pointer_bounds() {
        assert!(Ptr::empty().is_in_bounds(0));
        assert!(Ptr {
            offset: 0,
            len: 16,
            crc: 1
        }
        .is_in_bounds(16));
        assert!(!Ptr {
            offset: 10,
            len: 16,
            crc: 1
        }
        .is_in_bounds(20));
        assert!(!Ptr {
            offset: u64::MAX - 2,
            len: 16,
            crc: 1
        }
        .is_in_bounds(u64::MAX));
        assert!(!Ptr {
            offset: 0,
            len: 5,
            crc: 1
        }
        .is_in_bounds(100)); // 短于记录头
    }

    #[test]
    fn slot_alternation() {
        assert_eq!(slot_for_generation(0), 0);
        assert_eq!(slot_for_generation(1), 1);
        assert_eq!(slot_for_generation(2), 0);
        assert_eq!(slot_for_generation(3), 1);
    }
}
