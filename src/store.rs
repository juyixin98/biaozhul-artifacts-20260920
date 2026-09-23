//! 文件支持的存储库：磁盘格式、WAL 提交协议、崩溃恢复与增量 Merkle 更新。
//!
//! # 磁盘布局
//!
//! ```text
//! <repo>/
//! ├── manifest.json        # 已提交状态快照（JSON, 见 ManifestState）
//! ├── journal              # 前滚 WAL：帧序列 IMRKJRN1 || len || payload || sha256(payload)
//! ├── chunks/              # 正式块：chunks/<块序号>，内容即原始块字节
//! └── tmp/
//!     └── staging/         # 提交暂存块：tmp/staging/<块序号>
//! ```
//!
//! # manifest.json
//!
//! ```json
//! {
//!   "version": 3,
//!   "chunk_size": 4096,
//!   "file_length": 12300,
//!   "chunk_count": 4,
//!   "root": "<hex sha256>"
//! }
//! ```
//!
//! `root` 由块内容与协议参数派生（冗余存储仅用于打开时自检）。
//!
//! # journal 帧
//!
//! | 偏移 | 长度 | 内容 |
//! |---|---|---|
//! | 0 | 8 | 魔数 `IMRKJRN1` |
//! | 8 | 4 | payload 长度（小端 u32） |
//! | 12 | n | payload（UTF-8 JSON，见 [`CommitRecord`]） |
//! | 12+n | 32 | `SHA256(payload)` |
//!
//! 恢复时顺序解析帧：遇到第一个截断/校验失败的帧即停止，尾部丢弃（torn write）。
//!
//! # 同步边界（提交流程，每一步对应一个 [`FaultPoint`]）
//!
//! 1. 写暂存块 `tmp/staging/<i>` 并逐块 fsync（`ChunkWrite` / `ChunkSync`）。
//! 2. 追加 journal 提交帧并 fsync journal（`JournalAppend` / `JournalSync`）——**提交点**：
//!    此 fsync 返回后，即使断电，恢复也必须达到新状态。
//! 3. 暂存块提升为 `chunks/<i>`，删除被收缩的尾块（`PromoteChunk`）。
//! 4. 写 `manifest.json.tmp`、fsync、原子 rename 为 `manifest.json`
//!    （`ManifestWrite` / `ManifestSync` / `ManifestRename`）——检查点。
//! 5. 截断 journal 到 0 并 fsync（`JournalTruncate` / `JournalTruncateSync`），清理暂存块。
//!
//! 打开时重放：manifest 为基线，journal 中 version 更新的有效帧前滚（幂等：暂存块
//! 缺失但正式块已存在即视为已提升），然后重写检查点并清空 journal。

use crate::merkle::{
    self, build_levels, chunk_count, hash_leaf, prove_range, rebuild_root_from_chunks,
    update_levels, Hash32, RangeProof, VerifyError, DEFAULT_CHUNK_SIZE,
};
use crate::vfs::{FaultPoint, FaultPolicy, Vfs};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{self, Read, SeekFrom, Write};
use std::sync::{Arc, Mutex};

pub const MANIFEST: &str = "manifest.json";
pub const MANIFEST_TMP: &str = "manifest.json.tmp";
pub const JOURNAL: &str = "journal";
pub const CHUNK_DIR: &str = "chunks";
pub const STAGE_DIR: &str = "tmp/staging";

const MAGIC: &[u8; 8] = b"IMRKJRN1";

/// 存储库/格式错误。
#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Corrupt(String),
    Verify(VerifyError),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "io error: {e}"),
            StoreError::Corrupt(m) => write!(f, "corrupt repository: {m}"),
            StoreError::Verify(e) => write!(f, "verification failed: {e}"),
        }
    }
}
impl std::error::Error for StoreError {}
impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}
impl From<serde_json::Error> for StoreError {
    fn from(e: serde_json::Error) -> Self {
        StoreError::Corrupt(format!("json error: {e}"))
    }
}
impl From<VerifyError> for StoreError {
    fn from(e: VerifyError) -> Self {
        StoreError::Verify(e)
    }
}

fn corrupt(msg: impl Into<String>) -> StoreError {
    StoreError::Corrupt(msg.into())
}

/// manifest 中持久化的已提交状态。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManifestState {
    pub version: u64,
    pub chunk_size: u64,
    pub file_length: u64,
    pub chunk_count: u64,
    /// 派生根的 hex（自检用）。
    pub root: String,
}

/// journal 中的一次提交记录。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct CommitRecord {
    version: u64,
    chunk_size: u64,
    file_length: u64,
    chunk_count: u64,
    /// 本次写入/覆盖的块序号（内容在 tmp/staging 或 chunks 中）。
    changes: Vec<u64>,
    /// 本次删除的尾块序号。
    deletes: Vec<u64>,
    root: String,
}

