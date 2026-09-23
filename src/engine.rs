//! 简化 LSM 存储引擎。
//!
//! 层次：
//! - memtable：内存 BTreeMap，单键只保留最新版本（带全局单调 seq）。
//! - 不可变有序段（segment）：flush 时整体写成 JSONL 不可变文件，按层（level）组织。
//! - manifest：段清单，tmp 文件 + rename 原子切换；段文件先落盘并 fsync，再切清单。
//!
//! 合并（compaction）把 level n 与 level n+1 的所有段归并为 level n+1 的一个新段；
//! 某个墓碑只有在确认更老层（level > n+1）不存在同键时才允许丢弃。
//! 新段文件写好（含 fsync）但清单尚未切换时崩溃：重启后该文件是孤儿，会被清理，
//! 旧清单保持有效，因此已发布的可视图不会被破坏。

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard};

/// 一条记录。`value = None` 表示墓碑（删除标记）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct Entry {
    pub key: String,
    pub seq: u64,
    pub value: Option<String>,
}

impl Entry {
    pub fn is_tombstone(&self) -> bool {
        self.value.is_none()
    }
}

/// 合并故障注入点（仅用于验收测试）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    None,
    /// 新段文件已写入并 fsync、但 manifest 尚未切换时 panic（模拟进程崩溃）。
    PanicAfterSegWrite,
    /// 同上，但直接以退出码 42 退出进程（真实崩溃，供子进程重启测试使用）。
    ExitAfterSegWrite,
}

/// 磁盘上的不可变有序段（完整加载在内存中）。
#[derive(Debug)]
pub struct Segment {
    pub id: u64,
    pub level: usize,
    pub file_name: String,
    pub entries: Vec<Entry>, // 已按 key 排序，单键单版本
    pub min_key: Option<String>,
    pub max_key: Option<String>,
}

impl Segment {
    fn contains_key_hint(&self, key: &str) -> bool {
        match (&self.min_key, &self.max_key) {
            (Some(lo), Some(hi)) => key >= lo.as_str() && key <= hi.as_str(),
            _ => false,
        }
    }

