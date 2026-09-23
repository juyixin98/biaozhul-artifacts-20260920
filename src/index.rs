//! 磁盘可扩展哈希索引核心。
//!
//! ## 目录与桶
//!
//! - 目录是长度 `2^global_depth` 的桶 id 数组，**多个目录槽可以引用同一个桶**
//!   （桶的 local_depth < global_depth 时）。
//! - 目录槽取散列的**高 global_depth 位**：`slot = hash >> (32-global_depth)`。
//!   该约定下，相同高位前缀的桶占据一段**连续且对齐**的槽块，块大小
//!   `2^(gd-ld)`。
//! - 桶页定长，最多容纳 `bucket_cap` 条记录。
//!
//! ## 插入与分裂链
//!
//! 桶满时，沿散列从高到低逐位分裂，直到待插入键所在的叶子桶有空间。
//! 若新键与满桶内记录在当前分裂位处于同一侧，本次分裂对新键“空转”（在另一
//! 侧产生一个空桶，局部深度仍 +1），随后继续分裂——这在强碰撞哈希下是常态。
//! 整条分裂链（含本次插入）是**一个原子事务**：最终的多个桶页先写入日志镜像，
//! 元数据落盘为提交点，之后才覆写正式桶页并重定向目录。若新键与全部现有记录
//! 的 32 位散列完全相同，任何分裂都无法分离，直接返回
//! [`IndexError::BucketCapacityExhausted`]；全碰撞（`const` 哈希）在第一次桶满
//! 时即命中。
//!
//! ## 删除、合并与目录收缩
//!
//! 删除后桶空且 ld>0：伙伴槽块 = 本块基址异或块大小。
//!
//! - 伙伴桶 ld 相同：等深合并，幸存桶 ld-1，回收空桶页；
//! - 伙伴桶更深（已再次分裂）：本层不可合并。
//!
//! 幸存桶若仍为空则继续向上合并。链结束后，若所有桶 ld < gd，目录减半
//! （只改 global_depth；磁盘高半槽成为过期数据，下次倍增覆盖）。
//!
//! ## 崩溃恢复
//!
//! 打开文件时若发现未清日志：分裂链按描述符表把镜像拷回正式桶页、必要时倍增
//! 目录、重定向所有最终槽块；合并则拷回幸存桶、重定向、回收。全部步骤幂等。

use std::path::{Path, PathBuf};

use crate::errors::{IndexError, Result};
use crate::hash::{hasher_for, HashKind, KeyHasher};
use crate::page::{
    decode_bucket, decode_desc_page, encode_bucket, encode_desc_page, encode_free_page,
    require_live_bucket, BucketImage, BucketSlot, Entry, Header, JournalMeta, JournalOp,
    SplitFinal, KEY_CAP, MAX_BUCKET_CAP, VAL_CAP,
};
use crate::pager::{
    Pager, FIRST_BUCKET_PAGE, JOURNAL_DESC_PAGE, JOURNAL_IMAGE_FIRST, JOURNAL_IMAGE_PAGES,
    JOURNAL_META_PAGE, MAX_GLOBAL_DEPTH, PAGE_SIZE,
};

const NONE_BUCKET: u32 = u32::MAX;

/// 分裂/合并过程中注入“崩溃”的点（故障注入点之前的数据都已 fsync）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    /// 分裂链：高水位已推进、目录已倍增并 fsync，但日志尚未提交。
    /// 此点崩溃不应丢任何已提交数据：重开后无日志、回收洞页、目录自洽。
    SplitBeforeCommit,
    /// 分裂链：日志已提交，但一个目录槽都还没改。
    SplitBeforeDirRedirect,
    /// 分裂链：目录重定向完成约一半槽后崩溃。
    SplitDuringDirRedirect,
    /// 分裂链：目录已改完并 fsync，但日志还没清除。
    SplitBeforeJournalClear,
    /// 合并：幸存桶与日志已提交，目录重定向到一半。
    MergeDuringDirRedirect,
}

pub struct Index {
    pager: Pager,
    path: PathBuf,
    header: Header,
    directory: Vec<u32>,
    hasher: Box<dyn KeyHasher>,
    fault: Option<Fault>,
    rwbuf: [u8; PAGE_SIZE],
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Stats {
    pub global_depth: u8,
    pub directory_size: usize,
    pub bucket_capacity: u32,
    pub live_buckets: usize,
    pub total_records: usize,
    pub free_pages: usize,
    pub hole_pages: usize,
    pub next_bucket_id: u32,
    pub hash: String,
    pub max_global_depth: u8,
    pub key_max_bytes: usize,
    pub value_max_bytes: usize,
    pub pending_journal: bool,
}

fn slot_of_hash(hash: u32, gd: u8) -> usize {
    if gd == 0 {
        0
    } else {
        (hash >> (32 - gd)) as usize
    }
}

impl Index {
    // ------------------------------------------------------------ 创建/打开