/// 文件支持的增量 Merkle 存储库。
pub struct Repository {
    vfs: Arc<dyn Vfs>,
    fault: Option<Arc<Mutex<FaultPolicy>>>,
    on_crash: Option<Arc<dyn Fn() + Send + Sync>>,
    chunk_size: u64,
    file_length: u64,
    version: u64,
    /// `levels[0]` 为叶层；空文件时为空树（仅根由空根约定给出）。
    levels: Vec<Vec<Hash32>>,
}

fn chunk_path(idx: u64) -> String {
    format!("{CHUNK_DIR}/{idx}")
}
fn staging_path(idx: u64) -> String {
    format!("{STAGE_DIR}/{idx}")
}

/// 打开/创建仓库的选项。
pub struct OpenOptions {
    pub chunk_size: u64,
    pub fault: Option<(Arc<Mutex<FaultPolicy>>, Arc<dyn Fn() + Send + Sync>)>,
}

impl Default for OpenOptions {
    fn default() -> Self {
        Self {
            chunk_size: DEFAULT_CHUNK_SIZE,
            fault: None,
        }
    }
}

impl Repository {
    /// 在给定 VFS 上打开（不存在则创建）仓库。
    pub fn open(vfs: Arc<dyn Vfs>, chunk_size: u64) -> Result<Self, StoreError> {
        Self::open_with(
            vfs,
            OpenOptions {
                chunk_size,
                fault: None,
            },
        )
    }

    /// 带故障注入打开（测试用）。`fault` 为共享策略，`on_crash` 为崩溃回调
    /// （内存 VFS 上通常接 `MemVfs::simulate_crash`）。
    pub fn open_with_faults(
        vfs: Arc<dyn Vfs>,
        chunk_size: u64,
        fault: Arc<Mutex<FaultPolicy>>,
        on_crash: Arc<dyn Fn() + Send + Sync>,
    ) -> Result<Self, StoreError> {
        Self::open_with(
            vfs,
            OpenOptions {
                chunk_size,
                fault: Some((fault, on_crash)),
            },
        )
    }

    fn open_with(vfs: Arc<dyn Vfs>, opts: OpenOptions) -> Result<Self, StoreError> {
        if opts.chunk_size == 0 {
            return Err(corrupt("chunk_size must be > 0"));
        }
        vfs.mkdirs(CHUNK_DIR)?;
        vfs.mkdirs(STAGE_DIR)?;

        let mut repo = Repository {
            vfs,
            fault: opts.fault.as_ref().map(|(f, _)| f.clone()),
            on_crash: opts.fault.as_ref().map(|(_, c)| c.clone()),
            chunk_size: opts.chunk_size,
            file_length: 0,
            version: 0,
            levels: Vec::new(),
        };

        if !repo.vfs.exists(MANIFEST) {
            // 全新仓库：写入空状态检查点
            repo.levels = Vec::new();
            repo.write_manifest(0, 0, 0, merkle::root_from_leaves(&[], opts.chunk_size))?;
            return Ok(repo);
        }

        // 1) 读 manifest 基线
        let mut state = repo.read_manifest()?;
        if state.chunk_size != opts.chunk_size {
            return Err(corrupt(format!(
                "chunk_size mismatch: manifest={}, requested={}",
                state.chunk_size, opts.chunk_size
            )));
        }
        repo.chunk_size = state.chunk_size;

        // 2) 重放 journal
        state = repo.replay_journal(state)?;

        // 3) 载入状态并从磁盘块重建树（打开时全量一次；之后验证只按范围读块）
        repo.version = state.version;
        repo.file_length = state.file_length;
        repo.levels = repo.load_levels(state.chunk_count)?;

        let root = repo.root();
        if root != decode_hex32(&state.root).ok_or_else(|| corrupt("bad root hex in manifest"))? {
            return Err(corrupt(
                "manifest root does not match blocks rebuilt from chunks/",
            ));
        }
        Ok(repo)
    }

    // ---------- 基本访问器 ----------

    pub fn chunk_size(&self) -> u64 {
        self.chunk_size
    }
    pub fn file_length(&self) -> u64 {
        self.file_length
    }
    pub fn chunk_count(&self) -> u64 {
        if self.levels.is_empty() {
            0
        } else {
            self.levels[0].len() as u64
        }
    }
    pub fn version(&self) -> u64 {
        self.version
    }
    pub fn root(&self) -> Hash32 {
        if self.levels.is_empty() {
            merkle::hash_empty_root_pub(self.chunk_size)
        } else {
            self.levels.last().unwrap()[0]
        }
    }
    pub fn root_hex(&self) -> String {
        crate::hex_codec::to_hex(&self.root())
    }

