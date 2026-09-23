//! 页内结构编解码：索引头、桶页、事务日志。
//!
//! 多字节整数一律小端。每页末尾 4 字节为 CRC-32（覆盖前面所有字节）。

use crate::crc32::crc32;
use crate::errors::{IndexError, Result};
use crate::hash::HashKind;
use crate::pager::{
    BUCKET_HEADER_LEN, BUCKET_MAGIC, CRC_SIZE, FREE_MAGIC, HEADER_MAGIC, JOURNAL_MAGIC, PAGE_SIZE,
};

/// 键的最大字节长度。
pub const KEY_CAP: usize = 128;
/// 值的最大字节长度。
pub const VAL_CAP: usize = 512;
/// 每条记录的定长步长：u32 键长 + 键区 + u32 值长 + 值区。
pub const STRIDE: usize = 4 + KEY_CAP + 4 + VAL_CAP;
/// 48B 桶头 + 最多 6 条记录 <= 4096（实际 48 + 6*648 = 3936）。
pub const MAX_BUCKET_CAP: u32 = ((PAGE_SIZE - CRC_SIZE - BUCKET_HEADER_LEN) / STRIDE) as u32;

/// 一条桶内记录（定长槽：u32 键长 + 128B 键 + 512B 值）。
#[derive(Debug, Clone)]
pub struct Entry {
    pub key: Vec<u8>,
    pub value: Vec<u8>,
}

/// 桶页在内存中的镜像。
#[derive(Debug, Clone)]
pub struct BucketImage {
    pub local_depth: u8,
    pub entries: Vec<Entry>,
}

/// 桶页位置的三种状态。
#[derive(Debug)]
pub enum BucketSlot {
    /// 存活桶。
    Live(BucketImage),
    /// 已回收到空闲链表的页；u32 为链上下一个 id。
    Free(u32),
    /// 从未写过的洞页（高水位已推进但提交点前崩溃时可能短暂出现）。
    Hole,
}

/// 索引头（仅占用第 0 页前 36 字节，其余保留）。
#[derive(Debug, Clone)]
pub struct Header {
    pub global_depth: u8,
    pub bucket_cap: u32,
    pub hash_kind: HashKind,
    /// 下一个从未分配过的桶 id（高水位）。
    pub next_bucket_id: u32,
    /// 空闲桶页链表头；u32::MAX 表示无空闲页。
    pub free_head: u32,
}

// ---------------------------------------------------------------- 小端工具

fn put_u32(buf: &mut [u8], at: usize, v: u32) {
    buf[at..at + 4].copy_from_slice(&v.to_le_bytes());
}
fn get_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(buf[at..at + 4].try_into().unwrap())
}

/// 页尾 CRC：校验整页（除最后 4 字节）。
pub fn check_page_crc(page: &[u8], what: &str) -> Result<u32> {
    let stored = get_u32(page, PAGE_SIZE - CRC_SIZE);
    let actual = crc32(&page[..PAGE_SIZE - CRC_SIZE]);
    if stored != actual {
        return Err(IndexError::Corrupt(format!(
            "{what} CRC 校验失败：存储值 {stored:#010x}，计算值 {actual:#010x}（可能发生撕裂写或文件损坏）"
        )));
    }
    Ok(stored)
}

fn stamp_crc(page: &mut [u8]) {
    let c = crc32(&page[..PAGE_SIZE - CRC_SIZE]);
    put_u32(page, PAGE_SIZE - CRC_SIZE, c);
}

// ---------------------------------------------------------------- 索引头

impl Header {
    /// 头字段布局：
    /// 0..4 magic, 4 version, 5 global_depth, 8 bucket_cap,
    /// 12 next_bucket_id, 16 free_head, 20..23 hash_kind(3B 编码), 其余保留。
    pub fn encode(&self) -> [u8; PAGE_SIZE] {
        let mut p = [0u8; PAGE_SIZE];
        put_u32(&mut p, 0, HEADER_MAGIC);
        p[4] = crate::pager::FORMAT_VERSION;
        p[5] = self.global_depth;
        put_u32(&mut p, 8, self.bucket_cap);
        put_u32(&mut p, 12, self.next_bucket_id);
        put_u32(&mut p, 16, self.free_head);
        let hk = self.hash_kind.encode();
        p[20..23].copy_from_slice(&hk);
        stamp_crc(&mut p);
        p
    }

