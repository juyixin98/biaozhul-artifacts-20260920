//! LSM 内核：memtable / immutable memtables / 不可变有序段、点查、范围扫描、
//! 版本化合并与墓碑规则、原子清单发布。

use crate::config::Config;
use crate::manifest::{merge_latest_per_key, Layout, ManifestData, SegmentMeta};
use crate::segment::Entry;
use anyhow::{anyhow, Result};
use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex};

/// 合并故障注入点（用于“合并中断”验收）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    /// 新段文件写完（已 rename+fsync），但 MANIFEST 尚未切换。
    AfterNewSegmentBeforeManifest,
    /// MANIFEST 已原子切换，但旧段文件尚未删除。
    AfterManifestBeforeDeleteOld,
}

impl CrashPoint {
    pub fn name(self) -> &'static str {
        match self {
            CrashPoint::AfterNewSegmentBeforeManifest => "after_new_segment_before_manifest",
            CrashPoint::AfterManifestBeforeDeleteOld => "after_manifest_before_delete_old",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "after_new_segment_before_manifest" => Some(Self::AfterNewSegmentBeforeManifest),
            "after_manifest_before_delete_old" => Some(Self::AfterManifestBeforeDeleteOld),
            _ => None,
        }
    }
}

/// 已加载到内存的段。
#[derive(Debug, Clone)]
pub struct LoadedSegment {
    pub meta: SegmentMeta,
    /// 按 key 升序、同键仅一条（最新 seq）。
    pub entries: Vec<Entry>,
}

/// 一次合并的结果。
#[derive(Debug, Clone, serde::Serialize)]
pub struct CompactionReport {
    /// 参与合并的旧段 id（新 -> 旧）。
    pub input_ids: Vec<u64>,
    /// 新段 id；若合并结果为空（全部墓碑被丢弃）则为 None。
    pub new_segment_id: Option<u64>,
    /// 被丢弃的墓碑键（已确认所有更老层都无同键）。
    pub dropped_tombstones: Vec<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct StateSnapshot {
    pub next_seq: u64,
    pub next_segment_id: u64,
    pub memtable_size: usize,
    pub immutable_memtable_count: usize,
    /// 存活段，新 -> 旧。
    pub segments: Vec<SegmentMeta>,
}

struct Inner {
    next_seq: u64,
    next_id: u64,
    /// 可变内存表：同键保留最新。
    memtable: BTreeMap<String, Entry>,
    /// 冻结内存表，新 -> 旧；每个元素为按 key 排序的向量。
    immutable: Vec<Vec<Entry>>,
    /// 已发布段，新 -> 旧。
    segments: Vec<LoadedSegment>,
    /// 崩溃注入后置位：本进程视为“已宕机”，拒绝一切写/合并/flush，
    /// 读仍可用于诊断；真实系统此时进程应直接退出。
    crashed: bool,
}

/// LSM 数据库句柄。所有修改在 `Mutex<Inner>` 下串行化（简化设计）。
pub struct Db {
    layout: Layout,
    config: Config,
    inner: Mutex<Inner>,
}

impl Db {
    /// 打开（或创建）数据目录并恢复清单。
    pub fn open(config: Config) -> Result<Arc<Self>> {
        let layout = Layout::new(&config.dir);
        let data: ManifestData = layout.open()?;

        let mut segments = Vec::with_capacity(data.segments.len());
        for meta in &data.segments {
            let entries = layout.read_segment(meta.id)?;
            debug_assert_sorted_unique(&entries, meta.id)?;
            segments.push(LoadedSegment {
                meta: meta.clone(),
                entries,
            });
        }

        let inner = Inner {
            next_seq: data.next_seq,
            next_id: data.next_id,
            memtable: BTreeMap::new(),
            immutable: Vec::new(),
            segments,
            crashed: false,
        };
        let db = Arc::new(Self {
            layout,
            config: config.clone(),
            inner: Mutex::new(inner),
        });

        if config.background_flush {
            spawn_bg_flush(db.clone());
        }
        Ok(db)
    }