    /// 读取单个块（仅用于生成范围证明，不需要全文件）。
    pub fn read_chunk(&self, idx: u64) -> io::Result<Vec<u8>> {
        let mut f = self.vfs.open(&chunk_path(idx))?;
        let mut buf = Vec::new();
        f.read_to_end(&mut buf)?;
        Ok(buf)
    }

    /// 生成 `[start, end)` 块区间的范围证明，只读取区间内块。
    pub fn range_proof(&self, start: u64, end: u64) -> Result<RangeProof, StoreError> {
        let total = self.chunk_count();
        let proof = prove_range(
            &self.levels,
            self.chunk_size,
            self.file_length,
            total,
            start,
            end,
            |i| self.read_chunk(i).ok(),
            self.root(),
        )?;
        Ok(proof)
    }

    /// 磁盘完整性自检：重新读取 chunks/ 全量重建根并与当前根比较（验收基线）。
    pub fn verify_block_store(&self) -> Result<Hash32, StoreError> {
        let n = self.chunk_count();
        let mut chunks = Vec::with_capacity(n as usize);
        for i in 0..n {
            chunks.push(self.read_chunk(i)?);
        }
        let rebuilt = rebuild_root_from_chunks(&chunks, self.chunk_size);
        if rebuilt != self.root() {
            return Err(corrupt("rebuilt root differs from committed root"));
        }
        if chunk_count(self.file_length, self.chunk_size) != n {
            return Err(corrupt(
                "file_length/chunk_size inconsistent with chunk_count",
            ));
        }
        Ok(rebuilt)
    }

    // ---------- 写入：整文件替换（内部走增量路径） ----------

    /// 用新文件内容替换当前文件。
    ///
    /// 仅内容不同的块参与提交；长度不变时 Merkle 各层走增量重算
    /// （[`update_levels`]，只重算脏路径）；块数变化（增长/缩短）时整树重建。
    /// 返回写入的块数与删除的块数。
    pub fn put_file(&mut self, data: &[u8]) -> Result<(usize, usize), StoreError> {
        let cs = self.chunk_size;
        let new_count = chunk_count(data.len() as u64, cs);
        let old_count = self.chunk_count();

        // 找出内容变化的块
        let mut changes: Vec<(u64, Vec<u8>)> = Vec::new();
        for i in 0..new_count {
            let start = (i * cs) as usize;
            let end = ((i + 1) * cs) as usize;
            let new_bytes = data[start..end.min(data.len())].to_vec();
            let same = if i < old_count {
                self.read_chunk(i)
                    .map(|old| old == new_bytes)
                    .unwrap_or(false)
            } else {
                false
            };
            if !same {
                changes.push((i, new_bytes));
            }
        }
        let deletes: Vec<u64> = (new_count..old_count).collect();

        if changes.is_empty() && deletes.is_empty() {
            return Ok((0, 0)); // 内容完全相同
        }

        let new_root = self.commit(&changes, &deletes, data.len() as u64, new_count)?;

        // 更新内存中的树
        if new_count == old_count {
            let leaf_changes: Vec<(u64, Hash32)> = changes
                .iter()
                .map(|(i, bytes)| (*i, hash_leaf(*i, cs, bytes)))
                .collect();
            self.levels = update_levels(&self.levels, &leaf_changes);
        } else {
            // 树形变了，全量重建（块数变化）
            self.levels = self.load_levels(new_count)?;
        }
        debug_assert_eq!(
            self.levels.last().map(|l| l[0]).unwrap_or(new_root),
            new_root
        );
        self.file_length = data.len() as u64;

        Ok((changes.len(), deletes.len()))
    }

    // ---------- WAL 提交流程 ----------

    fn fault(&self, at: FaultPoint) -> io::Result<()> {
        if let Some(p) = &self.fault {
            let crash = self.on_crash.clone();
            p.lock().unwrap().hit(at, &|| {
                if let Some(c) = &crash {
                    c();
                }
            })
        } else {
            Ok(())
        }
    }

