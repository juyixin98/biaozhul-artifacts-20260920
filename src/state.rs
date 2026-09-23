//! 宿主状态：提交态 KV 存储 + 执行日志（journal）。
//!
//! 提交态是唯一会落盘的状态；模块执行期间的写入只存在于 engine 的事务覆盖层，
//! 只有执行成功才 [`CommittedState::apply`] 并原子发布（临时文件 + rename）。
//! 失败（燃料耗尽/越界/陷阱）覆盖层直接丢弃，绝不触碰提交态。

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::RwLock;

use crate::engine::JournalEntry;

#[derive(Debug, serde::Serialize, serde::Deserialize, Default)]
struct OnDisk {
    /// 单调递增的状态版本号，每次成功发布 +1。
    state_seq: u64,
    kv: BTreeMap<String, Vec<u8>>,
}

/// 已提交（对外可见、已持久化）的宿主状态。
pub struct CommittedState {
    path: Option<PathBuf>,
    inner: RwLock<Inner>,
}

#[derive(Default)]
struct Inner {
    seq: u64,
    kv: BTreeMap<String, Vec<u8>>,
}

impl CommittedState {
    /// 不落盘的内存态（测试用）。
    pub fn memory() -> Self {
        Self {
            path: None,
            inner: RwLock::new(Inner::default()),
        }
    }

    /// 从 `dir/state.json` 加载；文件不存在时初始化为空。
    pub fn load(dir: &Path) -> anyhow::Result<Self> {
        let path = dir.join("state.json");
        let inner = match std::fs::read(&path) {
            Ok(bytes) => {
                let disk: OnDisk = serde_json::from_slice(&bytes)?;
                Inner {
                    seq: disk.state_seq,
                    kv: disk.kv,
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Inner::default(),
            Err(e) => return Err(e.into()),
        };
        Ok(Self {
            path: Some(path),
            inner: RwLock::new(inner),
        })
    }

    /// 快照读取（用于事务建立读基线与状态查询 API）。
    pub fn snapshot(&self) -> (u64, BTreeMap<String, Vec<u8>>) {
        let g = self.inner.read().unwrap();
        (g.seq, g.kv.clone())
    }

    /// 应用一次成功执行产生的差异（upserts/deletes），并原子发布。
    ///
    /// 原子性：先写同目录临时文件并 fsync，再 rename 覆盖目标文件。
    /// 进程崩溃时要么是旧文件、要么是新文件，不会出现半截 JSON。
    pub fn apply(
        &self,
        upserts: BTreeMap<String, Vec<u8>>,
        deletes: Vec<String>,
    ) -> anyhow::Result<u64> {
        let mut g = self.inner.write().unwrap();
        for (k, v) in upserts {
            g.kv.insert(k, v);
        }
        for k in deletes {
            g.kv.remove(&k);
        }
        g.seq += 1;
        let disk = OnDisk {
            state_seq: g.seq,
            kv: g.kv.clone(),
        };
        if let Some(path) = &self.path {
            let bytes = serde_json::to_vec_pretty(&disk)?;
            let tmp = path.with_extension("json.tmp");
            {
                let mut f = std::fs::OpenOptions::new()
                    .write(true)
                    .create(true)
                    .truncate(true)
                    .open(&tmp)?;
                f.write_all(&bytes)?;
                f.sync_all()?;
            }
            std::fs::rename(&tmp, path)?;
            // fsync 目录，保证 rename 自身落盘（best-effort，失败仅记录）。
            if let Some(dir) = path.parent() {
                if let Ok(d) = std::fs::File::open(dir) {
                    let _ = d.sync_all();
                }
            }
        }
        Ok(g.seq)
    }
}

/// 执行日志：记录每次执行请求（含失败），便于审计与「相同输入+版本」复核。
pub struct Journal {
    entries: RwLock<Vec<JournalEntry>>,
    cap: usize,
}

impl Journal {
    pub fn new(cap: usize) -> Self {
        Self {
            entries: RwLock::new(Vec::new()),
            cap,
        }
    }

    pub fn append(&self, mut entry: JournalEntry) {
        let mut g = self.entries.write().unwrap();
        entry.journal_index = g.len() as u64;
        g.push(entry);
        if g.len() > self.cap {
            let excess = g.len() - self.cap;
            g.drain(0..excess);
        }
    }

    pub fn list(&self, limit: usize) -> Vec<JournalEntry> {
        let g = self.entries.read().unwrap();
        g.iter().rev().take(limit).cloned().collect()
    }
}
