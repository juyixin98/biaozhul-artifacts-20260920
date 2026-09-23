//! 段文件与 MANIFEST 持久化。
//!
//! 段文件：`segments/<id>.seg`，JSON Lines，每行一个 [`Entry`]，按 key 排序、同键仅保留最新版本。
//! 清单文件：`MANIFEST`，JSON，记录全部存活段（新 -> 旧）以及下一个可用段 id。
//!
//! 发布协议（崩溃安全）：
//! 1. 写段文件到 `segments/<id>.seg.tmp`，flush + fsync；
//! 2. rename 为 `segments/<id>.seg`，并对 `segments/` 目录 fsync；
//! 3. 写 `MANIFEST.tmp`（flush + fsync）-> rename 为 `MANIFEST` -> fsync 数据目录。
//!
//! 在任何一步崩溃：
//! - 旧 MANIFEST 仍完整，重启后只看到旧段（合并中的新段即使已 rename 也会被当成孤儿清理/忽略）；
//! - 已发布的新段一定会被新 MANIFEST 引用。

use crate::segment::Entry;
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

/// 一个已发布的不可变段。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SegmentMeta {
    pub id: u64,
    /// 段内条目数（不含被合并掉的旧版本）。
    pub count: usize,
    /// 最小/最大 key，便于扫描剪枝。
    pub min_key: String,
    pub max_key: String,
}

/// MANIFEST 内容。段顺序：新 -> 旧（与内存中一致）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ManifestData {
    /// 下一个写入版本号（重启后继续单调递增）。
    pub next_seq: u64,
    /// 下一个可分配的段 id。
    pub next_id: u64,
    /// 全部存活段，新 -> 旧。
    pub segments: Vec<SegmentMeta>,
}

impl Default for ManifestData {
    fn default() -> Self {
        Self {
            next_seq: 0,
            next_id: 1,
            segments: Vec::new(),
        }
    }
}

/// 磁盘布局辅助。
pub struct Layout {
    pub dir: PathBuf,
}

impl Layout {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    pub fn manifest_path(&self) -> PathBuf {
        self.dir.join("MANIFEST")
    }

    pub fn manifest_tmp(&self) -> PathBuf {
        self.dir.join("MANIFEST.tmp")
    }

    pub fn seg_dir(&self) -> PathBuf {
        self.dir.join("segments")
    }

    pub fn seg_path(&self, id: u64) -> PathBuf {
        self.seg_dir().join(format!("{id}.seg"))
    }