    /// 执行一次事务，返回新根。
    fn commit(
        &mut self,
        changes: &[(u64, Vec<u8>)],
        deletes: &[u64],
        new_length: u64,
        new_count: u64,
    ) -> Result<Hash32, StoreError> {
        let new_version = self.version + 1;

        // 预估新根（暂存块写完后即可计算；根也写入提交帧）
        let new_root = self.preview_root(changes, new_count)?;

        // 1) 暂存块落盘并 fsync
        for (idx, bytes) in changes {
            self.fault(FaultPoint::ChunkWrite).map_err(StoreError::Io)?;
            let mut f = self.vfs.create(&staging_path(*idx))?;
            f.write_all(bytes)?;
            f.flush()?;
            self.fault(FaultPoint::ChunkSync).map_err(StoreError::Io)?;
            f.sync_all()?;
            drop(f);
        }

        // 2) journal 追加提交帧并 fsync —— 提交点
        let record = CommitRecord {
            version: new_version,
            chunk_size: self.chunk_size,
            file_length: new_length,
            chunk_count: new_count,
            changes: changes.iter().map(|(i, _)| *i).collect(),
            deletes: deletes.to_vec(),
            root: crate::hex_codec::to_hex(&new_root),
        };
        let frame = encode_frame(&record);
        self.fault(FaultPoint::JournalAppend)
            .map_err(StoreError::Io)?;
        {
            let mut j = self
                .vfs
                .open(JOURNAL)
                .or_else(|_| self.vfs.create(JOURNAL))?;
            j.seek(SeekFrom::End(0))?;
            j.write_all(&frame)?;
            j.flush()?;
            self.fault(FaultPoint::JournalSync)
                .map_err(StoreError::Io)?;
            j.sync_all()?;
        }

        // 3) 提升暂存块、删除尾块
        for (idx, _) in changes {
            self.fault(FaultPoint::PromoteChunk)
                .map_err(StoreError::Io)?;
            // rename 在真实 FS 与 MemVfs 上均覆盖已存在目标
            self.vfs.rename(&staging_path(*idx), &chunk_path(*idx))?;
        }
        for idx in deletes {
            if self.vfs.exists(&chunk_path(*idx)) {
                self.vfs.remove(&chunk_path(*idx))?;
            }
        }

        // 4) manifest 检查点（tmp + fsync + rename）
        self.fault(FaultPoint::ManifestWrite)
            .map_err(StoreError::Io)?;
        {
            let payload = serde_json::to_vec_pretty(&ManifestState {
                version: new_version,
                chunk_size: self.chunk_size,
                file_length: new_length,
                chunk_count: new_count,
                root: crate::hex_codec::to_hex(&new_root),
            })?;
            let mut m = self.vfs.create(MANIFEST_TMP)?;
            m.write_all(&payload)?;
            m.flush()?;
            self.fault(FaultPoint::ManifestSync)
                .map_err(StoreError::Io)?;
            m.sync_all()?;
        }
        self.fault(FaultPoint::ManifestRename)
            .map_err(StoreError::Io)?;
        self.vfs.rename(MANIFEST_TMP, MANIFEST)?;

        // 5) journal 截断并 fsync，随后清理暂存残留
        self.fault(FaultPoint::JournalTruncate)
            .map_err(StoreError::Io)?;
        {
            let mut j = self.vfs.open(JOURNAL)?;
            j.set_len(0)?;
            j.seek(SeekFrom::Start(0))?;
            self.fault(FaultPoint::JournalTruncateSync)
                .map_err(StoreError::Io)?;
            j.sync_all()?;
        }
        for (idx, _) in changes {
            let sp = staging_path(*idx);
            if self.vfs.exists(&sp) {
                let _ = self.vfs.remove(&sp);
            }
        }

        self.version = new_version;
        Ok(new_root)
    }

    /// 不落盘地预览提交后的根（同形树走增量，变形树重建）。
    fn preview_root(
        &self,
        changes: &[(u64, Vec<u8>)],
        new_count: u64,
    ) -> Result<Hash32, StoreError> {
        let old_count = self.chunk_count();
        if new_count == old_count {
            if self.levels.is_empty() && new_count == 0 {
                return Ok(merkle::hash_empty_root_pub(self.chunk_size));
            }
            let leaf_changes: Vec<(u64, Hash32)> = changes
                .iter()
                .map(|(i, bytes)| (*i, hash_leaf(*i, self.chunk_size, bytes)))
                .collect();
            let updated = update_levels(&self.levels, &leaf_changes);
            Ok(updated.last().unwrap()[0])
        } else {
            // 变形：按“新块集合”全量计算
            let mut leaves: Vec<Hash32> = Vec::with_capacity(new_count as usize);
            for i in 0..new_count {
                let h = if let Some((_, bytes)) = changes.iter().find(|(idx, _)| *idx == i) {
                    hash_leaf(i, self.chunk_size, bytes)
                } else if i < old_count {
                    let bytes = self.read_chunk(i)?;
                    hash_leaf(i, self.chunk_size, &bytes)
                } else {
                    return Err(corrupt(format!("new chunk {i} missing in preview")));
                };
                leaves.push(h);
            }
            Ok(merkle::root_from_leaves(&leaves, self.chunk_size))
        }
    }

    // ---------- manifest / journal ----------

    fn write_manifest(
        &mut self,
        version: u64,
        file_length: u64,
        count: u64,
        root: Hash32,
    ) -> Result<(), StoreError> {
        let state = ManifestState {
            version,
            chunk_size: self.chunk_size,
            file_length,
            chunk_count: count,
            root: crate::hex_codec::to_hex(&root),
        };
        let payload = serde_json::to_vec_pretty(&state)?;
        let mut m = self.vfs.create(MANIFEST)?;
        m.write_all(&payload)?;
        m.sync_all()?;
        Ok(())
    }

