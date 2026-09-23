//! 单进程 MVCC 键值引擎。
//!
//! 设计要点：
//! - 每个键保存一条按提交时间戳排序的版本链（`Vec<Version>`），就地裁剪，
//!   不做全库复制。
//! - 事务在 `begin` 时取起始时间戳 `start_ts = 当前已提交上界`，读操作只看见
//!   `commit_ts <= start_ts` 的最新版本。
//! - 提交在全局锁内分配单调递增的 commit_ts 并做写-写冲突检测
//!   （first-committer-wins）：写集中任意键若存在 `commit_ts > start_ts`
//!   的已提交版本，则本事务中止。
//! - 删除写入墓碑版本（value = None）。
//! - 活跃事务与独立只读快照都登记到快照集合；GC 水位线 = 存活快照最小时间戳，
//!   只能回收所有存活快照都不再需要的旧版本，因此旧读在 GC 前后结果一致。

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

/// 单个版本：某键在 `commit_ts` 提交后的值；`None` 表示墓碑（删除）。
#[derive(Clone, Debug)]
pub struct Version {
    pub commit_ts: u64,
    pub value: Option<Vec<u8>>,
}

/// 活跃事务。
struct Transaction {
    /// 事务开始（取快照）的时间戳，读可见性上界。
    start_ts: u64,
    /// 写集：键 -> 值（None 为删除）。提交时一次性安装。
    writes: HashMap<Vec<u8>, Option<Vec<u8>>>,
}

struct Inner {
    /// 键 -> 版本链，版本按 commit_ts 升序保存。
    store: HashMap<Vec<u8>, Vec<Version>>,
    /// 已提交时间戳上界；下一个提交时间戳为 last_commit_ts + 1。
    last_commit_ts: u64,
    /// 活跃事务：txn_id -> 事务。txn_id 由 begin 序号分配，全局唯一。
    txns: HashMap<u64, Transaction>,
    /// 所有存活快照时间戳（活跃事务 start_ts 与只读快照 ts 的并集，去重）。
    snapshots: HashSet<u64>,
    /// 独立只读快照：snapshot_id -> 快照时间戳。
    readonly_snapshots: HashMap<u64, u64>,
    /// begin / create_snapshot 共用的 id 分配序号。
    next_id: u64,
}

pub struct MvccStore {
    inner: Mutex<Inner>,
}

/// 提交失败原因。
#[derive(Debug, PartialEq, Eq)]
pub enum CommitError {
    /// 写-写冲突：写集中存在键在本事务开始后已被他人提交。
    WriteWriteConflict { key: Vec<u8> },
    /// 事务不存在（已提交/已回滚/非法 id）。
    NotFound,
}

/// GC 统计。
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct GcStats {
    /// 本次扫描的键数。
    pub keys_scanned: usize,
    /// 被回收的版本数（含随空键整条移除的墓碑）。
    pub versions_reclaimed: usize,
    /// 被整条移除的键数（仅余墓碑且无存活快照可见）。
    pub keys_removed: usize,
    /// 当前 GC 水位线（存活快照最小 ts；无存活快照时为 null）。
    pub watermark: Option<u64>,
}

// 引擎状态由一把 Mutex 保护，所有 begin/get/commit/gc 均为锁内短临界区。

impl MvccStore {
    pub fn new() -> Self {
        MvccStore {
            inner: Mutex::new(Inner {
                store: HashMap::new(),
                last_commit_ts: 0,
                txns: HashMap::new(),
                snapshots: HashSet::new(),
                readonly_snapshots: HashMap::new(),
                next_id: 0,
            }),
        }
    }

    /// 开始一个读写事务，返回 (txn_id, start_ts) 并登记快照。
    pub fn begin(&self) -> (u64, u64) {
        let mut g = self.inner.lock().unwrap();
        g.next_id += 1;
        let txn_id = g.next_id;
        let start_ts = g.last_commit_ts;
        g.snapshots.insert(start_ts);
        g.txns.insert(
            txn_id,
            Transaction {
                start_ts,
                writes: HashMap::new(),
            },
        );
        (txn_id, start_ts)
    }