    /// 打开（必要时创建）数据目录并加载清单。
    pub fn open(&self) -> Result<ManifestData> {
        fs::create_dir_all(self.seg_dir())
            .with_context(|| format!("create data dir {:?}", self.dir))?;
        fsync_dir(&self.seg_dir())?;

        let data = match File::open(self.manifest_path()) {
            Ok(f) => {
                let f = BufReader::new(f);
                serde_json::from_reader(f).context("parse MANIFEST")?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => ManifestData::default(),
            Err(e) => return Err(e).context("open MANIFEST"),
        };

        // 崩溃恢复：删除任何未被清单引用的孤儿段文件（含合并中途发布、未来得及更新清单的段）。
        self.garbage_collect_orphans(&data)?;
        // 清理可能残留的 MANIFEST 临时文件。
        let _ = fs::remove_file(self.manifest_tmp());
        Ok(data)
    }

    fn garbage_collect_orphans(&self, data: &ManifestData) -> Result<()> {
        let live: std::collections::HashSet<u64> = data.segments.iter().map(|s| s.id).collect();
        for entry in fs::read_dir(self.seg_dir())? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|x| x.to_str()) != Some("seg") {
                // 顺手清掉 *.tmp
                if path.extension().and_then(|x| x.to_str()) == Some("tmp") {
                    let _ = fs::remove_file(&path);
                }
                continue;
            }
            let id = path
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|s| s.parse::<u64>().ok());
            match id {
                Some(id) if live.contains(&id) => {}
                _ => {
                    fs::remove_file(&path)
                        .with_context(|| format!("remove orphan segment {:?}", path))?;
                }
            }
        }
        Ok(())
    }

    /// 读取一个段的全部条目。
    pub fn read_segment(&self, id: u64) -> Result<Vec<Entry>> {
        let file = File::open(self.seg_path(id)).with_context(|| format!("open segment {id}"))?;
        let reader = BufReader::new(file);
        let mut out = Vec::new();
        for (i, line) in reader.lines().enumerate() {
            let line = line.with_context(|| format!("read segment {id} line {}", i + 1))?;
            if line.trim().is_empty() {
                continue;
            }
            let entry: Entry = serde_json::from_str(&line)
                .with_context(|| format!("parse segment {id} line {}", i + 1))?;
            out.push(entry);
        }
        Ok(out)
    }

    /// 将一批已按 key 排序、去重的条目持久化为新段文件（tmp + fsync + rename）。
    pub fn write_segment(&self, id: u64, entries: &[Entry]) -> Result<SegmentMeta> {
        if entries.is_empty() {
            return Err(anyhow!("refuse to write empty segment {id}"));
        }
        let tmp = self.seg_dir().join(format!("{id}.seg.tmp"));
        let final_path = self.seg_path(id);
        {
            let mut file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp)
                .with_context(|| format!("create {:?}", tmp))?;
            for e in entries {
                serde_json::to_writer(&mut file, e)?;
                file.write_all(b"\n")?;
            }
            file.flush()?;
            file.sync_all()?;
        }
        fs::rename(&tmp, &final_path)
            .with_context(|| format!("rename {:?} -> {:?}", tmp, final_path))?;
        fsync_dir(&self.seg_dir())?;

        let (min_key, max_key) = (
            entries.first().unwrap().key.clone(),
            entries.last().unwrap().key.clone(),
        );
        Ok(SegmentMeta {
            id,
            count: entries.len(),
            min_key,
            max_key,
        })
    }

    /// 原子发布新清单（tmp + fsync + rename + 目录 fsync）。
    pub fn publish_manifest(&self, data: &ManifestData) -> Result<()> {
        let tmp = self.manifest_tmp();
        {
            let mut file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp)?;
            serde_json::to_writer_pretty(&mut file, data)?;
            file.write_all(b"\n")?;
            file.flush()?;
            file.sync_all()?;
        }
        fs::rename(&tmp, self.manifest_path())?;
        fsync_dir(&self.dir)?;
        Ok(())
    }

    /// 删除段文件（用于合并后删除被替换的旧段）。
    pub fn delete_segment(&self, id: u64) -> Result<()> {
        let path = self.seg_path(id);
        match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e).with_context(|| format!("delete segment {id}")),
        }
    }
}

/// fsync 一个目录（保证其中 rename/create 元数据落盘）。
fn fsync_dir(path: &Path) -> Result<()> {
    let file = File::open(path)?;
    file.sync_all()?;
    Ok(())
}

/// 合并多路有序条目流：同键取 seq 最大者。
///
/// `sources` 必须已按“新 -> 旧”排列；每个源内部按 key 排序且同键唯一。
/// 输出按 key 排序，每个键仅一条（取全局 seq 最大的那条，墓碑同样保留）。
pub fn merge_latest_per_key(sources: &[Vec<Entry>]) -> Vec<Entry> {
    // 每源游标
    let n = sources.len();
    let mut cursors = vec![0usize; n];
    let mut out: Vec<Entry> = Vec::new();

    loop {
        // 找所有源当前 key 中最小的
        let mut min_key: Option<&str> = None;
        for (i, src) in sources.iter().enumerate() {
            if cursors[i] < src.len() {
                let k = &src[cursors[i]].key;
                match min_key {
                    None => min_key = Some(k),
                    Some(mk) if k.as_str() < mk => min_key = Some(k),
                    _ => {}
                }
            }
        }
        let min_key = match min_key {
            Some(k) => k.to_string(),
            None => break,
        };

        // 收集所有源中 key == min_key 的当前条目，按 seq 取最大。
        let mut best: Option<Entry> = None;
        for (i, src) in sources.iter().enumerate() {
            while cursors[i] < src.len() && src[cursors[i]].key == min_key {
                let e = &src[cursors[i]];
                if best.as_ref().is_none_or(|b| e.seq > b.seq) {
                    best = Some(e.clone());
                }
                cursors[i] += 1;
            }
        }
        if let Some(e) = best {
            out.push(e);
        }
    }

    out
}