    fn read_manifest(&self) -> Result<ManifestState, StoreError> {
        let mut f = self.vfs.open(MANIFEST)?;
        let mut buf = Vec::new();
        f.read_to_end(&mut buf)?;
        serde_json::from_slice(&buf).map_err(|e| corrupt(format!("bad manifest.json: {e}")))
    }

    /// 顺序重放 journal 中的有效帧，返回最终状态；重放结束后写检查点并清空 journal。
    fn replay_journal(&self, base: ManifestState) -> Result<ManifestState, StoreError> {
        let raw = if self.vfs.exists(JOURNAL) {
            let mut f = self.vfs.open(JOURNAL)?;
            let mut buf = Vec::new();
            f.read_to_end(&mut buf)?;
            buf
        } else {
            Vec::new()
        };

        let mut state = base;
        let mut pos = 0usize;
        let mut applied_any = false;
        loop {
            match parse_frame(&raw[pos..]) {
                FrameResult::Frame { record, len } => {
                    if record.chunk_size != state.chunk_size {
                        return Err(corrupt("journal chunk_size differs from manifest"));
                    }
                    if record.version > state.version {
                        self.apply_record(&record)?;
                        state = ManifestState {
                            version: record.version,
                            chunk_size: record.chunk_size,
                            file_length: record.file_length,
                            chunk_count: record.chunk_count,
                            root: record.root.clone(),
                        };
                        applied_any = true;
                    }
                    pos += len;
                }
                FrameResult::Empty => break,
                // 截断/损坏尾部：丢弃剩余字节（本次重放不会让它们生效）
                FrameResult::Torn => break,
            }
        }

        if applied_any || !raw.is_empty() {
            // 恢复检查点（幂等）
            let root = decode_hex32(&state.root)
                .ok_or_else(|| corrupt("bad root hex in journal record"))?;
            let payload = serde_json::to_vec_pretty(&state)?;
            let mut m = self.vfs.create(MANIFEST_TMP)?;
            m.write_all(&payload)?;
            m.sync_all()?;
            drop(m);
            self.vfs.rename(MANIFEST_TMP, MANIFEST)?;

            if self.vfs.exists(JOURNAL) {
                let mut j = self.vfs.open(JOURNAL)?;
                j.set_len(0)?;
                j.sync_all()?;
            }
            // 重放后再次自检：磁盘块必须与记录的根一致
            let n = state.chunk_count;
            let mut chunks = Vec::with_capacity(n as usize);
            for i in 0..n {
                let mut f = self.vfs.open(&chunk_path(i))?;
                let mut buf = Vec::new();
                f.read_to_end(&mut buf)?;
                chunks.push(buf);
            }
            let rebuilt = rebuild_root_from_chunks(&chunks, state.chunk_size);
            if rebuilt != root {
                return Err(corrupt("post-replay root mismatch"));
            }
        }
        Ok(state)
    }

    /// 幂等应用一帧：暂存块提升（若存在），删除尾块。
    fn apply_record(&self, record: &CommitRecord) -> Result<(), StoreError> {
        for idx in &record.changes {
            let sp = staging_path(*idx);
            let cp = chunk_path(*idx);
            if self.vfs.exists(&sp) {
                self.vfs.rename(&sp, &cp)?;
            } else if !self.vfs.exists(&cp) {
                return Err(corrupt(format!(
                    "journal v{} references missing chunk {idx}",
                    record.version
                )));
            }
        }
        for idx in &record.deletes {
            let cp = chunk_path(*idx);
            if self.vfs.exists(&cp) {
                self.vfs.remove(&cp)?;
            }
        }
        Ok(())
    }

    fn load_levels(&self, count: u64) -> Result<Vec<Vec<Hash32>>, StoreError> {
        if count == 0 {
            return Ok(Vec::new());
        }
        let mut leaves = Vec::with_capacity(count as usize);
        for i in 0..count {
            let bytes = self.read_chunk(i)?;
            leaves.push(hash_leaf(i, self.chunk_size, &bytes));
        }
        Ok(build_levels(&leaves))
    }
}

// ---------------- 帧编解码 ----------------

fn encode_frame(record: &CommitRecord) -> Vec<u8> {
    let payload = serde_json::to_vec(record).expect("record serialization");
    let checksum: [u8; 32] = Sha256::digest(&payload).into();
    let mut out = Vec::with_capacity(12 + payload.len() + 32);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&payload);
    out.extend_from_slice(&checksum);
    out
}

enum FrameResult {
    Frame { record: CommitRecord, len: usize },
    Empty,
    Torn,
}