    fn get(&self, key: &str) -> Option<&Entry> {
        if !self.contains_key_hint(key) {
            return None;
        }
        self.entries
            .binary_search_by(|e| e.key.as_str().cmp(key))
            .ok()
            .map(|i| &self.entries[i])
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct SegHeader {
    format: String,
    version: u32,
    id: u64,
    level: usize,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct SegRow {
    k: String,
    s: u64,
    // None（JSON null 或缺省）即墓碑
    v: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct Manifest {
    version: u32,
    segments: Vec<ManifestSeg>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct ManifestSeg {
    id: u64,
    level: usize,
    file: String,
    min_key: Option<String>,
    max_key: Option<String>,
    entries: usize,
}

struct Inner {
    mem: BTreeMap<String, Entry>,
    segs: Vec<Segment>, // 读取顺序：level 升序、同 level id 降序（新 -> 老）
    next_id: u64,
    max_mem: usize,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SegView {
    pub id: u64,
    pub level: usize,
    pub file: String,
    pub entries: usize,
    pub min_key: Option<String>,
    pub max_key: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct StateView {
    pub memtable_len: usize,
    pub next_seq: u64,
    pub next_segment_id: u64,
    pub segments: Vec<SegView>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct KeyValue {
    pub key: String,
    pub value: String,
    pub seq: u64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct GetResult {
    pub key: String,
    pub value: String,
    pub seq: u64,
    pub source: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct CompactReport {
    pub level: usize,
    pub input_segments: Vec<u64>,
    /// 输出段 id；None 表示归并结果为空、未产生新段（仅删除输入段）。
    pub output_segment: Option<u64>,
    pub output_entries: usize,
    pub tombstones_kept: usize,
    pub tombstones_dropped: usize,
}

pub struct Engine {
    root: PathBuf,
    inner: Mutex<Inner>,
    next_seq: AtomicU64,
}

fn seg_file_name(id: u64, level: usize) -> String {
    format!("seg-{:06}-L{}.jsonl", id, level)
}

fn fsync_dir(path: &Path) -> std::io::Result<()> {
    let f = OpenOptions::new().read(true).open(path)?;
    f.sync_all()
}

impl Engine {
    /// 打开（必要时创建）数据目录并从 manifest 恢复。
    pub fn open(root: impl Into<PathBuf>, max_mem: usize) -> std::io::Result<Engine> {
        let root = root.into();
        fs::create_dir_all(&root)?;

        let mut segs: Vec<Segment> = Vec::new();
        let manifest_path = root.join("manifest.json");
        let mut known_files: std::collections::HashSet<String> = std::collections::HashSet::new();

        if manifest_path.exists() {
            let mf: Manifest = serde_json::from_slice(&fs::read(&manifest_path)?)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
            for ms in mf.segments {
                let path = root.join(&ms.file);
                let seg = load_segment(&path, ms.id, ms.level)?;
                known_files.insert(ms.file);
                segs.push(seg);
            }
        }

        // 清理孤儿段文件：段已 fsync 但 manifest 切换前崩溃留下的文件。
        // 注意 next_id 也要把孤儿 id 算进去，避免新段复用其文件名。
        let mut max_id: u64 = segs.iter().map(|s| s.id).max().unwrap_or(0);
        for entry in fs::read_dir(&root)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().to_string();
            if let Some(id) = parse_seg_file_name(&name) {
                max_id = max_id.max(id);
                if !known_files.contains(&name) {
                    let _ = fs::remove_file(entry.path());
                }
            }
        }

        let max_seq = segs
            .iter()
            .flat_map(|s| s.entries.iter().map(|e| e.seq))
            .max();

        order_segments(&mut segs);

        Ok(Engine {
            root,
            inner: Mutex::new(Inner {
                mem: BTreeMap::new(),
                segs,
                next_id: max_id + 1,
                max_mem: max_mem.max(1),
            }),
            next_seq: AtomicU64::new(max_seq.map(|m| m + 1).unwrap_or(0)),
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn bump_seq(&self) -> u64 {
        self.next_seq.fetch_add(1, Ordering::SeqCst)
    }

    pub fn put(&self, key: String, value: String) -> std::io::Result<u64> {
        let seq = self.bump_seq();
        let mut g = self.lock();
        g.mem.insert(
            key.clone(),
            Entry {
                key,
                seq,
                value: Some(value),
            },
        );
        let full = g.mem.len() >= g.max_mem;
        if full {
            flush_locked(&self.root, &mut g)?;
        }
        Ok(seq)
    }

    pub fn delete(&self, key: String) -> std::io::Result<u64> {
        let seq = self.bump_seq();
        let mut g = self.lock();
        g.mem.insert(
            key.clone(),
            Entry {
                key,
                seq,
                value: None,
            },
        );
        let full = g.mem.len() >= g.max_mem;
        if full {
            flush_locked(&self.root, &mut g)?;
        }
        Ok(seq)
    }

    pub fn get(&self, key: &str) -> Option<GetResult> {
        let g = self.lock();
        if let Some(e) = g.mem.get(key) {
            return e.value.as_ref().map(|v| GetResult {
                key: key.to_string(),
                value: v.clone(),
                seq: e.seq,
                source: "memtable".to_string(),
            });
        }
        for seg in &g.segs {
            if let Some(e) = seg.get(key) {
                return e.value.as_ref().map(|v| GetResult {
                    key: key.to_string(),
                    value: v.clone(),
                    seq: e.seq,
                    source: format!("segment:{}:L{}", seg.id, seg.level),
                });
            }
        }
        None
    }

    /// 范围扫描：start 包含、end 不包含，按 key 升序，跨全部层按 seq 取最新版本，
    /// 墓碑键不输出（同一键只输出一次，杜绝重复）。
    pub fn range(&self, start: Option<&str>, end: Option<&str>, limit: Option<usize>) -> Vec<KeyValue> {
        let g = self.lock();
        let mut latest: BTreeMap<String, Entry> = BTreeMap::new();

        let mut consider = |e: &Entry| {
            let in_range = start.map_or(true, |s| e.key.as_str() >= s)
                && end.map_or(true, |en| e.key.as_str() < en);
            if !in_range {
                return;
            }
            match latest.get(&e.key) {
                Some(cur) if cur.seq >= e.seq => {}
                _ => {
                    latest.insert(e.key.clone(), e.clone());
                }
            }
        };

        // 老数据先入、新数据后覆盖；直接用 seq 比较，顺序无所谓，这里按物理顺序即可。
        for seg in &g.segs {
            for e in &seg.entries {
                consider(e);
            }
        }
        for (_, e) in &g.mem {
            consider(e);
        }

        latest
            .into_values()
            .filter(|e| !e.is_tombstone())
            .filter_map(|e| {
                e.value.map(|v| KeyValue {
                    key: e.key,
                    value: v,
                    seq: e.seq,
                })
            })
            .take(limit.unwrap_or(usize::MAX))
            .collect()
    }

    /// 把 memtable 刷成一个新的 L0 段并原子发布。返回新段 id（memtable 为空时返回 None）。
    pub fn flush(&self) -> std::io::Result<Option<u64>> {
        let mut g = self.lock();
        flush_locked(&self.root, &mut g)
    }

    pub fn state(&self) -> StateView {
        let g = self.lock();
        StateView {
            memtable_len: g.mem.len(),
            next_seq: self.next_seq.load(Ordering::SeqCst),
            next_segment_id: g.next_id,
            segments: g
                .segs
                .iter()
                .map(|s| SegView {
                    id: s.id,
                    level: s.level,
                    file: s.file_name.clone(),
                    entries: s.entries.len(),
                    min_key: s.min_key.clone(),
                    max_key: s.max_key.clone(),
                })
                .collect(),
        }
    }

    /// 合并 level n 与 level n+1 的全部段为一个新的 level n+1 段。
    ///
    /// 墓碑规则：归并后某键的胜出版本是墓碑时，只有在 level > n+1 的更老层中
    /// 确认不存在同键，才丢弃该墓碑（连同该键一起消失）；否则必须保留墓碑，
    /// 防止更老层的旧值在后续读取/合并中复活。
    pub fn compact(&self, level: usize, fault: Fault) -> std::io::Result<CompactReport> {
        let mut g = self.lock();

        // 1. 选出参与合并的输入段。
        let input_ids: std::collections::BTreeSet<u64> = g
            .segs
            .iter()
            .filter(|s| s.level == level || s.level == level + 1)
            .map(|s| s.id)
            .collect();

        if input_ids.is_empty() {
            return Ok(CompactReport {
                level,
                input_segments: vec![],
                output_segment: None,
                output_entries: 0,
                tombstones_kept: 0,
                tombstones_dropped: 0,
            });
        }

        // 2. 归并：按读取顺序（新 -> 老）首次出现即为最新版本；用 seq 再兜底比较。
        let mut merged: BTreeMap<String, Entry> = BTreeMap::new();
        for seg in read_order_iter(&g.segs) {
            if !input_ids.contains(&seg.id) {
                continue;
            }
            for e in &seg.entries {
                match merged.get(&e.key) {
                    Some(cur) if cur.seq >= e.seq => {}
                    _ => {
                        merged.insert(e.key.clone(), e.clone());
                    }
                }
            }
        }

        // 3. 检查更老层（level > n+1）是否存在同键。
        let older: Vec<&Segment> = g.segs.iter().filter(|s| s.level > level + 1).collect();
        let exists_in_older = |key: &str| older.iter().any(|s| s.get(key).is_some());

        let mut kept = 0usize;
        let mut dropped = 0usize;
        let output: Vec<Entry> = merged
            .into_values()
            .filter(|e| {
                if e.is_tombstone() {
                    if exists_in_older(&e.key) {
                        kept += 1;
                        true // 更老层有同键：墓碑必须保留
                    } else {
                        dropped += 1;
                        false // 确认更老层无同键：安全丢弃
                    }
                } else {
                    true
                }
            })
            .collect();

        // 4. 写新段文件（若输出非空），fsync；此时 manifest 尚未切换。
        let new_id = g.next_id;
        g.next_id += 1;
        let new_level = level + 1;

        let output_id = if output.is_empty() {
            None
        } else {
            write_segment(&self.root, new_id, new_level, &output)?;
            // 故障注入点：文件已持久化、清单未切换。真实系统此时掉电。
            if fault != Fault::None {
                // 先让目录项落盘，尽量贴近“段文件已完整提交”的现场。
                let _ = fsync_dir(&self.root);
                if fault == Fault::ExitAfterSegWrite {
                    eprintln!(
                        "[fault] segment {} written & fsynced, exiting before manifest switch",
                        new_id
                    );
                    std::process::exit(42);
                } else {
                    panic!(
                        "[fault] segment {} written & fsynced, panicking before manifest switch",
                        new_id
                    );
                }
            }
            Some(new_id)
        };

        // 5. 原子切换 manifest：移除输入段、登记新段。
        let removed_files: Vec<String> = g
            .segs
            .iter()
            .filter(|s| input_ids.contains(&s.id))
            .map(|s| s.file_name.clone())
            .collect();
        let before: Vec<Segment> = g.segs.drain(..).collect();
        let mut kept_segs: Vec<Segment> = before
            .into_iter()
            .filter(|s| !input_ids.contains(&s.id))
            .collect();

        if let Some(id) = output_id {
            let file_name = seg_file_name(id, new_level);
            let seg = load_segment(&self.root.join(&file_name), id, new_level)?;
            kept_segs.push(seg);
        }
        order_segments(&mut kept_segs);
        write_manifest(&self.root, &kept_segs)?;
        g.segs = kept_segs;

        // 清单已切换并持久化后，删除被取代的输入段文件；删除失败无妨，
        // 下次启动会作为孤儿文件清理（manifest 是唯一真相来源）。
        for name in removed_files {
            let _ = fs::remove_file(self.root.join(&name));
        }

        Ok(CompactReport {
            level,
            input_segments: input_ids.into_iter().collect(),
            output_segment: output_id,
            output_entries: output.len(),
            tombstones_kept: kept,
            tombstones_dropped: dropped,
        })
    }
}

fn flush_locked(root: &Path, g: &mut Inner) -> std::io::Result<Option<u64>> {
    if g.mem.is_empty() {
        return Ok(None);
    }
    let entries: Vec<Entry> = g.mem.values().cloned().collect(); // BTreeMap 已按 key 升序
    let id = g.next_id;
    g.next_id += 1;

    write_segment(root, id, 0, &entries)?;
    let file_name = seg_file_name(id, 0);
    let seg = load_segment(&root.join(&file_name), id, 0)?;

    let mut new_segs = std::mem::take(&mut g.segs);
    new_segs.push(seg);
    order_segments(&mut new_segs);
    write_manifest(root, &new_segs)?;
    g.segs = new_segs;
    g.mem.clear();
    Ok(Some(id))
}

fn read_order_iter(segs: &[Segment]) -> impl Iterator<Item = &Segment> {
    // level 升序、同 level id 大的在前（新 -> 老）。
    let mut order: Vec<&Segment> = segs.iter().collect();
    order.sort_by(|a, b| a.level.cmp(&b.level).then(b.id.cmp(&a.id)));
    order.into_iter()
}

fn order_segments(segs: &mut Vec<Segment>) {
    segs.sort_by(|a, b| a.level.cmp(&b.level).then(b.id.cmp(&a.id)));
}

fn parse_seg_file_name(name: &str) -> Option<u64> {
    let rest = name.strip_prefix("seg-")?;
    let rest = rest.strip_suffix(".jsonl")?;
    let (id_part, _level) = rest.split_once('-')?;
    id_part.parse::<u64>().ok()
}

fn write_segment(root: &Path, id: u64, level: usize, entries: &[Entry]) -> std::io::Result<()> {
    let final_name = seg_file_name(id, level);
    let tmp_name = format!("{final_name}.tmp");
    let tmp_path = root.join(&tmp_name);
    let final_path = root.join(&final_name);

    {
        let mut f = File::create(&tmp_path)?;
        writeln!(
            f,
            "{}",
            serde_json::to_string(&SegHeader {
                format: "lsm-seg".to_string(),
                version: 1,
                id,
                level,
            })?
        )?;
        for e in entries {
            writeln!(
                f,
                "{}",
                serde_json::to_string(&SegRow {
                    k: e.key.clone(),
                    s: e.seq,
                    v: e.value.clone(),
                })?
            )?;
        }
        f.flush()?;
        f.sync_all()?;
    }
    fs::rename(&tmp_path, &final_path)?;
    fsync_dir(root)?;
    Ok(())
}

fn load_segment(path: &Path, want_id: u64, want_level: usize) -> std::io::Result<Segment> {
    let f = File::open(path)?;
    let reader = BufReader::new(f);
    let mut lines = reader.lines();

    let header: SegHeader = serde_json::from_str(
        &lines
            .next()
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "empty segment"))??,
    )
    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    if header.format != "lsm-seg" || header.id != want_id || header.level != want_level {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("segment header mismatch in {}", path.display()),
        ));
    }

    let mut entries = Vec::new();
    for line in lines {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let row: SegRow =
            serde_json::from_str(&line).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        entries.push(Entry {
            key: row.k,
            seq: row.s,
            value: row.v,
        });
    }

    // 校验有序且无重复键（不可变段的核心不变量）。
    for w in entries.windows(2) {
        if w[0].key >= w[1].key {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("segment {} not strictly sorted by key", path.display()),
            ));
        }
    }

    let min_key = entries.first().map(|e| e.key.clone());
    let max_key = entries.last().map(|e| e.key.clone());
    Ok(Segment {
        id: want_id,
        level: want_level,
        file_name: path.file_name().unwrap().to_string_lossy().to_string(),
        entries,
        min_key,
        max_key,
    })
}

fn write_manifest(root: &Path, segs: &[Segment]) -> std::io::Result<()> {
    let mf = Manifest {
        version: 1,
        segments: segs
            .iter()
            .map(|s| ManifestSeg {
                id: s.id,
                level: s.level,
                file: s.file_name.clone(),
                min_key: s.min_key.clone(),
                max_key: s.max_key.clone(),
                entries: s.entries.len(),
            })
            .collect(),
    };
    let tmp = root.join("manifest.json.tmp");
    let final_path = root.join("manifest.json");
    {
        let mut f = File::create(&tmp)?;
        f.write_all(&serde_json::to_vec_pretty(&mf)?)?;
        f.flush()?;
        f.sync_all()?;
    }
    fs::rename(&tmp, &final_path)?;
    fsync_dir(root)?;
    Ok(())
}