    /// 写入一个键值。
    pub fn put(&self, key: &str, value: &str) -> Result<u64> {
        self.write(key, Some(value.to_string()))
    }

    /// 删除一个键（写入墓碑）。
    pub fn delete(&self, key: &str) -> Result<u64> {
        self.write(key, None)
    }

    fn write(&self, key: &str, value: Option<String>) -> Result<u64> {
        let mut g = self.inner.lock().unwrap();
        if g.crashed {
            return Err(anyhow!(
                "database is in crashed state (crash injection); restart the process"
            ));
        }
        let seq = g.next_seq;
        g.next_seq += 1;
        g.memtable
            .insert(key.to_string(), Entry::new(key, seq, value));
        if g.memtable.len() >= self.config.memtable_entries {
            freeze(&mut g);
        }
        drop(g);
        Ok(seq)
    }

    /// 冻结当前 memtable 并将全部 immutable memtable 落盘（后台关闭时测试用）。
    pub fn flush(&self) -> Result<Vec<u64>> {
        let mut g = self.inner.lock().unwrap();
        if g.crashed {
            return Err(anyhow!(
                "database is in crashed state (crash injection); restart the process"
            ));
        }
        freeze(&mut g);
        let mut ids = Vec::new();
        while let Some(seg_id) = flush_oldest(&self.layout, &mut g)? {
            ids.push(seg_id);
        }
        Ok(ids)
    }

    /// 点查：按 memtable -> immutable(新->旧) -> segments(新->旧) 顺序，
    /// 找到的第一个即最新版本；墓碑返回 None。
    pub fn get(&self, key: &str) -> Result<Option<String>> {
        let g = self.inner.lock().unwrap();
        if let Some(e) = g.memtable.get(key) {
            return Ok(e.value.clone());
        }
        for imm in &g.immutable {
            if let Ok(i) = imm.binary_search_by(|e| e.key.as_str().cmp(key)) {
                return Ok(imm[i].value.clone());
            }
        }
        for seg in &g.segments {
            if let Ok(i) = seg.entries.binary_search_by(|e| e.key.as_str().cmp(key)) {
                return Ok(seg.entries[i].value.clone());
            }
        }
        Ok(None)
    }

    /// 范围扫描，区间 `[start, end)`（end 为 None 时无上界）。
    /// 跨所有层按版本合并，墓碑遮蔽旧值，结果按键升序且每个键至多出现一次。
    pub fn scan(&self, start: Option<&str>, end: Option<&str>) -> Result<Vec<(String, String)>> {
        let g = self.inner.lock().unwrap();
        let mut sources: Vec<Vec<Entry>> = Vec::new();

        // memtable
        sources.push(filter_btree(&g.memtable, start, end));
        // immutable 新 -> 旧
        for imm in &g.immutable {
            sources.push(filter_slice(imm, start, end));
        }
        // segments 新 -> 旧
        for seg in &g.segments {
            sources.push(filter_slice(&seg.entries, start, end));
        }

        let merged = merge_latest_per_key(&sources);
        Ok(merged
            .into_iter()
            .filter_map(|e| e.value.map(|v| (e.key, v)))
            .collect())
    }

