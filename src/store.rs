//! 存储引擎：append-only 数据文件 + 交替双页超级块。
//!
//! 这一层完全通过 [`Storage`] 抽象访问介质，因此同一套引擎代码既能跑在
//! 真实文件上，也能跑在可注入断电/半页写的模拟磁盘上。
//!
//! ## 写入（提交协议，同步边界见 `format` 模块文档）
//!
//! 1. 为当前快照中的每个键写一条 LEAF 记录，fsync 数据文件；
//! 2. 写 ROOT 快照记录，fsync 数据文件（**先同步数据，再发布根**）；
//! 3. 在交替槽位整页写新超级块，fsync 超级块文件。
//!
//! 内存状态只在第 3 步成功后才更新，因此故障后旧仓库镜像仍然自洽。
//!
//! ## 恢复
//!
//! 挂载时两槽位各做解码，并从**最高代次**开始验证候选：
//! 根指针越界、记录 CRC 不符、ROOT 代次不一致、任一 LEAF 引用越界/损坏，
//! 该候选即失效，转而去验证较旧代次；两个槽位都失效才报错（绝不静默使用
//! 无效指针）。这就是“选取完整且引用有效的最高代次，拒绝越界指针”。

use std::collections::BTreeMap;
use std::fmt;
use std::io;

use crate::crc::Crc32;
use crate::format::{
    decode_leaf, decode_root, decode_super_page, encode_leaf, encode_record, encode_root,
    encode_super_page, get_u32, slot_for_generation, Ptr, Record, Root, RootEntry, SuperBlock,
    KIND_LEAF, KIND_ROOT, REC_HEADER_LEN, REC_MAGIC, SUPER_FILE_SIZE, SUPER_SLOTS,
};
use crate::io_layer::{SimFaultError, Storage};

/// 键/值长度上限（防止损坏数据中的长度字段造成巨量分配）。
const MAX_KV_LEN: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum StoreError {
    /// I/O 错误（真实文件错误，或测试注入的断电/半页写故障）。
    Io(io::Error),
    /// 目录下没有可识别的存储文件。
    NotFormatted,
    /// 两个超级块槽位都无效，或所有候选根均引用失败。
    CorruptStore(String),
    /// 键/值超长。
    TooLarge { what: &'static str, len: usize },
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "I/O 错误: {e}"),
            StoreError::NotFormatted => write!(f, "存储尚未格式化"),
            StoreError::CorruptStore(s) => write!(f, "存储不可恢复: {s}"),
            StoreError::TooLarge { what, len } => write!(f, "{what} 过长: {len} 字节"),
        }
    }
}
impl std::error::Error for StoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            StoreError::Io(e) => Some(e),
            _ => None,
        }
    }
}
impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl StoreError {
    /// 是否由故障注入触发（区别于真实介质错误）。
    pub fn is_injected_fault(&self) -> bool {
        match self {
            StoreError::Io(e) => e.get_ref().is_some_and(|c| c.is::<SimFaultError>()),
            _ => false,
        }
    }

    /// 返回注入故障的详细信息（测试用）。
    pub fn injected_fault(&self) -> Option<&SimFaultError> {
        match self {
            StoreError::Io(e) => e.get_ref().and_then(|c| c.downcast_ref::<SimFaultError>()),
            _ => None,
        }
    }
}

/// 一个已挂载、可读写的仓库。
pub struct Repository<S: Storage> {
    st: S,
    generation: u64,
    root: Ptr,
    kv: BTreeMap<Vec<u8>, Vec<u8>>,
    /// 当前已知数据文件长度（随本实例的追加更新；用于确定新记录偏移）。
    data_len: u64,
}

/// 挂载后观察到的仓库视图（供 HTTP / 诊断使用）。
#[derive(Debug)]
pub struct Snapshot {
    pub generation: u64,
    pub root: Ptr,
    pub entries: usize,
}