fn parse_frame(buf: &[u8]) -> FrameResult {
    if buf.is_empty() {
        return FrameResult::Empty;
    }
    if buf.len() < 12 + 32 || &buf[..8] != MAGIC {
        return FrameResult::Torn;
    }
    let payload_len = u32::from_le_bytes(buf[8..12].try_into().unwrap()) as usize;
    if payload_len == 0 || 12 + payload_len + 32 > buf.len() {
        return FrameResult::Torn;
    }
    let payload = &buf[12..12 + payload_len];
    let checksum = &buf[12 + payload_len..12 + payload_len + 32];
    let actual: [u8; 32] = Sha256::digest(payload).into();
    if actual.as_slice() != checksum {
        return FrameResult::Torn;
    }
    match serde_json::from_slice::<CommitRecord>(payload) {
        Ok(record) => FrameResult::Frame {
            record,
            len: 12 + payload_len + 32,
        },
        Err(_) => FrameResult::Torn,
    }
}

fn decode_hex32(s: &str) -> Option<Hash32> {
    crate::hex_codec::from_hex(s)?.try_into().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{FaultPoint, FaultPolicy, MemVfs};

    fn mem() -> (Arc<dyn Vfs>, MemVfs) {
        let m = MemVfs::new();
        (Arc::new(m.clone()), m)
    }

    fn sample(len: usize) -> Vec<u8> {
        (0..len).map(|i| (i % 251) as u8).collect()
    }

    #[test]
    fn empty_repo_has_well_known_empty_root() {
        let (fs, _m) = mem();
        let repo = Repository::open(fs, 16).unwrap();
        assert_eq!(repo.chunk_count(), 0);
        assert_eq!(repo.file_length(), 0);
        assert_eq!(repo.root(), merkle::hash_empty_root_pub(16));
        // 范围证明在空文件上非法
        assert!(matches!(
            repo.range_proof(0, 1).unwrap_err(),
            StoreError::Verify(VerifyError::BadRange)
        ));
    }

    #[test]
    fn write_grow_shrink_and_persist() {
        let (fs, _m) = mem();
        let cs = 16u64;
        let mut repo = Repository::open(fs.clone(), cs).unwrap();

        // 写 3.5 块
        let data = sample(56);
        let (w, d) = repo.put_file(&data).unwrap();
        assert_eq!((w, d), (4, 0));
        let root_full = {
            let chunks = merkle::split_chunks(&data, cs);
            merkle::rebuild_root_from_chunks(&chunks, cs)
        };
        assert_eq!(repo.root(), root_full);
        repo.verify_block_store().unwrap();

        // 只改最后一块的 1 个字节：增量更新，根应等于全量重建
        let mut data2 = data.clone();
        data2[50] ^= 0x5a;
        let (w, d) = repo.put_file(&data2).unwrap();
        assert_eq!((w, d), (1, 0));
        let root_inc = repo.root();
        let root_rebuild = {
            let chunks = merkle::split_chunks(&data2, cs);
            merkle::rebuild_root_from_chunks(&chunks, cs)
        };
        assert_eq!(
            root_inc, root_rebuild,
            "incremental root must match full rebuild"
        );
        repo.verify_block_store().unwrap();

        // 改第一块（奇数提升树里的左边界路径）
        let mut data3 = data2.clone();
        data3[0] = 0xee;
        repo.put_file(&data3).unwrap();
        let root3 = {
            let chunks = merkle::split_chunks(&data3, cs);
            merkle::rebuild_root_from_chunks(&chunks, cs)
        };
        assert_eq!(repo.root(), root3);

        // 缩短到 1 块
        let short = data3[..10].to_vec();
        let (w, d) = repo.put_file(&short).unwrap();
        assert_eq!(w, 1);
        assert_eq!(d, 3);
        assert_eq!(repo.chunk_count(), 1);
        let root4 = merkle::rebuild_root_from_chunks(&[short.clone()], cs);
        assert_eq!(repo.root(), root4);
        repo.verify_block_store().unwrap();

        // 重开：状态完整恢复
        drop(repo);
        let repo2 = Repository::open(fs.clone(), cs).unwrap();
        assert_eq!(repo2.file_length(), 10);
        assert_eq!(repo2.chunk_count(), 1);
        assert_eq!(repo2.root(), root4);
        repo2.verify_block_store().unwrap();

        // 用错误的 chunk_size 打开必须报错
        assert!(Repository::open(fs, 32).is_err());
    }

    #[test]
    fn update_few_blocks_only_dirty_paths() {
        // 用 33 块（多层奇数提升）覆盖随机若干块，对比增量与全量
        let (fs, _m) = mem();
        let cs = 8u64;
        let mut repo = Repository::open(fs, cs).unwrap();
        let mut data = sample((33 * cs) as usize);
        repo.put_file(&data).unwrap();

        for &idx in &[0u64, 7, 15, 16, 31, 32] {
            let off = (idx * cs + 2) as usize;
            data[off] = data[off].wrapping_add(1);
            repo.put_file(&data).unwrap();
            let chunks = merkle::split_chunks(&data, cs);
            assert_eq!(
                repo.root(),
                merkle::rebuild_root_from_chunks(&chunks, cs),
                "after updating chunk {idx}"
            );
        }
    }

    #[test]
    fn range_proof_head_tail_on_store() {
        let (fs, _m) = mem();
        let cs = 8u64;
        let mut repo = Repository::open(fs, cs).unwrap();
        let data = sample(50); // 7 块（最后一块 2 字节）
        repo.put_file(&data).unwrap();
        repo.verify_block_store().unwrap();

        for (s, e) in [(0, 1), (6, 7), (0, 3), (4, 7), (0, 7)] {
            let p = repo.range_proof(s, e).unwrap();
            assert_eq!(p.chunks.len(), (e - s) as usize);
            p.verify().unwrap();
            // 证明只携带区间块，体积远小于全文件
            let proof_bytes: usize = p.chunks.iter().map(|c| c.len()).sum();
            assert!(proof_bytes <= (e - s) as usize * cs as usize);
        }

        // 非法区间
        assert!(repo.range_proof(3, 2).is_err());
        assert!(repo.range_proof(0, 8).is_err());
    }

    #[test]
    fn clean_commit_leaves_no_journal_or_staging() {
        let (fs, m) = mem();
        let mut repo = Repository::open(fs.clone(), 16).unwrap();
        repo.put_file(&sample(40)).unwrap();
        assert_eq!(m.snapshot(JOURNAL).unwrap_or_default().len(), 0);
        assert!(fs.list(STAGE_DIR).unwrap().is_empty());
        assert!(fs.exists(MANIFEST));
    }

    /// 在每个故障点注入“普通 I/O 错误”，断言：
    /// - put_file 返回错误；
    /// - 重新打开后仓库处于**旧状态或新状态**之一（无撕裂），且块与根自洽。
    #[test]
    fn io_error_at_every_fault_point_is_recoverable() {
        for point in FaultPoint::all() {
            let mem = MemVfs::new();
            let fs: Arc<dyn Vfs> = Arc::new(mem.clone());
            let policy = Arc::new(Mutex::new(FaultPolicy::new().io_error(point, 1)));
            let on_crash: Arc<dyn Fn() + Send + Sync> = Arc::new({
                let mem = mem.clone();
                move || mem.simulate_crash()
            });

            let mut repo = Repository::open_with_faults(fs.clone(), 16, policy, on_crash).unwrap();
            let root_before = repo.root();
            let res = repo.put_file(&sample(48)); // 3 块
            assert!(res.is_err(), "expected failure at {point:?}");
            drop(repo);

            // 重新打开必须成功，且状态自洽
            let reopened = Repository::open(fs.clone(), 16)
                .unwrap_or_else(|e| panic!("reopen failed after fault at {point:?}: {e}"));
            reopened.verify_block_store().unwrap_or_else(|e| {
                panic!("block store inconsistent after fault at {point:?}: {e}")
            });
            let root_after = reopened.root();
            // 提交点之前（Chunk*/Journal*）应回滚到旧根；之后可能是新根
            matches!(
                point,
                FaultPoint::ChunkWrite | FaultPoint::ChunkSync | FaultPoint::JournalAppend
            )
            .then(|| {
                assert_eq!(
                    root_after, root_before,
                    "rolled-back state expected at {point:?}"
                )
            });
        }
    }

    /// 在每个故障点注入“崩溃”（丢失未 fsync 数据），重开后状态必须原子：
    /// JournalSync 及之后崩溃 => 新状态已提交；之前崩溃 => 旧状态。
    #[test]
    fn crash_at_every_fault_point_is_atomic() {
        for point in FaultPoint::all() {
            let mem = MemVfs::new();
            let fs: Arc<dyn Vfs> = Arc::new(mem.clone());
            let policy = Arc::new(Mutex::new(FaultPolicy::new().crash(point, 1)));
            let on_crash: Arc<dyn Fn() + Send + Sync> = Arc::new({
                let m = mem.clone();
                move || m.simulate_crash()
            });

            let mut repo = Repository::open_with_faults(fs.clone(), 16, policy, on_crash).unwrap();
            let root_before = repo.root();
            let data = sample(48);
            let root_after_expected =
                merkle::rebuild_root_from_chunks(&merkle::split_chunks(&data, 16), 16);
            let res = repo.put_file(&data);
            assert!(res.is_err(), "crash expected at {point:?}");
            drop(repo);

            let reopened = Repository::open(fs.clone(), 16)
                .unwrap_or_else(|e| panic!("reopen failed after crash at {point:?}: {e}"));
            reopened
                .verify_block_store()
                .unwrap_or_else(|e| panic!("inconsistent after crash at {point:?}: {e}"));
            let committed = reopened.root();

            let committed_before_commit_point = matches!(
                point,
                FaultPoint::ChunkWrite
                    | FaultPoint::ChunkSync
                    | FaultPoint::JournalAppend
                    | FaultPoint::JournalSync
            );
            // JournalSync 命中故障意味着 fsync“未成功返回”：按严格语义视为未提交
            if committed_before_commit_point {
                assert_eq!(
                    committed, root_before,
                    "crash at {point:?} must leave old state"
                );
            } else {
                assert_eq!(
                    committed, root_after_expected,
                    "crash at {point:?} must leave new committed state"
                );
            }
            // 恢复后 journal 已清空（检查点已重建）
            assert_eq!(mem.snapshot(JOURNAL).unwrap_or_default().len(), 0);
        }
    }

    /// journal 帧解析：物理截断帧 / 校验和损坏 / 垃圾字节 均识别为 Torn。
    #[test]
    fn torn_journal_tail_is_discarded() {
        let rec = CommitRecord {
            version: 2,
            chunk_size: 16,
            file_length: 32,
            chunk_count: 2,
            changes: vec![],
            deletes: vec![],
            root: "00".repeat(32),
        };
        let good = encode_frame(&rec);
        assert!(matches!(parse_frame(&good), FrameResult::Frame { .. }));
        assert!(matches!(parse_frame(&[]), FrameResult::Empty));
        // 物理截断（帧头声明的 payload 未写全）
        assert!(matches!(
            parse_frame(&good[..good.len() - 5]),
            FrameResult::Torn
        ));
        // 校验和损坏（长度完整、字节被改）
        let mut bad_ck = good.clone();
        let last = bad_ck.len() - 1;
        bad_ck[last] ^= 0xff;
        assert!(matches!(parse_frame(&bad_ck), FrameResult::Torn));
        // 魔数错误
        let mut bad_magic = good.clone();
        bad_magic[0] ^= 0xff;
        assert!(matches!(parse_frame(&bad_magic), FrameResult::Torn));
        // 后跟垃圾尾巴：先解析出好帧，剩余垃圾再解析为 Torn
        let mut journal = good.clone();
        journal.extend_from_slice(b"partial garbage");
        assert!(matches!(parse_frame(&journal), FrameResult::Frame { .. }));
        // 模拟 replay 的推进：解析好帧后跳过其长度，余下应判 Torn
        if let FrameResult::Frame { len, .. } = parse_frame(&journal) {
            assert!(matches!(parse_frame(&journal[len..]), FrameResult::Torn));
        }
    }

    #[test]
    fn reopened_repo_replays_committed_frame() {
        // JournalSync 之后立即崩溃：帧已落盘但检查点未更新 -> 重开应前滚到新状态。
        let mem = MemVfs::new();
        let fs: Arc<dyn Vfs> = Arc::new(mem.clone());
        let policy = Arc::new(Mutex::new(
            FaultPolicy::new().crash(FaultPoint::PromoteChunk, 1),
        ));
        let on_crash: Arc<dyn Fn() + Send + Sync> = Arc::new({
            let m = mem.clone();
            move || m.simulate_crash()
        });

        let data = sample(48);
        let expected_root = merkle::rebuild_root_from_chunks(&merkle::split_chunks(&data, 16), 16);
        {
            let mut repo = Repository::open_with_faults(fs.clone(), 16, policy, on_crash).unwrap();
            assert!(repo.put_file(&data).is_err());
        }
        let repo = Repository::open(fs, 16).expect("reopen+replay must succeed");
        assert_eq!(repo.root(), expected_root);
        assert_eq!(repo.file_length(), 48);
        repo.verify_block_store().unwrap();
    }

    /// 在**真实磁盘**（RealVfs）上注入普通 I/O 错误：进程存活，重开后仓库必须一致。
    /// 证明故障注入层不仅作用于内存 FS。
    #[test]
    fn io_error_on_real_disk_is_recoverable() {
        use crate::vfs::RealVfs;
        let dir = std::env::temp_dir().join(format!(
            "imerkle-realdisk-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&dir);

        let policy = Arc::new(Mutex::new(
            FaultPolicy::new().io_error(FaultPoint::PromoteChunk, 1),
        ));
        let on_crash: Arc<dyn Fn() + Send + Sync> = Arc::new(|| {}); // 真实崩溃不模拟

        {
            let vfs: Arc<dyn Vfs> = Arc::new(RealVfs::new(&dir));
            let mut repo = Repository::open_with_faults(vfs, 16, policy, on_crash).unwrap();
            assert!(repo.put_file(&sample(48)).is_err());
        }
        let vfs: Arc<dyn Vfs> = Arc::new(RealVfs::new(&dir));
        let repo = Repository::open(vfs, 16).expect("reopen on real disk must succeed");
        repo.verify_block_store()
            .expect("real-disk block store must be consistent after injected I/O error");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