    /// 合并段区间 `[newer_idx, older_idx]`（索引按“新 -> 旧”，0 为最新段）。
    ///
    /// 墓碑规则：区间内某键的最新版本是墓碑时，只有在**确认所有更老层
    /// （`older_idx` 之后的段）都不存在同键**后才丢弃；否则墓碑必须保留，
    /// 防止更老的值在未来合并/读取中复活。
    pub fn compact_range(
        &self,
        newer_idx: usize,
        older_idx: usize,
        crash: Option<CrashPoint>,
    ) -> Result<CompactionReport> {
        let mut g = self.inner.lock().unwrap();
        if g.crashed {
            return Err(anyhow!(
                "database is in crashed state (crash injection); restart the process"
            ));
        }
        let n = g.segments.len();
        if n == 0 {
            return Err(anyhow!("no segments to compact"));
        }
        if newer_idx > older_idx || older_idx >= n {
            return Err(anyhow!(
                "invalid compaction range [{newer_idx}, {older_idx}] with {n} segments"
            ));
        }

        // 1) 收集输入（新 -> 旧），多路归并为每键最新版本。
        let input_sources: Vec<Vec<Entry>> = g.segments[newer_idx..=older_idx]
            .iter()
            .map(|s| s.entries.clone())
            .collect();
        let input_ids: Vec<u64> = g.segments[newer_idx..=older_idx]
            .iter()
            .map(|s| s.meta.id)
            .collect();
        let mut merged = merge_latest_per_key(&input_sources);

        // 2) 确认更老层中存在哪些键 -> 这些键的墓碑绝不能丢。
        let older_keys: HashSet<String> = g.segments[older_idx + 1..]
            .iter()
            .flat_map(|s| s.entries.iter().map(|e| e.key.clone()))
            .collect();

        let mut dropped_tombstones: Vec<String> = Vec::new();
        merged.retain(|e| match &e.value {
            Some(_) => true,
            None => {
                if older_keys.contains(&e.key) {
                    true // 更老层有同键：保留墓碑继续遮蔽
                } else {
                    dropped_tombstones.push(e.key.clone());
                    false // 已确认更老层不存在同键：安全丢弃
                }
            }
        });

        // 3) 写新段（若结果非空），然后原子切换清单。
        let new_id = g.next_id;
        let new_segment = if merged.is_empty() {
            None
        } else {
            let meta = self.layout.write_segment(new_id, &merged)?;
            Some(LoadedSegment {
                meta,
                entries: merged,
            })
        };
        let new_segment_id = new_segment.as_ref().map(|s| s.meta.id);

        if crash == Some(CrashPoint::AfterNewSegmentBeforeManifest) {
            g.crashed = true;
            return Err(anyhow!(
                "[crash injection] {}: segment {new_id} durable, manifest not switched",
                CrashPoint::AfterNewSegmentBeforeManifest.name()
            ));
        }

        // 4) 构造并原子发布新清单（段顺序仍为新 -> 旧）。
        let mut new_segments: Vec<LoadedSegment> = Vec::with_capacity(n);
        new_segments.extend(g.segments[..newer_idx].iter().cloned());
        if let Some(s) = &new_segment {
            new_segments.push(s.clone());
        }
        new_segments.extend(g.segments[older_idx + 1..].iter().cloned());

        let data = ManifestData {
            next_seq: g.next_seq,
            next_id: g.next_id + 1,
            segments: new_segments.iter().map(|s| s.meta.clone()).collect(),
        };
        self.layout.publish_manifest(&data)?;

        if crash == Some(CrashPoint::AfterManifestBeforeDeleteOld) {
            g.crashed = true;
            return Err(anyhow!(
                "[crash injection] {}: manifest switched, old segment files not deleted",
                CrashPoint::AfterManifestBeforeDeleteOld.name()
            ));
        }

        // 5) 发布成功：更新内存态，再删除旧段文件（清单已不引用，
        //    即使删除失败也会在下次 open 时作为孤儿被清理）。
        g.next_id += 1;
        g.segments = new_segments;
        drop(g);

        for id in &input_ids {
            self.layout.delete_segment(*id)?;
        }

        Ok(CompactionReport {
            input_ids,
            new_segment_id,
            dropped_tombstones,
        })
    }

    /// 全量合并：所有段归并为一个段；此时不存在更老层，所有墓碑均可安全丢弃。
    pub fn compact_full(&self, crash: Option<CrashPoint>) -> Result<CompactionReport> {
        let g = self.inner.lock().unwrap();
        let n = g.segments.len();
        drop(g);
        if n == 0 {
            return Err(anyhow!("no segments to compact"));
        }
        self.compact_range(0, n - 1, crash)
    }