    pub fn decode(page: &[u8]) -> Result<Header> {
        check_page_crc(page, "索引头")?;
        if get_u32(page, 0) != HEADER_MAGIC {
            return Err(IndexError::Corrupt("索引头 magic 不匹配".into()));
        }
        if page[4] != crate::pager::FORMAT_VERSION {
            return Err(IndexError::Corrupt(format!(
                "不支持的格式版本 {}（本程序支持版本 {}）",
                page[4],
                crate::pager::FORMAT_VERSION
            )));
        }
        let global_depth = page[5];
        let bucket_cap = get_u32(page, 8);
        let next_bucket_id = get_u32(page, 12);
        let free_head = get_u32(page, 16);
        let mut hk = [0u8; 3];
        hk.copy_from_slice(&page[20..23]);
        let hash_kind = HashKind::decode(hk)?;
        if bucket_cap == 0 || bucket_cap > MAX_BUCKET_CAP {
            return Err(IndexError::Corrupt(format!(
                "头中桶容量 {bucket_cap} 超出允许范围 1..={MAX_BUCKET_CAP}"
            )));
        }
        Ok(Header {
            global_depth,
            bucket_cap,
            hash_kind,
            next_bucket_id,
            free_head,
        })
    }
}

// ---------------------------------------------------------------- 桶页

/// 桶页头部布局（共 48 字节）：
/// 0..4 magic(=BUCK 活页 / =FREE 空闲页), 4 local_depth, 8 count(实际记录数)。
pub fn encode_bucket(b: &BucketImage, cap: u32) -> Result<[u8; PAGE_SIZE]> {
    // 分裂的中间态允许“仍满”的桶（cap 条记录全落在下一位的同一侧）：
    // 它将由后续插入触发继续分裂。因此这里只按页面可容纳的硬上限拦截，
    // 软容量 cap 由 Index::put 的分裂循环保证对用户不可见。
    if b.entries.len() > MAX_BUCKET_CAP as usize {
        return Err(IndexError::Corrupt(format!(
            "桶内记录数 {} 超过页面硬上限 {}",
            b.entries.len(),
            MAX_BUCKET_CAP
        )));
    }
    let _ = cap;
    let mut p = [0u8; PAGE_SIZE];
    put_u32(&mut p, 0, BUCKET_MAGIC);
    p[4] = b.local_depth;
    put_u32(&mut p, 8, b.entries.len() as u32);
    // 记录从偏移 48 开始紧密排列
    for (i, e) in b.entries.iter().enumerate() {
        if e.key.is_empty() {
            return Err(IndexError::EmptyKey);
        }
        if e.key.len() > KEY_CAP {
            return Err(IndexError::KeyTooLong {
                len: e.key.len(),
                max: KEY_CAP,
            });
        }
        if e.value.len() > VAL_CAP {
            return Err(IndexError::ValueTooLong {
                len: e.value.len(),
                max: VAL_CAP,
            });
        }
        let base = BUCKET_HEADER_LEN + i * STRIDE;
        put_u32(&mut p, base, e.key.len() as u32);
        p[base + 4..base + 4 + e.key.len()].copy_from_slice(&e.key);
        let voff = base + 4 + KEY_CAP;
        put_u32(&mut p, voff, e.value.len() as u32);
        p[voff + 4..voff + 4 + e.value.len()].copy_from_slice(&e.value);
    }
    stamp_crc(&mut p);
    Ok(p)
}