impl<S: Storage> Repository<S> {
    /// 在给定 I/O 后端上挂载并执行恢复。
    pub fn open(st: S) -> Result<Self, StoreError> {
        let data = st.read_data()?;
        let superfile = st.read_super()?;
        if superfile.len() < SUPER_FILE_SIZE {
            return Err(StoreError::NotFormatted);
        }

        // 1) 解码两个槽位（CRC/魔数/版本在此过滤）。
        let mut decoded: Vec<(usize, SuperBlock)> = Vec::new();
        for slot in 0..SUPER_SLOTS {
            let page = &superfile[slot * 4096..(slot + 1) * 4096];
            if let Some(sb) = decode_super_page(page) {
                decoded.push((slot, sb));
            }
        }
        if decoded.is_empty() {
            return Err(StoreError::CorruptStore(
                "两个超级块槽位均无效（魔数/CRC/版本校验失败）".into(),
            ));
        }

        // 2) 代次从高到低（同代次取槽位号大者）验证候选。
        decoded.sort_by(|a, b| b.1.generation.cmp(&a.1.generation).then(b.0.cmp(&a.0)));
        let mut rejection: Vec<String> = Vec::new();
        for (slot, sb) in &decoded {
            match validate_candidate(&data, sb) {
                Ok(kv) => {
                    return Ok(Repository {
                        st,
                        generation: sb.generation,
                        root: sb.root,
                        kv,
                        data_len: data.len() as u64,
                    });
                }
                Err(reason) => {
                    rejection.push(format!("槽位{slot} 代次{} 无效: {reason}", sb.generation))
                }
            }
        }
        Err(StoreError::CorruptStore(format!(
            "全部超级块候选均未通过引用校验（{}）",
            rejection.join("；")
        )))
    }

    /// 格式化：写入代次 0 的空超级块（槽位 0）。调用方需保证后端是新介质
    /// （真实文件请先用 `RealStorage::create_files`）。
    pub fn format(st: S) -> Result<Self, StoreError> {
        let sb = SuperBlock {
            generation: 0,
            root: Ptr::empty(),
        };
        let page = encode_super_page(&sb);
        st.write_super_slot(0, &page)?;
        st.sync_super()?;
        Ok(Repository {
            st,
            generation: 0,
            root: Ptr::empty(),
            kv: BTreeMap::new(),
            data_len: 0,
        })
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn len(&self) -> usize {
        self.kv.len()
    }
    pub fn is_empty(&self) -> bool {
        self.kv.is_empty()
    }
    pub fn get(&self, key: &[u8]) -> Option<&[u8]> {
        self.kv.get(key).map(|v| v.as_slice())
    }
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            generation: self.generation,
            root: self.root,
            entries: self.kv.len(),
        }
    }
    pub fn list(&self) -> impl Iterator<Item = (&[u8], &[u8])> {
        self.kv.iter().map(|(k, v)| (k.as_slice(), v.as_slice()))
    }

    /// 写入/更新一批键（重复键以最后一个为准），作为**一个**新代次提交。
    pub fn put_batch<I, K, V>(&mut self, items: I) -> Result<Snapshot, StoreError>
    where
        I: IntoIterator<Item = (K, V)>,
        K: AsRef<[u8]>,
        V: AsRef<[u8]>,
    {
        let mut merged: BTreeMap<Vec<u8>, Vec<u8>> = self.kv.clone();
        for (k, v) in items {
            let (k, v) = (k.as_ref(), v.as_ref());
            if k.len() > MAX_KV_LEN {
                return Err(StoreError::TooLarge {
                    what: "键",
                    len: k.len(),
                });
            }
            if v.len() > MAX_KV_LEN {
                return Err(StoreError::TooLarge {
                    what: "值",
                    len: v.len(),
                });
            }
            merged.insert(k.to_vec(), v.to_vec());
        }
        self.commit(merged)
    }

    /// 删除一批键；键不存在也算成功。作为一个新代次提交。
    pub fn delete_batch<I, K>(&mut self, keys: I) -> Result<Snapshot, StoreError>
    where
        I: IntoIterator<Item = K>,
        K: AsRef<[u8]>,
    {
        let mut next = self.kv.clone();
        for k in keys {
            next.remove(k.as_ref());
        }
        self.commit(next)
    }

    /// 提交协议：叶子 → fsync → 根 → fsync → 超级块 → fsync。
    fn commit(&mut self, merged: BTreeMap<Vec<u8>, Vec<u8>>) -> Result<Snapshot, StoreError> {
        let new_generation = self
            .generation
            .checked_add(1)
            .ok_or(StoreError::CorruptStore("代次溢出".into()))?;
        let slot = slot_for_generation(new_generation);
        let mut cursor = self.data_len;
        let mut entries: Vec<RootEntry> = Vec::with_capacity(merged.len());

        // —— 阶段 1：追加全部 LEAF ——
        for (key, value) in &merged {
            let record = encode_record(KIND_LEAF, &encode_leaf(key, value));
            let ptr = Ptr {
                offset: cursor,
                len: record.len() as u64,
                crc: crc_of_record(&record),
            };
            self.st.append_data(&record)?;
            cursor += record.len() as u64;
            entries.push(RootEntry {
                key: key.clone(),
                leaf: ptr,
            });
        }
        self.st.sync_data()?; // 同步点 A：叶子必须先持久化

        // —— 阶段 2：追加 ROOT ——
        let root = Root {
            generation: new_generation,
            entries,
        };
        let root_bytes = encode_record(KIND_ROOT, &encode_root(&root));
        let root_ptr = Ptr {
            offset: cursor,
            len: root_bytes.len() as u64,
            crc: crc_of_record(&root_bytes),
        };
        self.st.append_data(&root_bytes)?;
        self.st.sync_data()?; // 同步点 B：根持久化后才允许发布超级块
        cursor += root_bytes.len() as u64;

        // —— 阶段 3：交替槽位发布超级块 ——
        let sb = SuperBlock {
            generation: new_generation,
            root: root_ptr,
        };
        let page = encode_super_page(&sb);
        self.st.write_super_slot(slot, &page)?;
        self.st.sync_super()?; // 同步点 C：发布点

        // 发布成功后才更新内存镜像。
        self.kv = merged;
        self.generation = new_generation;
        self.root = root_ptr;
        self.data_len = cursor;
        Ok(Snapshot {
            generation: new_generation,
            root: root_ptr,
            entries: self.kv.len(),
        })
    }
}

