//! 文件系统存储层：上传会话、分块、不可变对象发布、崩溃恢复。
//!
//! ## 目录布局
//!
//! ```text
//! <data-dir>/
//!   uploads/<upload_id>/
//!       meta.json          # 会话元数据（原子 rename 写入）
//!       0.part             # 各分块（先写 .tmp，fsync 后 rename）
//!       1.part ...
//!   objects/<sha256-hex>   # 已发布的不可变对象（临时文件 fsync 后 rename 进来）
//!   objects/<sha256-hex>.meta.json
//! ```
//!
//! 崩溃安全要点：所有元数据与数据都走“写临时文件 → fsync → rename → fsync 目录”，
//! 因此重启后目录中出现的任何文件要么完整、要么是待清理的临时文件/待收养的孤儿分块。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::fs;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::error::{AppError, AppResult};

/// 单个分块的描述。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChunkInfo {
    /// 该分块期望（=实际）字节数
    pub size: u64,
    /// 该分块内容的 sha256 十六进制
    pub sha256: String,
    /// 收到时间（RFC3339 UTC）
    pub received_at: String,
}

/// 上传会话元数据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UploadMeta {
    pub upload_id: String,
    /// 对象整体 sha256（创建会话时由客户端声明，发布时校验）
    pub sha256: String,
    pub total_size: u64,
    /// 编号 -> 分块大小；编号从 0 开始，必须连续
    pub chunk_sizes: BTreeMap<u32, u64>,
    /// 已落盘并校验通过的分块
    pub chunks: BTreeMap<u32, ChunkInfo>,
    pub created_at: String,
    /// 完成（整体校验通过、对象发布）后置为 true
    pub committed: bool,
}

impl UploadMeta {
    /// 分块总数（由 total_size 与 chunk_sizes 推导，chunk_sizes 必须填满 0..k）。
    pub fn chunk_count(&self) -> Option<u32> {
        let k = self.chunk_sizes.len() as u32;
        for i in 0..k {
            if !self.chunk_sizes.contains_key(&i) {
                return None;
            }
        }
        if self.chunk_sizes.values().sum::<u64>() != self.total_size {
            return None;
        }
        Some(k)
    }

    /// 缺失分块编号（升序）。
    pub fn missing_chunks(&self) -> Vec<u32> {
        let k = self.chunk_sizes.len() as u32;
        (0..k).filter(|i| !self.chunks.contains_key(i)).collect()
    }
}

#[derive(Clone)]
pub struct Store {
    root: PathBuf,
}

/// 发布/读取对象时附带的元数据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectMeta {
    pub sha256: String,
    pub size: u64,
}

/// 完成发布的结果。
pub struct CommitOutcome {
    pub object_id: String,
    pub already_committed: bool,
}

impl Store {
    pub async fn new(root: impl Into<PathBuf>) -> AppResult<Self> {
        let root = root.into();
        fs::create_dir_all(root.join("uploads"))
            .await
            .map_err(AppError::internal)?;
        fs::create_dir_all(root.join("objects"))
            .await
            .map_err(AppError::internal)?;
        let store = Self { root };
        store.recover().await?;
        Ok(store)
    }

    pub fn data_dir(&self) -> &Path {
        &self.root
    }

    fn uploads_dir(&self) -> PathBuf {
        self.root.join("uploads")
    }

    fn objects_dir(&self) -> PathBuf {
        self.root.join("objects")
    }

    fn upload_dir(&self, upload_id: &str) -> PathBuf {
        self.uploads_dir().join(upload_id)
    }

    pub fn meta_path(&self, upload_id: &str) -> PathBuf {
        self.upload_dir(upload_id).join("meta.json")
    }

    fn part_path(&self, upload_id: &str, index: u32) -> PathBuf {
        self.upload_dir(upload_id).join(format!("{index}.part"))
    }

    fn part_tmp_path(&self, upload_id: &str, index: u32) -> PathBuf {
        self.upload_dir(upload_id).join(format!("{index}.part.tmp"))
    }