/// 解析桶页位置：活桶 / FREE 链页 / 从未写过的洞页。
///
/// 全零页视为洞页（稀疏文件读洞返回零）；magic 非零但 CRC 不符一律报损坏，
/// 绝不把撕裂写静默当成洞。
pub fn decode_bucket(page: &[u8]) -> Result<BucketSlot> {
    let magic = get_u32(page, 0);
    if magic == 0 {
        // 洞页必须整体为零（包括 CRC 位置）。
        if page.iter().all(|&b| b == 0) {
            return Ok(BucketSlot::Hole);
        }
        return Err(IndexError::Corrupt(
            "桶页 magic 为零但页内存在非零字节（撕裂写）".into(),
        ));
    }
    check_page_crc(page, "桶页")?;
    if magic == FREE_MAGIC {
        return Ok(BucketSlot::Free(get_u32(page, 4)));
    }
    if magic != BUCKET_MAGIC {
        return Err(IndexError::Corrupt(format!(
            "桶页 magic 异常：{magic:#010x}（期望 {BUCKET_MAGIC:#010x}）"
        )));
    }
    let local_depth = page[4];
    let count = get_u32(page, 8) as usize;
    if count > MAX_BUCKET_CAP as usize {
        return Err(IndexError::Corrupt(format!("桶头记录数 {count} 异常")));
    }
    let mut entries = Vec::with_capacity(count);
    for i in 0..count {
        let base = BUCKET_HEADER_LEN + i * STRIDE;
        let klen = get_u32(page, base) as usize;
        if klen == 0 || klen > KEY_CAP {
            return Err(IndexError::Corrupt(format!("桶内键长 {klen} 异常")));
        }
        let key = page[base + 4..base + 4 + klen].to_vec();
        let voff = base + 4 + KEY_CAP;
        let vlen = get_u32(page, voff) as usize;
        if vlen > VAL_CAP {
            return Err(IndexError::Corrupt(format!("桶内值长 {vlen} 异常")));
        }
        let value = page[voff + 4..voff + 4 + vlen].to_vec();
        entries.push(Entry { key, value });
    }
    Ok(BucketSlot::Live(BucketImage {
        local_depth,
        entries,
    }))
}

/// 便利函数：要求该位置必须是活桶。
pub fn require_live_bucket(page: &[u8], id: u32) -> Result<BucketImage> {
    match decode_bucket(page)? {
        BucketSlot::Live(b) => Ok(b),
        BucketSlot::Free(_) => Err(IndexError::Corrupt(format!(
            "内部错误：目录引用的桶 {id} 是空闲页"
        ))),
        BucketSlot::Hole => Err(IndexError::Corrupt(format!(
            "内部错误：目录引用的桶 {id} 是未写入的洞页（上次结构变更未完成且无日志可恢复）"
        ))),
    }
}

/// 空闲页编码：magic=FREE，offset 4 存下一个空闲桶 id（u32::MAX 为链尾）。
pub fn encode_free_page(next: u32) -> [u8; PAGE_SIZE] {
    let mut p = [0u8; PAGE_SIZE];
    put_u32(&mut p, 0, FREE_MAGIC);
    put_u32(&mut p, 4, next);
    stamp_crc(&mut p);
    p
}

/// 解析空闲页，返回链上的下一个桶 id。若该位置根本没有写过（全零，CRC 不为 0），
/// 调用方应把它当作“未分配的高水位页”，而不是空闲页。
pub fn decode_free_next(page: &[u8]) -> Result<u32> {
    check_page_crc(page, "空闲桶页")?;
    if get_u32(page, 0) != FREE_MAGIC {
        return Err(IndexError::Corrupt("期望 FREE 空闲页，magic 不符".into()));
    }
    Ok(get_u32(page, 4))
}

// ---------------------------------------------------------------- 事务日志

/// 一次分裂链的结果：原满桶沿散列位连续分裂 `steps` 次，产生
/// `steps + 1` 个**最终桶**（其中一个复用原桶 id，其余为新桶）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplitFinal {
    /// 最终桶 id（第一个为原桶，其余为本次新分配）。
    pub bucket_id: u32,
    /// 该最终桶的局部深度（= 起始局部深度 + steps）。
    pub local_depth: u8,
    /// 该桶在新目录中占据的连续块起始槽（已对 new_global_depth 对齐）。
    pub anchor_slot: u32,
    /// 该桶占据的槽块大小（2 的幂）。
    pub block_slots: u32,
}