    /// 创建独立只读快照（不绑定事务），返回 (snapshot_id, snap_ts)。
    pub fn create_snapshot(&self) -> (u64, u64) {
        let mut g = self.inner.lock().unwrap();
        g.next_id += 1;
        let snap_id = g.next_id;
        let snap_ts = g.last_commit_ts;
        g.snapshots.insert(snap_ts);
        g.readonly_snapshots.insert(snap_id, snap_ts);
        (snap_id, snap_ts)
    }

    /// 关闭独立快照；关闭并且无同 ts 其他存活者后，其依赖版本方可回收。
    pub fn close_snapshot(&self, snap_id: u64) -> bool {
        let mut g = self.inner.lock().unwrap();
        match g.readonly_snapshots.remove(&snap_id) {
            Some(ts) => {
                release_snapshot(&mut g, ts);
                true
            }
            None => false,
        }
    }

    /// 事务内读：读己之写优先，否则读快照点上最新已提交版本。
    /// 返回 Ok(Some(bytes)) 值存在；Ok(None) 不存在/已删除；Err(()) 事务不存在。
    pub fn get(&self, txn_id: u64, key: &[u8]) -> Result<Option<Vec<u8>>, ()> {
        let g = self.inner.lock().unwrap();
        let txn = g.txns.get(&txn_id).ok_or(())?;
        if let Some(v) = txn.writes.get(key) {
            return Ok(v.clone());
        }
        Ok(snapshot_get(&g.store, key, txn.start_ts))
    }

    /// 在指定只读快照上读。
    pub fn snapshot_read(&self, snap_id: u64, key: &[u8]) -> Result<Option<Vec<u8>>, ()> {
        let g = self.inner.lock().unwrap();
        let snap = *g.readonly_snapshots.get(&snap_id).ok_or(())?;
        Ok(snapshot_get(&g.store, key, snap))
    }