    pub fn create(path: impl Into<PathBuf>, bucket_cap: u32, kind: HashKind) -> Result<Index> {
        let path = path.into();
        if bucket_cap == 0 || bucket_cap > MAX_BUCKET_CAP {
            return Err(IndexError::Corrupt(format!(
                "桶容量必须在 1..={MAX_BUCKET_CAP} 之间，收到 {bucket_cap}"
            )));
        }
        let mut pager = Pager::create(&path)?;
        let header = Header {
            global_depth: 0,
            bucket_cap,
            hash_kind: kind,
            next_bucket_id: 1,
            free_head: NONE_BUCKET,
        };
        pager.write_page(0, &header.encode())?;
        let root = BucketImage {
            local_depth: 0,
            entries: Vec::new(),
        };
        pager.write_page(Pager::bucket_page(0), &encode_bucket(&root, bucket_cap)?)?;
        pager.sync()?;
        let mut idx = Index {
            pager,
            path,
            header,
            directory: vec![0],
            hasher: hasher_for(kind),
            fault: None,
            rwbuf: [0u8; PAGE_SIZE],
        };
        idx.assert_invariants(false);
        Ok(idx)
    }

    pub fn open(path: impl Into<PathBuf>) -> Result<Index> {
        Self::open_with(path, None)
    }