/// 日志操作类型。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalOp {
    /// 分裂链（一次插入可能需要连续多次分裂，整体作为一个原子事务）。
    SplitChain {
        /// 事务前的全局深度。
        old_global_depth: u8,
        /// 事务后的全局深度。
        new_global_depth: u8,
        /// 最终桶描述（含镜像页序号：按数组顺序放在镜像区）。
        finals: Vec<SplitFinal>,
    },
    /// 等深合并：幸存桶采用新内容（ld-1），被回收桶 free。
    /// `local_depth_before` 字段在元数据中实际记录的是**合并后幸存桶的 ld**。
    Merge {
        survivor: u32,
        removed: u32,
        anchor_slot: usize,
        local_depth_before: u8,
    },
    None,
}

/// 元数据页小字段布局：
/// 0..4 magic；4 op(1=chain,2=merge)；5 old_gd；6 new_gd / merge:ld_before；
/// 7 finals 数量；8 survivor 或 finals[0].bucket；12 removed；16 anchor。
pub struct JournalMeta {
    pub op: JournalOp,
}

/// 描述符页每项 16 字节：u32 bucket_id, u32 anchor_slot, u32 block_slots, u8 ld。
pub const DESC_STRIDE: usize = 16;

/// 日志元数据页解码出的原始字段（含义随 `op` 而不同，见 [`JournalOp`]）。
#[derive(Debug, Clone, Copy)]
pub struct RawJournalMeta {
    /// 操作码：1=分裂链，2=合并。
    pub op: u8,
    /// 分裂链=old_global_depth。
    pub b5: u8,
    /// 分裂链=new_global_depth；合并=幸存桶合并后的 local_depth。
    pub b6: u8,
    /// 分裂链=最终桶数量。
    pub nfinals: u32,
    /// 分裂链保留；合并=survivor。
    pub f8: u32,
    /// 合并=removed。
    pub f12: u32,
    /// 合并=anchor_slot。
    pub f16: u32,
}

impl JournalMeta {
    pub fn encode(&self) -> [u8; PAGE_SIZE] {
        let mut p = [0u8; PAGE_SIZE];
        match &self.op {
            JournalOp::None => return p,
            JournalOp::SplitChain {
                old_global_depth,
                new_global_depth,
                finals,
            } => {
                put_u32(&mut p, 0, JOURNAL_MAGIC);
                p[4] = 1;
                p[5] = *old_global_depth;
                p[6] = *new_global_depth;
                p[7] = finals.len() as u8;
            }
            JournalOp::Merge {
                survivor,
                removed,
                anchor_slot,
                local_depth_before,
            } => {
                put_u32(&mut p, 0, JOURNAL_MAGIC);
                p[4] = 2;
                p[6] = *local_depth_before;
                put_u32(&mut p, 8, *survivor);
                put_u32(&mut p, 12, *removed);
                put_u32(&mut p, 16, *anchor_slot as u32);
            }
        }
        stamp_crc(&mut p);
        p
    }

    /// 解析元数据；配合描述符页/镜像页由上层完整恢复。无活动日志返回 None。
    pub fn decode_meta(page: &[u8]) -> Result<Option<RawJournalMeta>> {
        if get_u32(page, 0) == 0 {
            return Ok(None);
        }
        check_page_crc(page, "事务日志")?;
        if get_u32(page, 0) != JOURNAL_MAGIC {
            return Err(IndexError::Corrupt("事务日志 magic 不匹配".into()));
        }
        Ok(Some(RawJournalMeta {
            op: page[4],
            b5: page[5],
            b6: page[6],
            nfinals: page[7] as u32,
            f8: get_u32(page, 8),
            f12: get_u32(page, 12),
            f16: get_u32(page, 16),
        }))
    }
}