    fn object_path(&self, object_id: &str) -> PathBuf {
        self.objects_dir().join(object_id)
    }

    fn object_meta_path(&self, object_id: &str) -> PathBuf {
        self.objects_dir().join(format!("{object_id}.meta.json"))
    }

    // ---------- 元数据原子读写 ----------

    pub async fn load_meta(&self, upload_id: &str) -> AppResult<UploadMeta> {
        let p = self.meta_path(upload_id);
        match fs::read(&p).await {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| AppError::internal(format!("corrupt meta.json for {upload_id}: {e}"))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(AppError::not_found(
                format!("upload '{upload_id}' not found"),
            )),
            Err(e) => Err(AppError::internal(e)),
        }
    }

    /// 原子写：临时文件 fsync 后 rename，再尽力 fsync 目录。
    async fn atomic_write(&self, target: &Path, bytes: &[u8]) -> AppResult<()> {
        let dir = target.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(dir).await.map_err(AppError::internal)?;
        let tmp = dir.join(format!(
            ".{}.tmp-{}",
            target
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("file"),
            uuid::Uuid::new_v4().simple()
        ));
        {
            let mut f = fs::File::create(&tmp).await.map_err(AppError::internal)?;
            f.write_all(bytes).await.map_err(AppError::internal)?;
            f.flush().await.map_err(AppError::internal)?;
            f.sync_all().await.map_err(AppError::internal)?;
        }
        fs::rename(&tmp, target).await.map_err(AppError::internal)?;
        fsync_dir(dir).await;
        Ok(())
    }

    pub async fn save_meta(&self, meta: &UploadMeta) -> AppResult<()> {
        let bytes = serde_json::to_vec_pretty(meta).map_err(AppError::internal)?;
        self.atomic_write(&self.meta_path(&meta.upload_id), &bytes)
            .await
    }

    // ---------- 会话生命周期 ----------

    pub async fn create_upload(
        &self,
        upload_id: String,
        sha256: String,
        total_size: u64,
        chunk_sizes: BTreeMap<u32, u64>,
        created_at: String,
    ) -> AppResult<UploadMeta> {
        let dir = self.upload_dir(&upload_id);
        fs::create_dir_all(&dir).await.map_err(AppError::internal)?;
        let meta = UploadMeta {
            upload_id: upload_id.clone(),
            sha256,
            total_size,
            chunk_sizes,
            chunks: BTreeMap::new(),
            created_at,
            committed: false,
        };
        self.save_meta(&meta).await?;
        Ok(meta)
    }