/// 记录的 CRC（记录头中存储的值，等于对“CRC 字段清零的整段记录”求校验和）。
fn crc_of_record(record: &[u8]) -> u32 {
    // 增量计算，避免为大值复制整段记录。
    let mut c = Crc32::new();
    c.update(&record[..9]);
    c.update(&[0, 0, 0, 0]);
    c.update(&record[13..]);
    c.finalize()
}

/// 校验单个超级块候选：根指针边界、ROOT 记录完整性、代次一致、
/// 且 ROOT 引用的每一个 LEAF 都在界内且 CRC 正确。成功则重建键值镜像。
fn validate_candidate(data: &[u8], sb: &SuperBlock) -> Result<BTreeMap<Vec<u8>, Vec<u8>>, String> {
    // 空根（代次 0 的格式化快照）。
    if sb.root.is_empty() {
        if sb.root.crc != 0 {
            return Err("空根指针 CRC 非零".into());
        }
        return Ok(BTreeMap::new());
    }

    let root_rec = read_record_at(data, &sb.root)
        .ok_or_else(|| "根指针越界或记录 CRC 不符（拒绝越界/损坏指针）".to_string())?;
    if root_rec.kind != KIND_ROOT {
        return Err("根指针未指向 ROOT 记录".into());
    }
    let root = decode_root(&root_rec.payload).ok_or("ROOT payload 无法解析")?;
    if root.generation != sb.generation {
        return Err(format!(
            "ROOT 代次 {} 与超级块代次 {} 不一致",
            root.generation, sb.generation
        ));
    }

    let mut kv = BTreeMap::new();
    for entry in &root.entries {
        if entry.key.len() > MAX_KV_LEN {
            return Err("ROOT 中存在超长键".into());
        }
        let leaf_rec = read_record_at(data, &entry.leaf)
            .ok_or_else(|| format!("键 {:?} 的 LEAF 引用越界或损坏", entry.key))?;
        if leaf_rec.kind != KIND_LEAF {
            return Err(format!("键 {:?} 的引用未指向 LEAF", entry.key));
        }
        let leaf = decode_leaf(&leaf_rec.payload)
            .ok_or_else(|| format!("键 {:?} 的 LEAF payload 损坏", entry.key))?;
        if leaf.key != entry.key {
            return Err("LEAF 实际键与 ROOT 登记键不一致".to_string());
        }
        if leaf.value.len() > MAX_KV_LEN {
            return Err("存在超长值".into());
        }
        kv.insert(leaf.key, leaf.value);
    }
    Ok(kv)
}

/// 按指针读取一条记录：先做边界检查（越界一律 None），再核 CRC。
fn read_record_at(data: &[u8], ptr: &Ptr) -> Option<Record> {
    if !ptr.is_in_bounds(data.len() as u64) {
        return None;
    }
    let start = ptr.offset as usize;
    let end = start + ptr.len as usize;
    let bytes = &data[start..end];
    // 头部自带的 CRC 是对“CRC 字段清零的整段记录”计算的，这里逐段增量核算。
    let mut c = Crc32::new();
    c.update(&bytes[..9]);
    c.update(&[0, 0, 0, 0]);
    c.update(&bytes[13..]);
    if c.finalize() != ptr.crc {
        return None;
    }
    let magic = get_u32(&bytes[0..4]);
    if magic != REC_MAGIC {
        return None;
    }
    let kind = bytes[4];
    if !matches!(kind, KIND_LEAF | KIND_ROOT) {
        return None;
    }
    let payload_len = get_u32(&bytes[5..9]) as usize;
    if payload_len + REC_HEADER_LEN != bytes.len() {
        return None;
    }
    Some(Record {
        kind,
        payload: bytes[REC_HEADER_LEN..].to_vec(),
    })
}