/// 编码描述符页（finals 表）。
pub fn encode_desc_page(finals: &[SplitFinal]) -> [u8; PAGE_SIZE] {
    assert!(finals.len() * DESC_STRIDE < PAGE_SIZE - CRC_SIZE);
    let mut p = [0u8; PAGE_SIZE];
    for (i, f) in finals.iter().enumerate() {
        let b = i * DESC_STRIDE;
        put_u32(&mut p, b, f.bucket_id);
        put_u32(&mut p, b + 4, f.anchor_slot);
        put_u32(&mut p, b + 8, f.block_slots);
        p[b + 12] = f.local_depth;
    }
    stamp_crc(&mut p);
    p
}

/// 解码描述符页。
pub fn decode_desc_page(page: &[u8], count: usize) -> Result<Vec<SplitFinal>> {
    check_page_crc(page, "事务日志描述符")?;
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let b = i * DESC_STRIDE;
        out.push(SplitFinal {
            bucket_id: get_u32(page, b),
            anchor_slot: get_u32(page, b + 4),
            block_slots: get_u32(page, b + 8),
            local_depth: page[b + 12],
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_roundtrip() {
        let h = Header {
            global_depth: 3,
            bucket_cap: 4,
            hash_kind: HashKind::LowMod(3),
            next_bucket_id: 7,
            free_head: u32::MAX,
        };
        let p = h.encode();
        let h2 = Header::decode(&p).unwrap();
        assert_eq!(h2.global_depth, 3);
        assert_eq!(h2.bucket_cap, 4);
        assert_eq!(h2.hash_kind, HashKind::LowMod(3));
        assert_eq!(h2.next_bucket_id, 7);
        assert_eq!(h2.free_head, u32::MAX);
    }

    #[test]
    fn bucket_roundtrip_and_capacity_math() {
        let entries: Vec<_> = (0..MAX_BUCKET_CAP as usize)
            .map(|i| Entry {
                key: format!("k{i}").into_bytes(),
                value: vec![i as u8; VAL_CAP],
            })
            .collect();
        let b = BucketImage {
            local_depth: 2,
            entries,
        };
        let p = encode_bucket(&b, MAX_BUCKET_CAP).unwrap();
        let back = match decode_bucket(&p).unwrap() {
            BucketSlot::Live(b) => b,
            other => panic!("期望活桶，得到 {other:?}"),
        };
        assert_eq!(back.local_depth, 2);
        assert_eq!(back.entries.len(), MAX_BUCKET_CAP as usize);
        assert_eq!(back.entries[3].key, b"k3");
        assert_eq!(back.entries[3].value.len(), VAL_CAP);
    }

    #[test]
    fn crc_detects_torn_write() {
        let h = Header {
            global_depth: 1,
            bucket_cap: 4,
            hash_kind: HashKind::Fx,
            next_bucket_id: 1,
            free_head: u32::MAX,
        };
        let mut p = h.encode();
        p[5] = 9; // 篡改 global_depth
        assert!(Header::decode(&p).is_err());
    }

    #[test]
    fn journal_meta_roundtrip_and_none() {
        let blank = JournalMeta {
            op: JournalOp::None,
        }
        .encode();
        assert!(JournalMeta::decode_meta(&blank).unwrap().is_none());

        let finals = vec![
            SplitFinal {
                bucket_id: 0,
                local_depth: 3,
                anchor_slot: 0,
                block_slots: 2,
            },
            SplitFinal {
                bucket_id: 3,
                local_depth: 3,
                anchor_slot: 2,
                block_slots: 2,
            },
            SplitFinal {
                bucket_id: 4,
                local_depth: 3,
                anchor_slot: 4,
                block_slots: 4,
            },
        ];
        let meta = JournalMeta {
            op: JournalOp::SplitChain {
                old_global_depth: 2,
                new_global_depth: 3,
                finals: finals.clone(),
            },
        }
        .encode();
        let m = JournalMeta::decode_meta(&meta).unwrap().unwrap();
        assert_eq!((m.op, m.b5, m.b6, m.nfinals as usize), (1, 2, 3, 3));

        let desc = encode_desc_page(&finals);
        let back = decode_desc_page(&desc, 3).unwrap();
        assert_eq!(back, finals);
    }
}