    /// 接收一个分块。
    ///
    /// - 流式落盘（先 `.tmp`），同时计算 sha256 与字节数；
    /// - 超过 `max_chunk_size` 立即中止（413）；
    /// - 大小不符 400；摘要不符 409 并删除临时文件；
    /// - 同编号同内容：幂等成功（不重新落盘）；同编号异内容：409 拒绝。
    pub async fn put_chunk<S, E>(
        &self,
        meta: &mut UploadMeta,
        index: u32,
        expected_sha: &str,
        max_chunk_size: u64,
        mut stream: S,
    ) -> AppResult<(bool, ChunkInfo)>
    where
        S: futures_util::Stream<Item = Result<axum::body::Bytes, E>> + Unpin,
        E: std::fmt::Display,
    {
        use futures_util::StreamExt;

        let expected_size = *meta.chunk_sizes.get(&index).ok_or_else(|| {
            AppError::bad_request(format!(
                "chunk index {index} out of range (0..{})",
                meta.chunk_sizes.len()
            ))
        })?;

        // 已存在的分块：仅做幂等/冲突判定，不重新落盘。
        if let Some(existing) = meta.chunks.get(&index) {
            if existing.sha256.eq_ignore_ascii_case(expected_sha) && existing.size == expected_size
            {
                return Ok((true, existing.clone()));
            }
            return Err(AppError::conflict(format!(
                "chunk {index} already uploaded with different content"
            )));
        }

        if meta.committed {
            return Err(AppError::conflict("upload already committed"));
        }

        let tmp = self.part_tmp_path(&meta.upload_id, index);
        let mut file = fs::File::create(&tmp).await.map_err(AppError::internal)?;
        let mut hasher = Sha256::new();
        let mut received: u64 = 0;
        let mut too_large = false;

        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|e| AppError::internal(e))?;
            received = received.saturating_add(chunk.len() as u64);
            if received > expected_size || received > max_chunk_size {
                too_large = true;
                break;
            }
            hasher.update(&chunk);
            file.write_all(&chunk).await.map_err(AppError::internal)?;
        }

        if too_large {
            drop(file);
            let _ = fs::remove_file(&tmp).await;
            if received > max_chunk_size {
                return Err(AppError::payload_too_large(format!(
                    "chunk {index} exceeds server max chunk size {max_chunk_size}"
                )));
            }
            return Err(AppError::bad_request(format!(
                "chunk {index} larger than declared size {expected_size}"
            )));
        }

        file.flush().await.map_err(AppError::internal)?;
        file.sync_all().await.map_err(AppError::internal)?;
        drop(file);

        if received != expected_size {
            let _ = fs::remove_file(&tmp).await;
            return Err(AppError::bad_request(format!(
                "chunk {index} size mismatch: got {received} bytes, expected {expected_size}"
            )));
        }

        let actual_sha = hex::encode(hasher.finalize());
        if !actual_sha.eq_ignore_ascii_case(expected_sha) {
            let _ = fs::remove_file(&tmp).await;
            return Err(AppError::conflict(format!(
                "chunk {index} sha256 mismatch: got {actual_sha}, client declared {expected_sha}"
            )));
        }

        let final_path = self.part_path(&meta.upload_id, index);
        fs::rename(&tmp, &final_path)
            .await
            .map_err(AppError::internal)?;
        fsync_dir(&self.upload_dir(&meta.upload_id)).await;

        let info = ChunkInfo {
            size: expected_size,
            sha256: actual_sha,
            received_at: now_rfc3339(),
        };
        meta.chunks.insert(index, info.clone());
        self.save_meta(meta).await?;
        Ok((false, info))
    }

    /// 完成上传：缺块报错；按序串接重算整体长度与 sha256，通过后原子发布不可变对象。
    pub async fn commit(&self, meta: &mut UploadMeta) -> AppResult<CommitOutcome> {
        // 已发布且对象存在：幂等成功。
        if meta.committed && self.object_exists(&meta.sha256).await {
            return Ok(CommitOutcome {
                object_id: meta.sha256.clone(),
                already_committed: true,
            });
        }

        let k = meta
            .chunk_count()
            .ok_or_else(|| AppError::bad_request("invalid session chunk layout"))?;
        let missing: Vec<u32> = (0..k).filter(|i| !meta.chunks.contains_key(i)).collect();
        if !missing.is_empty() {
            return Err(AppError::bad_request(format!(
                "cannot commit: missing chunks {missing:?}"
            )));
        }

        // 按序串接到 objects 临时文件，同时计算整体哈希。
        let tmp_obj = self
            .objects_dir()
            .join(format!(".tmp.obj.{}", uuid::Uuid::new_v4().simple()));
        let mut out = fs::File::create(&tmp_obj)
            .await
            .map_err(AppError::internal)?;
        let mut hasher = Sha256::new();
        let mut total: u64 = 0;
        let mut buf = vec![0u8; 1024 * 1024];
        for i in 0..k {
            let mut f = fs::File::open(self.part_path(&meta.upload_id, i))
                .await
                .map_err(AppError::internal)?;
            loop {
                let n = f.read(&mut buf).await.map_err(AppError::internal)?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
                total += n as u64;
                out.write_all(&buf[..n]).await.map_err(AppError::internal)?;
            }
        }
        out.flush().await.map_err(AppError::internal)?;
        out.sync_all().await.map_err(AppError::internal)?;
        drop(out);

        if total != meta.total_size {
            let _ = fs::remove_file(&tmp_obj).await;
            return Err(AppError::conflict(format!(
                "total length mismatch: assembled {total}, declared {}",
                meta.total_size
            )));
        }
        let actual = hex::encode(hasher.finalize());
        if !actual.eq_ignore_ascii_case(&meta.sha256) {
            let _ = fs::remove_file(&tmp_obj).await;
            return Err(AppError::conflict(format!(
                "whole-object sha256 mismatch: assembled {actual}, declared {}",
                meta.sha256
            )));
        }

        // 先发对象元数据，再 rename 对象，最后标记会话已提交（顺序崩溃安全）。
        let obj_meta = serde_json::to_vec_pretty(&ObjectMeta {
            sha256: actual.clone(),
            size: total,
        })
        .map_err(AppError::internal)?;
        self.atomic_write(&self.object_meta_path(&actual), &obj_meta)
            .await?;
        fs::rename(&tmp_obj, self.object_path(&actual))
            .await
            .map_err(AppError::internal)?;
        fsync_dir(&self.objects_dir()).await;

        meta.committed = true;
        self.save_meta(meta).await?;

        Ok(CommitOutcome {
            object_id: actual,
            already_committed: false,
        })
    }

    // ---------- 对象读取 ----------

    pub async fn object_exists(&self, object_id: &str) -> bool {
        if !is_object_id(object_id) {
            return false;
        }
        matches!(fs::metadata(self.object_path(object_id)).await, Ok(m) if m.is_file())
    }

    pub async fn open_object(&self, object_id: &str) -> AppResult<(ObjectMeta, fs::File)> {
        if !is_object_id(object_id) {
            return Err(AppError::not_found("object not found"));
        }
        let path = self.object_path(object_id);
        let file = match fs::File::open(&path).await {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(AppError::not_found("object not found"));
            }
            Err(e) => return Err(AppError::internal(e)),
        };
        let mut meta = match fs::read(self.object_meta_path(object_id)).await {
            Ok(b) => serde_json::from_slice(&b).unwrap_or_else(|_| ObjectMeta {
                sha256: object_id.to_string(),
                size: 0,
            }),
            Err(_) => ObjectMeta {
                sha256: object_id.to_string(),
                size: 0,
            },
        };
        if meta.size == 0 {
            meta.size = file.metadata().await.map(|m| m.len()).unwrap_or(0);
        }
        Ok((meta, file))
    }

    // ---------- 启动恢复 ----------

    /// 扫描目录，清理崩溃残留并收养孤儿分块。
    pub async fn recover(&self) -> AppResult<RecoveryReport> {
        let mut report = RecoveryReport::default();

        // 1) objects 目录：清理临时文件与孤儿 sidecar。
        let mut obj_entries = match fs::read_dir(self.objects_dir()).await {
            Ok(it) => it,
            Err(e) => return Err(AppError::internal(e)),
        };
        let mut object_ids = Vec::new();
        while let Some(ent) = obj_entries.next_entry().await.map_err(AppError::internal)? {
            let name = ent.file_name().to_string_lossy().to_string();
            if name.starts_with(".tmp.obj.") {
                let _ = fs::remove_file(ent.path()).await;
                report.temp_files_removed += 1;
            } else if let Some(id) = name.strip_suffix(".meta.json") {
                if !self.object_path(id).exists() {
                    let _ = fs::remove_file(ent.path()).await;
                    report.orphan_meta_removed += 1;
                }
            } else if is_object_id(&name) {
                object_ids.push(name);
            }
        }

        // 2) uploads 目录：逐会话恢复。
        let mut up_entries = match fs::read_dir(self.uploads_dir()).await {
            Ok(it) => it,
            Err(e) => return Err(AppError::internal(e)),
        };
        while let Some(ent) = up_entries.next_entry().await.map_err(AppError::internal)? {
            let dir = ent.path();
            if !dir.is_dir() {
                continue;
            }
            let upload_id = ent.file_name().to_string_lossy().to_string();
            if !is_upload_id(&upload_id) {
                continue;
            }

            let meta_path = dir.join("meta.json");
            if !meta_path.exists() {
                // 无元数据的孤儿目录：整目录移除（其中不会有已发布对象）。
                let _ = fs::remove_dir_all(&dir).await;
                report.orphan_dirs_removed += 1;
                continue;
            }
            let mut meta: UploadMeta = match fs::read(&meta_path).await {
                Ok(b) => match serde_json::from_slice(&b) {
                    Ok(m) => m,
                    Err(_) => {
                        // 元数据损坏：无法安全判断分块归属，隔离为 .corrupt 而非删除。
                        tracing::warn!("quarantining corrupt upload session {upload_id}");
                        let _ = fs::rename(&dir, dir.with_ext_name_corrupt()).await;
                        report.corrupt_sessions += 1;
                        continue;
                    }
                },
                Err(_) => continue,
            };

            let mut changed = false;
            let mut files = match fs::read_dir(&dir).await {
                Ok(it) => it,
                Err(_) => continue,
            };
            while let Some(f) = files.next_entry().await.map_err(AppError::internal)? {
                let name = f.file_name().to_string_lossy().to_string();
                if name.ends_with(".part.tmp") {
                    let _ = fs::remove_file(f.path()).await;
                    report.temp_files_removed += 1;
                    continue;
                }
                let Some(stem) = name.strip_suffix(".part") else {
                    continue;
                };
                let Ok(idx) = stem.parse::<u32>() else {
                    continue;
                };

                if meta.chunks.contains_key(&idx) {
                    continue; // 元数据已记录
                }
                // 孤儿分块（rename 后、meta 更新前崩溃）：校验大小与哈希后收养。
                let Some(&expected_size) = meta.chunk_sizes.get(&idx) else {
                    let _ = fs::remove_file(f.path()).await;
                    report.orphan_parts_removed += 1;
                    continue;
                };
                match fs::metadata(f.path()).await {
                    Ok(m) if m.len() == expected_size => {}
                    _ => {
                        let _ = fs::remove_file(f.path()).await;
                        report.orphan_parts_removed += 1;
                        continue;
                    }
                }
                match hash_file(&f.path()).await {
                    Ok(sha) => {
                        meta.chunks.insert(
                            idx,
                            ChunkInfo {
                                size: expected_size,
                                sha256: sha.clone(),
                                received_at: now_rfc3339(),
                            },
                        );
                        report.orphan_parts_adopted += 1;
                        changed = true;
                        tracing::info!(upload = %upload_id, chunk = idx, sha = %sha, "adopted orphan chunk after restart");
                    }
                    Err(_) => {
                        let _ = fs::remove_file(f.path()).await;
                        report.orphan_parts_removed += 1;
                    }
                }
            }
            if changed {
                self.save_meta(&meta).await?;
            }

            // 3) 已提交但对象缺失（发布中途崩溃）→ 重新组装发布；
            //    未提交但块已齐 → 不自动发布（必须由客户端显式 finish 触发）。
            if meta.committed && !object_ids.contains(&meta.sha256) {
                match self.commit(&mut meta).await {
                    Ok(_) => report.committed_after_restart += 1,
                    Err(e) => {
                        tracing::warn!(upload = %upload_id, error = %e, "could not re-publish committed object after restart");
                    }
                }
            }
        }
        Ok(report)
    }
}