    pub fn open_with(path: impl Into<PathBuf>, expect_kind: Option<HashKind>) -> Result<Index> {
        let path = path.into();
        let mut pager = Pager::open(&path)?;
        let mut hbuf = [0u8; PAGE_SIZE];
        pager.read_page(0, &mut hbuf)?;
        let header = Header::decode(&hbuf)?;
        if let Some(want) = expect_kind {
            if want != header.hash_kind {
                return Err(IndexError::HashModeMismatch {
                    file: header.hash_kind.describe(),
                    requested: want.describe(),
                });
            }
        }
        let kind = header.hash_kind;
        let mut idx = Index {
            pager,
            path,
            header,
            directory: Vec::new(),
            hasher: hasher_for(kind),
            fault: None,
            rwbuf: [0u8; PAGE_SIZE],
        };
        idx.recover()?;
        idx.load_directory()?;
        idx.assert_invariants(true);
        Ok(idx)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn global_depth(&self) -> u8 {
        self.header.global_depth
    }
    pub fn bucket_capacity(&self) -> u32 {
        self.header.bucket_cap
    }
    pub fn hash_kind(&self) -> HashKind {
        self.header.hash_kind
    }

    // ------------------------------------------------------------ 故障注入

    pub fn inject_fault(&mut self, f: Fault) {
        self.fault = Some(f);
    }

    fn crash(&self, at: Fault) -> Result<()> {
        if self.fault == Some(at) {
            return Err(IndexError::Corrupt(format!(
                "【故障注入】在 {at:?} 处模拟进程崩溃：此前数据已 fsync，丢弃内存状态"
            )));
        }
        Ok(())
    }

    // ------------------------------------------------------------ 目录/桶 IO

    fn load_directory(&mut self) -> Result<()> {
        let n = 1usize << self.header.global_depth;
        self.directory = vec![0u32; n];
        for slot in 0..n {
            self.directory[slot] = self.read_dir_slot(slot)?;
        }
        Ok(())
    }

    fn read_dir_slot(&mut self, slot: usize) -> Result<u32> {
        let (pg, off) = Pager::dir_slot_loc(slot);
        self.pager.read_page(pg, &mut self.rwbuf)?;
        Ok(u32::from_le_bytes(
            self.rwbuf[off..off + 4].try_into().unwrap(),
        ))
    }

    fn write_dir_slot(&mut self, slot: usize, bucket: u32) -> Result<()> {
        let (pg, off) = Pager::dir_slot_loc(slot);
        self.pager.read_page(pg, &mut self.rwbuf)?;
        self.rwbuf[off..off + 4].copy_from_slice(&bucket.to_le_bytes());
        self.pager.write_page(pg, &self.rwbuf)?;
        Ok(())
    }

    /// 高位槽约定的倍增：旧槽 s -> 新槽 2s、2s+1。先读全部再写。
    fn disk_directory_double(&mut self, _old_gd: u8) -> Result<()> {
        let old_n = self.directory.len().max(1usize << _old_gd);
        // 恢复阶段内存目录可能为空：从磁盘读旧槽。
        let old_slots: Vec<u32> = if !self.directory.is_empty() && self.directory.len() == old_n {
            self.directory.clone()
        } else {
            (0..old_n)
                .map(|s| self.read_dir_slot(s))
                .collect::<Result<Vec<_>>>()?
        };
        for (s, &b) in old_slots.iter().enumerate() {
            self.write_dir_slot(2 * s, b)?;
            self.write_dir_slot(2 * s + 1, b)?;
        }
        Ok(())
    }

    fn read_bucket(&mut self, id: u32) -> Result<BucketImage> {
        self.pager
            .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
        require_live_bucket(&self.rwbuf, id)
    }

    fn write_bucket(&mut self, id: u32, b: &BucketImage) -> Result<()> {
        let buf = encode_bucket(b, self.header.bucket_cap)?;
        self.pager.write_page(Pager::bucket_page(id), &buf)?;
        Ok(())
    }

    fn alloc_bucket(&mut self) -> Result<u32> {
        if self.header.free_head != NONE_BUCKET {
            let id = self.header.free_head;
            self.pager
                .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
            self.header.free_head = match decode_bucket(&self.rwbuf)? {
                BucketSlot::Free(next) => next,
                BucketSlot::Live(_) => {
                    return Err(IndexError::Corrupt(format!(
                        "内部错误：空闲链表头 {id} 指向存活桶页"
                    )))
                }
                BucketSlot::Hole => {
                    return Err(IndexError::Corrupt(format!(
                        "内部错误：空闲链表头 {id} 指向洞页"
                    )))
                }
            };
            return Ok(id);
        }
        let id = self.header.next_bucket_id;
        self.header.next_bucket_id += 1;
        Ok(id)
    }

    fn free_bucket(&mut self, id: u32) -> Result<()> {
        self.pager
            .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
        if !matches!(decode_bucket(&self.rwbuf)?, BucketSlot::Live(_)) {
            return Ok(());
        }
        let buf = encode_free_page(self.header.free_head);
        self.pager.write_page(Pager::bucket_page(id), &buf)?;
        self.header.free_head = id;
        Ok(())
    }

    fn write_header(&mut self) -> Result<()> {
        self.pager.write_page(0, &self.header.encode())
    }

    // ------------------------------------------------------------ 崩溃恢复

    fn read_meta(&mut self) -> Result<Option<crate::page::RawJournalMeta>> {
        self.pager.read_page(JOURNAL_META_PAGE, &mut self.rwbuf)?;
        JournalMeta::decode_meta(&self.rwbuf)
    }

    fn recover(&mut self) -> Result<()> {
        let meta = match self.read_meta()? {
            Some(m) => m,
            None => {
                // 无活动日志：回收“高水位已推进但事务未提交”留下的洞页。
                self.reap_hole_pages()?;
                return Ok(());
            }
        };
        let crate::page::RawJournalMeta {
            op,
            b5,
            b6,
            nfinals,
            f8,
            f12,
            f16,
        } = meta;
        match op {
            1 => self.recover_split_chain(b5, b6, nfinals as usize)?,
            2 => self.recover_merge(f8, f12, f16 as usize, b6)?,
            other => {
                return Err(IndexError::Corrupt(format!(
                    "事务日志出现未知操作码 {other}"
                )))
            }
        }
        // 清日志。
        self.pager.write_page(
            JOURNAL_META_PAGE,
            &JournalMeta {
                op: JournalOp::None,
            }
            .encode(),
        )?;
        self.pager.sync()?;
        Ok(())
    }

    fn recover_split_chain(&mut self, old_gd: u8, new_gd: u8, nfinals: usize) -> Result<()> {
        if nfinals == 0 || nfinals as u64 > JOURNAL_IMAGE_PAGES {
            return Err(IndexError::Corrupt(format!(
                "分裂链日志 finals 数量 {nfinals} 非法"
            )));
        }
        // 读描述符表。
        let mut dbuf = [0u8; PAGE_SIZE];
        self.pager.read_page(JOURNAL_DESC_PAGE, &mut dbuf)?;
        let finals = decode_desc_page(&dbuf, nfinals)?;

        // 1) 高水位推进（覆盖所有最终桶 id）。
        let max_id = finals.iter().map(|f| f.bucket_id).max().unwrap();
        if self.header.next_bucket_id <= max_id {
            self.header.next_bucket_id = max_id + 1;
            self.write_header()?;
        }

        // 2) 镜像拷回正式桶页。
        for (i, f) in finals.iter().enumerate() {
            let mut buf = [0u8; PAGE_SIZE];
            self.pager
                .read_page(JOURNAL_IMAGE_FIRST + i as u64, &mut buf)?;
            // 校验镜像确为该桶（local_depth 与描述符一致）。
            let img = require_live_bucket(&buf, f.bucket_id)?;
            if img.local_depth != f.local_depth {
                return Err(IndexError::Corrupt(format!(
                    "日志镜像桶 {} 的 local_depth {} 与描述符 {} 不一致",
                    f.bucket_id, img.local_depth, f.local_depth
                )));
            }
            self.pager
                .write_page(Pager::bucket_page(f.bucket_id), &buf)?;
        }

        // 3) 目录倍增到 new_gd（幂等：只在当前 gd 较小时做）。
        while self.header.global_depth < new_gd {
            let cur = self.header.global_depth;
            self.disk_directory_double(cur)?;
            self.header.global_depth += 1;
            self.write_header()?;
        }

        // 4) 重定向每个最终桶的槽块（幂等）。
        for f in &finals {
            let start = f.anchor_slot as usize;
            let len = f.block_slots as usize;
            for i in 0..len {
                self.write_dir_slot(start + i, f.bucket_id)?;
            }
        }
        let _ = old_gd;
        self.write_header()?;
        self.pager.sync()?;
        Ok(())
    }

    fn recover_merge(
        &mut self,
        survivor: u32,
        removed: u32,
        anchor: usize,
        new_ld: u8,
    ) -> Result<()> {
        // 1) 幸存桶镜像（镜像区第 0 页）拷回。
        let mut buf = [0u8; PAGE_SIZE];
        self.pager.read_page(JOURNAL_IMAGE_FIRST, &mut buf)?;
        let img = require_live_bucket(&buf, survivor)?;
        if img.local_depth != new_ld {
            return Err(IndexError::Corrupt(format!(
                "合并日志镜像 local_depth {} 与记录 {new_ld} 不一致",
                img.local_depth
            )));
        }
        self.pager.write_page(Pager::bucket_page(survivor), &buf)?;

        // 2) 重定向整组。
        let group = 1usize << (self.header.global_depth - new_ld);
        for i in 0..group {
            self.write_dir_slot(anchor + i, survivor)?;
        }
        // 3) 回收被删桶（幂等）。
        self.free_bucket(removed)?;
        self.write_header()?;
        self.pager.sync()?;
        // 4) 目录收缩（幂等，读磁盘判定）。
        while self.can_shrink_disk()? {
            self.header.global_depth -= 1;
            self.write_header()?;
        }
        self.pager.sync()?;
        Ok(())
    }

    fn reap_hole_pages(&mut self) -> Result<()> {
        let mut changed = false;
        for id in (0..self.header.next_bucket_id).rev() {
            self.pager
                .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
            if matches!(decode_bucket(&self.rwbuf)?, BucketSlot::Hole) {
                let buf = encode_free_page(self.header.free_head);
                self.pager.write_page(Pager::bucket_page(id), &buf)?;
                self.header.free_head = id;
                changed = true;
            }
        }
        if changed {
            self.write_header()?;
            self.pager.sync()?;
        }
        Ok(())
    }

    // ------------------------------------------------------------ 查询

    pub fn get(&mut self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        if key.is_empty() {
            return Err(IndexError::EmptyKey);
        }
        let slot = slot_of_hash(self.hasher.hash(key), self.header.global_depth);
        let bid = self.directory[slot];
        let bucket = self.read_bucket(bid)?;
        Ok(bucket
            .entries
            .iter()
            .find(|e| e.key == key)
            .map(|e| e.value.clone()))
    }

    pub fn contains(&mut self, key: &[u8]) -> Result<bool> {
        Ok(self.get(key)?.is_some())
    }

    // ------------------------------------------------------------ 插入

    pub fn put(&mut self, key: &[u8], value: &[u8]) -> Result<()> {
        if key.is_empty() {
            return Err(IndexError::EmptyKey);
        }
        if key.len() > KEY_CAP {
            return Err(IndexError::KeyTooLong {
                len: key.len(),
                max: KEY_CAP,
            });
        }
        if value.len() > VAL_CAP {
            return Err(IndexError::ValueTooLong {
                len: value.len(),
                max: VAL_CAP,
            });
        }

        let hash = self.hasher.hash(key);
        let slot = slot_of_hash(hash, self.header.global_depth);
        let bid = self.directory[slot];
        let mut bucket = self.read_bucket(bid)?;

        // 更新。
        if let Some(e) = bucket.entries.iter_mut().find(|e| e.key == key) {
            e.value = value.to_vec();
            self.write_bucket(bid, &bucket)?;
            self.pager.sync()?;
            return Ok(());
        }
        // 有空位直接插入。
        if (bucket.entries.len() as u32) < self.header.bucket_cap {
            bucket.entries.push(Entry {
                key: key.to_vec(),
                value: value.to_vec(),
            });
            self.write_bucket(bid, &bucket)?;
            self.pager.sync()?;
            return Ok(());
        }

        // 桶满：先判断是否永远无法分离（与全部现有记录 32 位散列相同）。
        let h0 = self.hasher.hash(&bucket.entries[0].key);
        if h0 == hash
            && bucket
                .entries
                .iter()
                .all(|e| self.hasher.hash(&e.key) == h0)
        {
            return Err(IndexError::BucketCapacityExhausted {
                bucket: bid,
                reason: "桶内全部记录与待插入键的 32 位散列完全相同，分裂无法分离（哈希全碰撞）",
            });
        }
        if bucket.local_depth >= MAX_GLOBAL_DEPTH {
            return Err(IndexError::BucketCapacityExhausted {
                bucket: bid,
                reason: "已达最大全局深度，无法继续分裂",
            });
        }

        self.split_chain_and_insert(bid, slot, &bucket, hash, key, value)?;
        Ok(())
    }

    /// 沿散列位连续分裂直到新键所在叶子桶能容纳它；整链一个原子事务。
    #[allow(clippy::too_many_arguments)]
    fn split_chain_and_insert(
        &mut self,
        root_bid: u32,
        root_slot: usize,
        root_bucket: &BucketImage,
        incoming_hash: u32,
        new_key: &[u8],
        new_value: &[u8],
    ) -> Result<()> {
        let ld0 = root_bucket.local_depth;
        let old_gd = self.header.global_depth;

        // ---- 阶段 1：纯内存规划（不分配、不写盘）----
        // 被分裂的满桶在当前目录中占据一个连续块，先求出它的块基址（全局槽）。
        let root_block = 1usize << (old_gd - ld0);
        let root_base_global = root_slot - (root_slot % root_block);

        struct LeafPlan {
            is_root: bool,
            ld: u8,
            /// 定稿当时目录深度。
            gd: u8,
            /// 定稿当时深度下的**全局**槽前缀（块基址）。
            anchor_at_gd: usize,
            entries: Vec<Entry>,
        }
        let mut finals: Vec<LeafPlan> = Vec::new();
        let mut follow = root_bucket.entries.clone();
        // follow 当前桶在 cur_gd 下的全局块基址。
        let mut follow_base = root_base_global;
        let mut cur_ld = ld0;
        let mut cur_gd = old_gd;

        loop {
            let bit = 1u32 << (31 - cur_ld);
            let mut lo = Vec::new();
            let mut hi = Vec::new();
            for e in follow.drain(..) {
                if self.hasher.hash(&e.key) & bit != 0 {
                    hi.push(e);
                } else {
                    lo.push(e);
                }
            }
            let incoming_hi = incoming_hash & bit != 0;

            // 若分裂桶的 ld == gd，目录必须先倍增；倍增后全局基址左移 1 位。
            if cur_ld == cur_gd {
                cur_gd += 1;
                follow_base <<= 1;
            }
            // 分裂后 follow 桶块大小减半：lo/hi 各占一半。
            let half_block = 1usize << (cur_gd - (cur_ld + 1));
            let lo_base = follow_base;
            let hi_base = follow_base + half_block;

            if incoming_hi {
                finals.push(LeafPlan {
                    is_root: false,
                    ld: cur_ld + 1,
                    gd: cur_gd,
                    anchor_at_gd: lo_base,
                    entries: lo,
                });
                follow = hi;
                follow_base = hi_base;
            } else {
                finals.push(LeafPlan {
                    is_root: false,
                    ld: cur_ld + 1,
                    gd: cur_gd,
                    anchor_at_gd: hi_base,
                    entries: hi,
                });
                follow = lo;
                follow_base = lo_base;
            }
            cur_ld += 1;

            if (follow.len() + 1) as u32 <= self.header.bucket_cap {
                follow.push(Entry {
                    key: new_key.to_vec(),
                    value: new_value.to_vec(),
                });
                finals.push(LeafPlan {
                    is_root: true,
                    ld: cur_ld,
                    gd: cur_gd,
                    anchor_at_gd: follow_base,
                    entries: follow,
                });
                break;
            }
            if cur_ld >= MAX_GLOBAL_DEPTH {
                return Err(IndexError::BucketCapacityExhausted {
                    bucket: root_bid,
                    reason: "分裂至最大深度仍无法容纳（32 位散列内不可分离）",
                });
            }
        }

        let new_gd = cur_gd;
        let side_count = finals.len() - 1;

        // ---- 阶段 2：分配桶 id（follow 叶复用 root_bid）----
        let mut leaf_ids: Vec<u32> = Vec::with_capacity(finals.len());
        for lf in finals.iter() {
            if lf.is_root {
                leaf_ids.push(root_bid);
            } else {
                leaf_ids.push(self.alloc_bucket()?);
            }
        }

        // 计算最终目录中的槽块描述。
        let mut descriptors: Vec<SplitFinal> = Vec::with_capacity(finals.len());
        let mut images: Vec<[u8; PAGE_SIZE]> = Vec::with_capacity(finals.len());
        for (lf, &id) in finals.iter().zip(leaf_ids.iter()) {
            let shift = new_gd - lf.gd;
            let anchor = lf.anchor_at_gd << shift;
            let block_slots = 1usize << (new_gd - lf.ld);
            let img = BucketImage {
                local_depth: lf.ld,
                entries: lf.entries.clone(),
            };
            images.push(encode_bucket(&img, self.header.bucket_cap)?);
            descriptors.push(SplitFinal {
                bucket_id: id,
                local_depth: lf.ld,
                anchor_slot: anchor as u32,
                block_slots: block_slots as u32,
            });
        }

        // 描述符必须恰好覆盖原满桶块（换算到 new_gd 后的大小）。
        let root_block_new_gd = 1usize << (new_gd - ld0);
        debug_assert_eq!(
            descriptors
                .iter()
                .map(|d| d.block_slots as usize)
                .sum::<usize>(),
            root_block_new_gd
        );
        // 起点也必须对齐到原块。
        let root_base_new = root_base_global << (new_gd - old_gd);
        debug_assert_eq!(
            descriptors
                .iter()
                .map(|d| d.anchor_slot as usize)
                .min()
                .unwrap(),
            root_base_new
        );

        // ---- 阶段 3：持久化（崩溃安全协议）----
        // 3a) 提交前：只推进高水位 + 倍增目录，不写正式桶页。
        //     此点崩溃：无日志，目录是倍增后的自洽副本（槽成对相等），桶未变。
        self.write_header()?;
        self.pager.sync()?;
        while self.header.global_depth < new_gd {
            let cur = self.header.global_depth;
            self.disk_directory_double(cur)?;
            self.header.global_depth += 1;
            self.write_header()?;
        }
        self.pager.sync()?;
        // 提交点之前的故障窗口：此点崩溃时镜像/日志都还未写。
        self.crash(Fault::SplitBeforeCommit)?;

        // 3b) 写镜像 + 描述符 + 元数据（提交点）。
        for (i, buf) in images.iter().enumerate() {
            self.pager.write_page(JOURNAL_IMAGE_FIRST + i as u64, buf)?;
        }
        self.pager
            .write_page(JOURNAL_DESC_PAGE, &encode_desc_page(&descriptors))?;
        self.pager.write_page(
            JOURNAL_META_PAGE,
            &JournalMeta {
                op: JournalOp::SplitChain {
                    old_global_depth: old_gd,
                    new_global_depth: new_gd,
                    finals: descriptors.clone(),
                },
            }
            .encode(),
        )?;
        self.pager.sync()?;

        // 3c) 提交后覆写正式桶页（崩溃由日志重放）。
        for (d, buf) in descriptors.iter().zip(images.iter()) {
            self.pager
                .write_page(Pager::bucket_page(d.bucket_id), buf)?;
        }
        self.pager.sync()?;

        // 3d) 重定向目录槽块。
        self.crash(Fault::SplitBeforeDirRedirect)?;
        let total: usize = descriptors.iter().map(|d| d.block_slots as usize).sum();
        let mut done = 0usize;
        for d in &descriptors {
            let start = d.anchor_slot as usize;
            let len = d.block_slots as usize;
            for i in 0..len {
                self.write_dir_slot(start + i, d.bucket_id)?;
                done += 1;
                // 在总进度一半处注入一次崩溃（确定性）。
                if done == total / 2 {
                    self.crash(Fault::SplitDuringDirRedirect)?;
                }
            }
        }
        self.pager.sync()?;
        self.crash(Fault::SplitBeforeJournalClear)?;

        // 3e) 同步内存目录、清日志。
        self.load_directory()?;
        self.pager.write_page(
            JOURNAL_META_PAGE,
            &JournalMeta {
                op: JournalOp::None,
            }
            .encode(),
        )?;
        // 描述符/镜像页不必清零（以元数据页为准），但清掉更干净。
        self.pager.sync()?;
        let _ = side_count;
        self.assert_invariants(false);
        Ok(())
    }

    // ------------------------------------------------------------ 删除

    /// 删除键；返回该键之前是否存在。
    pub fn delete(&mut self, key: &[u8]) -> Result<bool> {
        if key.is_empty() {
            return Err(IndexError::EmptyKey);
        }
        let hash = self.hasher.hash(key);
        let slot = slot_of_hash(hash, self.header.global_depth);
        let bid = self.directory[slot];
        let mut bucket = self.read_bucket(bid)?;
        let pos = match bucket.entries.iter().position(|e| e.key == key) {
            Some(p) => p,
            None => return Ok(false),
        };
        bucket.entries.remove(pos);
        self.write_bucket(bid, &bucket)?;
        self.pager.sync()?;

        if bucket.entries.is_empty() {
            self.try_merge_chain(bid)?;
            while self.can_shrink() {
                self.header.global_depth -= 1;
                self.write_header()?;
                self.pager.sync()?;
                self.directory.truncate(1usize << self.header.global_depth);
            }
        }
        self.assert_invariants(false);
        Ok(true)
    }

    fn try_merge_chain(&mut self, start: u32) -> Result<()> {
        let mut empty = start;
        loop {
            let img = self.read_bucket(empty)?;
            let ld = img.local_depth;
            if ld == 0 {
                return Ok(());
            }
            let gd = self.header.global_depth;
            let block = 1usize << (gd - ld);
            let some_slot = match self.directory.iter().position(|b| *b == empty) {
                Some(s) => s,
                None => return Ok(()),
            };
            let base = some_slot - (some_slot % block);
            let buddy_base = base ^ block;
            let buddy_bid = self.read_dir_slot(buddy_base)?;
            if buddy_bid == empty {
                return Ok(());
            }
            let buddy_img = self.read_bucket(buddy_bid)?;
            if buddy_img.local_depth > ld {
                return Ok(());
            }
            debug_assert_eq!(buddy_img.local_depth, ld);

            let aligned = base.min(buddy_base);
            let mut survivor_img = buddy_img;
            survivor_img.local_depth = ld - 1;
            let still_empty = survivor_img.entries.is_empty();
            self.merge_one(buddy_bid, empty, aligned, &survivor_img)?;
            if still_empty {
                empty = buddy_bid;
                continue;
            }
            return Ok(());
        }
    }

    fn merge_one(
        &mut self,
        survivor: u32,
        removed: u32,
        aligned: usize,
        survivor_img: &BucketImage,
    ) -> Result<()> {
        let new_ld = survivor_img.local_depth;
        let group = 1usize << (self.header.global_depth - new_ld);
        debug_assert!(group >= 2);
        debug_assert_eq!(aligned % group, 0);

        let buf = encode_bucket(survivor_img, self.header.bucket_cap)?;
        // 1) 镜像 + 元数据（提交点），正式页提交后才写。
        self.pager.write_page(JOURNAL_IMAGE_FIRST, &buf)?;
        self.pager.write_page(
            JOURNAL_META_PAGE,
            &JournalMeta {
                op: JournalOp::Merge {
                    survivor,
                    removed,
                    anchor_slot: aligned,
                    local_depth_before: new_ld, // b6 直接记录幸存桶合并后的 ld
                },
            }
            .encode(),
        )?;
        self.pager.sync()?;

        // 2) 覆写幸存桶正式页。
        self.pager.write_page(Pager::bucket_page(survivor), &buf)?;
        self.pager.sync()?;

        // 3) 重定向整组（半组处注入崩溃）。
        for i in 0..group {
            self.write_dir_slot(aligned + i, survivor)?;
            if i + 1 == group / 2 {
                self.crash(Fault::MergeDuringDirRedirect)?;
            }
        }
        self.pager.sync()?;

        // 4) 回收、更新头、同步内存槽、清日志。
        self.free_bucket(removed)?;
        self.write_header()?;
        self.pager.sync()?;
        for i in 0..group {
            self.directory[aligned + i] = survivor;
        }
        self.pager.write_page(
            JOURNAL_META_PAGE,
            &JournalMeta {
                op: JournalOp::None,
            }
            .encode(),
        )?;
        self.pager.sync()?;
        Ok(())
    }

    fn can_shrink(&self) -> bool {
        let gd = self.header.global_depth;
        if gd == 0 {
            return false;
        }
        let half = 1usize << (gd - 1);
        for s in 0..half {
            if self.directory[s] != self.directory[s + half] {
                return false;
            }
        }
        true
    }

    fn can_shrink_disk(&mut self) -> Result<bool> {
        let gd = self.header.global_depth;
        if gd == 0 {
            return Ok(false);
        }
        let half = 1usize << (gd - 1);
        for s in 0..half {
            if self.read_dir_slot(s)? != self.read_dir_slot(s + half)? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    // ------------------------------------------------------------ 统计/快照

    pub fn stats(&mut self) -> Result<Stats> {
        let mut live = 0usize;
        let mut records = 0usize;
        let mut free = 0usize;
        let mut holes = 0usize;
        for id in 0..self.header.next_bucket_id {
            self.pager
                .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
            match decode_bucket(&self.rwbuf)? {
                BucketSlot::Live(b) => {
                    live += 1;
                    records += b.entries.len();
                }
                BucketSlot::Free(_) => free += 1,
                BucketSlot::Hole => holes += 1,
            }
        }
        Ok(Stats {
            global_depth: self.header.global_depth,
            directory_size: self.directory.len(),
            bucket_capacity: self.header.bucket_cap,
            live_buckets: live,
            total_records: records,
            free_pages: free,
            hole_pages: holes,
            next_bucket_id: self.header.next_bucket_id,
            hash: self.header.hash_kind.describe(),
            max_global_depth: MAX_GLOBAL_DEPTH,
            key_max_bytes: KEY_CAP,
            value_max_bytes: VAL_CAP,
            pending_journal: self.read_meta()?.is_some(),
        })
    }

    pub fn snapshot(&mut self) -> Result<Vec<(Vec<u8>, Vec<u8>)>> {
        let mut out = Vec::new();
        for id in 0..self.header.next_bucket_id {
            self.pager
                .read_page(Pager::bucket_page(id), &mut self.rwbuf)?;
            if let BucketSlot::Live(b) = decode_bucket(&self.rwbuf)? {
                for e in b.entries {
                    out.push((e.key, e.value));
                }
            }
        }
        Ok(out)
    }

    pub fn directory(&self) -> &[u32] {
        &self.directory
    }

    pub fn bucket_debug(&mut self, id: u32) -> Result<(u8, usize)> {
        let b = self.read_bucket(id)?;
        Ok((b.local_depth, b.entries.len()))
    }

    // ------------------------------------------------------------ 不变量

    pub fn assert_invariants(&mut self, after_recovery: bool) {
        let gd = self.header.global_depth;
        let n = 1usize << gd;
        assert_eq!(self.directory.len(), n, "目录长度必须为 2^gd");

        let mut refcount = vec![0u32; self.header.next_bucket_id as usize];
        for &b in &self.directory {
            assert!(b != NONE_BUCKET, "目录槽不能为空引用");
            refcount[b as usize] += 1;
        }
        for id in 0..self.header.next_bucket_id {
            self.pager
                .read_page(Pager::bucket_page(id), &mut self.rwbuf)
                .expect("读桶页");
            match decode_bucket(&self.rwbuf).expect("解码桶页") {
                BucketSlot::Live(b) => {
                    let rc = refcount[id as usize];
                    assert!(rc > 0, "存活桶 {id} 未被任何目录槽引用");
                    assert!(
                        b.local_depth <= gd,
                        "桶 {id} ld {} > gd {gd}",
                        b.local_depth
                    );
                    assert!(
                        (b.entries.len() as u32) <= MAX_BUCKET_CAP,
                        "桶 {id} 记录数超过硬上限"
                    );
                    let expect_group = 1usize << (gd - b.local_depth);
                    assert_eq!(
                        rc as usize, expect_group,
                        "桶 {id}(ld={}) 引用槽数应为 {expect_group}，实际 {rc}",
                        b.local_depth
                    );
                    let slots: Vec<usize> = self
                        .directory
                        .iter()
                        .enumerate()
                        .filter(|(_, x)| **x == id)
                        .map(|(s, _)| s)
                        .collect();
                    let base = slots[0];
                    assert_eq!(base % expect_group, 0, "桶 {id} 槽块未对齐");
                    assert_eq!(slots, (base..base + expect_group).collect::<Vec<_>>());
                    let mut keys: Vec<&[u8]> = b.entries.iter().map(|e| e.key.as_slice()).collect();
                    let klen = keys.len();
                    keys.sort();
                    keys.dedup();
                    assert_eq!(keys.len(), klen, "桶 {id} 内存在重复键");
                }
                BucketSlot::Free(_) => {
                    assert_eq!(refcount[id as usize], 0, "空闲桶 {id} 不应被引用")
                }
                BucketSlot::Hole => {
                    assert_eq!(refcount[id as usize], 0, "洞页 {id} 不应被引用")
                }
            }
        }

        let snap = self.snapshot().expect("snapshot");
        let mut all: Vec<&[u8]> = snap.iter().map(|(k, _)| k.as_slice()).collect();
        let total = all.len();
        all.sort();
        all.dedup();
        assert_eq!(all.len(), total, "全局存在重复键");

        if after_recovery {
            assert!(
                self.read_meta().expect("读日志").is_none(),
                "恢复后日志必须为空"
            );
        }
    }
}

#[allow(dead_code)]
fn _unused_anchors() -> (u64, u64) {
    (FIRST_BUCKET_PAGE, JOURNAL_DESC_PAGE)
}