    /// 列出某快照点上全部“可见且未删除”的 (key, value)，按键升序。
    pub fn scan_at(&self, snap: u64) -> Vec<(Vec<u8>, Vec<u8>)> {
        let g = self.inner.lock().unwrap();
        let mut out = Vec::new();
        for (key, versions) in &g.store {
            if let Some(Some(v)) = visible_version(versions, snap) {
                out.push((key.clone(), v));
            }
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    /// 当前已提交上界时间戳。
    pub fn latest_ts(&self) -> u64 {
        self.inner.lock().unwrap().last_commit_ts
    }

    pub fn put(&self, txn_id: u64, key: Vec<u8>, value: Vec<u8>) -> Result<(), ()> {
        let mut g = self.inner.lock().unwrap();
        let txn = g.txns.get_mut(&txn_id).ok_or(())?;
        txn.writes.insert(key, Some(value));
        Ok(())
    }

    pub fn delete(&self, txn_id: u64, key: Vec<u8>) -> Result<(), ()> {
        let mut g = self.inner.lock().unwrap();
        let txn = g.txns.get_mut(&txn_id).ok_or(())?;
        txn.writes.insert(key, None);
        Ok(())
    }

    /// 提交：写-写冲突检测（首次提交者胜），通过后分配 commit_ts 并安装全部写。
    pub fn commit(&self, txn_id: u64) -> Result<u64, CommitError> {
        let mut g = self.inner.lock().unwrap();
        let txn = g.txns.remove(&txn_id).ok_or(CommitError::NotFound)?;

        // 冲突检测：写集中任一键若存在 commit_ts > start_ts 的已提交版本，
        // 说明该键在本事务取快照后被他人改过 -> 中止。
        for key in txn.writes.keys() {
            if let Some(versions) = g.store.get(key) {
                if versions.iter().any(|v| v.commit_ts > txn.start_ts) {
                    release_snapshot(&mut g, txn.start_ts);
                    return Err(CommitError::WriteWriteConflict { key: key.clone() });
                }
            }
        }

        g.last_commit_ts += 1;
        let commit_ts = g.last_commit_ts;
        for (key, value) in txn.writes {
            g.store.entry(key).or_default().push(Version {
                commit_ts,
                value,
            });
        }
        release_snapshot(&mut g, txn.start_ts);
        Ok(commit_ts)
    }

    /// 回滚事务，丢弃写集。
    pub fn rollback(&self, txn_id: u64) -> bool {
        let mut g = self.inner.lock().unwrap();
        match g.txns.remove(&txn_id) {
            Some(txn) => {
                release_snapshot(&mut g, txn.start_ts);
                true
            }
            None => false,
        }
    }

    /// 当前 GC 水位线：所有存活快照时间戳的最小值；无存活快照为 None。
    pub fn watermark(&self) -> Option<u64> {
        self.inner.lock().unwrap().snapshots.iter().min().copied()
    }

    /// 执行版本回收（就地裁剪版本链，无全库复制）。
    ///
    /// 设水位线 w = 存活快照最小 ts。对每个键：
    /// - 找到 w 点可见的最新版本下标 i（最后一个 commit_ts <= w）；
    ///   版本 0..i 对任何存活快照都不可见，回收。
    /// - 无存活快照（w = +inf）时只保留最新版本。
    /// - 若裁剪后仅剩一块墓碑，则该键对任何现存与未来事务均不可见，整条删除。
    /// - 键的所有版本都新于 w（键在最旧快照之后才创建）时整链保留。
    pub fn gc(&self) -> GcStats {
        let mut g = self.inner.lock().unwrap();
        let watermark = g.snapshots.iter().min().copied();
        let mut stats = GcStats {
            watermark,
            ..Default::default()
        };

        let mut dead_keys: Vec<Vec<u8>> = Vec::new();
        for (key, versions) in g.store.iter_mut() {
            stats.keys_scanned += 1;

            let keep_from = match watermark {
                Some(w) => versions.iter().rposition(|v| v.commit_ts <= w),
                None => versions.len().checked_sub(1),
            };

            if let Some(idx) = keep_from {
                if idx > 0 {
                    stats.versions_reclaimed += idx;
                    versions.drain(0..idx);
                }
                if versions.len() == 1 && versions[0].value.is_none() {
                    stats.versions_reclaimed += 1;
                    stats.keys_removed += 1;
                    dead_keys.push(key.clone());
                }
            }
        }
        for k in dead_keys {
            g.store.remove(&k);
        }
        stats
    }

    /// 诊断：某键原始版本链 (commit_ts, is_tombstone)，升序。
    pub fn debug_versions(&self, key: &[u8]) -> Vec<(u64, bool)> {
        let g = self.inner.lock().unwrap();
        g.store
            .get(key)
            .map(|vs| {
                vs.iter()
                    .map(|v| (v.commit_ts, v.value.is_none()))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// 诊断：活跃事务数。
    pub fn debug_active_txn_count(&self) -> usize {
        self.inner.lock().unwrap().txns.len()
    }

    /// 诊断：存活只读快照数。
    pub fn debug_snapshot_count(&self) -> usize {
        self.inner.lock().unwrap().readonly_snapshots.len()
    }
}

impl Default for MvccStore {
    fn default() -> Self {
        Self::new()
    }
}

/// 若没有其他活跃事务/只读快照停留在 ts，则从快照集合移除。
fn release_snapshot(g: &mut Inner, ts: u64) {
    if !g.txns.values().any(|t| t.start_ts == ts)
        && !g.readonly_snapshots.values().any(|v| *v == ts)
    {
        g.snapshots.remove(&ts);
    }
}

fn snapshot_get(
    store: &HashMap<Vec<u8>, Vec<Version>>,
    key: &[u8],
    snap: u64,
) -> Option<Vec<u8>> {
    store
        .get(key)
        .and_then(|vs| visible_version(vs, snap).flatten())
}

/// 链（按 commit_ts 升序）中快照点可见的最新版本；
/// 返回 None 外层级表示“键不可见/不存在”，内层 None 表示墓碑。
fn visible_version(versions: &[Version], snap: u64) -> Option<Option<Vec<u8>>> {
    versions
        .iter()
        .rev()
        .find(|v| v.commit_ts <= snap)
        .map(|v| v.value.clone())
}

#[cfg(test)]
mod tests;