// 给 PathBuf 加一个小工具，避免复杂的文件名拼接。
trait CorruptExt {
    fn with_ext_name_corrupt(&self) -> PathBuf;
}

impl CorruptExt for Path {
    fn with_ext_name_corrupt(&self) -> PathBuf {
        let mut n = self
            .file_name()
            .map(|s| s.to_os_string())
            .unwrap_or_default();
        n.push(".corrupt");
        self.with_file_name(n)
    }
}

#[derive(Debug, Default)]
pub struct RecoveryReport {
    pub temp_files_removed: u64,
    pub orphan_meta_removed: u64,
    pub orphan_dirs_removed: u64,
    pub orphan_parts_removed: u64,
    pub orphan_parts_adopted: u64,
    pub corrupt_sessions: u64,
    pub committed_after_restart: u64,
}

// ---------- 校验与工具 ----------

pub fn is_object_id(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn is_upload_id(s: &str) -> bool {
    s.len() == 36 && uuid::Uuid::parse_str(s).is_ok()
}

pub fn normalize_sha256(s: &str) -> Option<String> {
    let lower = s.to_ascii_lowercase();
    if is_object_id(&lower) {
        Some(lower)
    } else {
        None
    }
}

async fn hash_file(path: &Path) -> std::io::Result<String> {
    use tokio::io::AsyncReadExt;
    let mut f = fs::File::open(path).await?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = f.read(&mut buf).await?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// 尽力 fsync 目录（Linux 上保证 rename 落盘）；失败仅告警。
async fn fsync_dir(dir: &Path) {
    let dir = dir.to_path_buf();
    tokio::task::spawn_blocking(move || {
        if let Ok(f) = std::fs::OpenOptions::new().read(true).open(&dir) {
            if let Err(e) = f.sync_all() {
                tracing::debug!(dir = %dir.display(), error = %e, "fsync(dir) failed (non-fatal)");
            }
        }
    })
    .await
    .ok();
}

pub fn now_rfc3339() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // 轻量 UTC 时间戳，不引入 chrono
    format_unix_ts(secs)
}

fn format_unix_ts(secs: u64) -> String {
    // 将 unix 秒转换为 RFC3339（UTC），仅用整数运算。
    let days = (secs / 86400) as i64;
    let rem = secs % 86400;
    let (hour, min, sec) = (rem / 3600, (rem % 3600) / 60, rem % 60);

    // 1970-01-01 起的天数 -> 年月日（Howard Hinnant 算法）
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };

    format!("{year:04}-{m:02}-{d:02}T{hour:02}:{min:02}:{sec:02}Z")
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    #[test]
    fn unix_ts_formatting() {
        assert_eq!(format_unix_ts(0), "1970-01-01T00:00:00Z");
        assert_eq!(format_unix_ts(1_700_000_000), "2023-11-14T22:13:20Z");
        // 闰年前边界
        assert_eq!(format_unix_ts(1_709_769_600), "2024-03-07T00:00:00Z");
    }

    #[test]
    fn chunk_layout_and_missing() {
        let mut meta = UploadMeta {
            upload_id: "00000000-0000-0000-0000-000000000000".into(),
            sha256: "a".repeat(64),
            total_size: 30,
            chunk_sizes: BTreeMap::from([(0, 10), (1, 10), (2, 10)]),
            chunks: BTreeMap::new(),
            created_at: "t".into(),
            committed: false,
        };
        assert_eq!(meta.chunk_count(), Some(3));
        assert_eq!(meta.missing_chunks(), vec![0, 1, 2]);
        meta.chunks.insert(
            1,
            ChunkInfo {
                size: 10,
                sha256: "b".repeat(64),
                received_at: "t".into(),
            },
        );
        assert_eq!(meta.missing_chunks(), vec![0, 2]);

        // 尺寸和不等于总大小 -> 非法布局
        meta.total_size = 31;
        assert_eq!(meta.chunk_count(), None);
    }

    #[test]
    fn object_id_validation() {
        assert!(is_object_id(&"a".repeat(64)));
        assert!(!is_object_id(&"a".repeat(63)));
        assert!(!is_object_id(&format!("{}g", "a".repeat(63))));
        assert!(!is_object_id("../etc/passwd"));
    }
}