    pub fn snapshot(&self) -> StateSnapshot {
        let g = self.inner.lock().unwrap();
        StateSnapshot {
            next_seq: g.next_seq,
            next_segment_id: g.next_id,
            memtable_size: g.memtable.len(),
            immutable_memtable_count: g.immutable.len(),
            segments: g.segments.iter().map(|s| s.meta.clone()).collect(),
        }
    }
}

/// memtable 超阈值：冻结为不可变内存表。
fn freeze(g: &mut Inner) {
    if g.memtable.is_empty() {
        return;
    }
    let sorted: Vec<Entry> = g.memtable.values().cloned().collect(); // BTreeMap 已按 key 升序
    g.immutable.insert(0, sorted);
    g.memtable.clear();
}

/// 将最老的一个 immutable memtable 落盘为新段（id 越小越老），并原子发布清单。
fn flush_oldest(layout: &Layout, g: &mut Inner) -> Result<Option<u64>> {
    let entries = match g.immutable.pop() {
        Some(e) => e,
        None => return Ok(None),
    };
    let id = g.next_id;
    let meta = layout.write_segment(id, &entries)?;

    let mut metas: Vec<SegmentMeta> = g.segments.iter().map(|s| s.meta.clone()).collect();
    metas.insert(0, meta); // 新段在最前（最新）
    let data = ManifestData {
        next_seq: g.next_seq,
        next_id: g.next_id + 1,
        segments: metas,
    };
    layout.publish_manifest(&data)?;

    g.next_id += 1;
    g.segments.insert(
        0,
        LoadedSegment {
            meta: data.segments[0].clone(),
            entries,
        },
    );
    Ok(Some(id))
}

fn spawn_bg_flush(db: Arc<Db>) {
    std::thread::Builder::new()
        .name("lsm-bg-flush".into())
        .spawn(move || loop {
            std::thread::sleep(std::time::Duration::from_millis(100));
            let mut g = match db.inner.lock() {
                Ok(g) => g,
                Err(_) => return, // 进程收尾
            };
            if g.crashed {
                return;
            }
            loop {
                match flush_oldest(&db.layout, &mut g) {
                    Ok(Some(_)) => continue,
                    Ok(None) => break,
                    Err(e) => {
                        tracing::error!("background flush failed: {e:#}");
                        break;
                    }
                }
            }
        })
        .expect("spawn bg flush thread");
}

fn filter_btree(
    map: &BTreeMap<String, Entry>,
    start: Option<&str>,
    end: Option<&str>,
) -> Vec<Entry> {
    use std::ops::Bound::{Excluded, Included, Unbounded};
    let lo = match start {
        Some(s) => Included(s),
        None => Unbounded,
    };
    let hi = match end {
        Some(e) => Excluded(e),
        None => Unbounded,
    };
    map.range::<str, _>((lo, hi))
        .map(|(_, v)| v.clone())
        .collect()
}

fn filter_slice(s: &[Entry], start: Option<&str>, end: Option<&str>) -> Vec<Entry> {
    let lo = match start {
        Some(st) => s.partition_point(|e| e.key.as_str() < st),
        None => 0,
    };
    let hi = match end {
        Some(en) => s.partition_point(|e| e.key.as_str() < en),
        None => s.len(),
    };
    s[lo..hi].to_vec()
}

fn debug_assert_sorted_unique(entries: &[Entry], id: u64) -> Result<()> {
    for w in entries.windows(2) {
        if w[0].key >= w[1].key {
            return Err(anyhow!(
                "segment {id} corrupt: keys not strictly sorted ({:?} >= {:?})",
                w[0].key,
                w[1].key
            ));
        }
    }
    Ok(())
}
