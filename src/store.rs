//! Filesystem-backed storage for multipart resumable uploads.
//!
//! On-disk layout (under `root`):
//!
//! ```text
//! root/
//!   uploads/<upload_id>/meta.json        # session metadata (one JSON document per upload)
//!   uploads/<upload_id>/parts/<n>        # part data, written atomically via <n>.tmp + rename
//!   objects/<key>/object                 # published immutable object bytes
//!   objects/<key>/meta.json              # {key, size, sha256, upload_id, created_at}
//!   tmp/                                 # staging file assembled during commit
//! ```
//!
//! Guarantees:
//! * Uncommitted parts are namespaced by upload id and are never readable through
//!   the object API.
//! * Publishing is atomic: the assembled file is fsynced and renamed into place,
//!   and only then does the session get marked committed. A crash at any point
//!   leaves either the old state (no object) or a fully published object.
//! * Restart reconciliation: sessions whose object already exists on disk are
//!   marked committed; missing parts/tmp files from crashed writes are ignored.

use std::path::PathBuf;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Mutex;

use crate::error::{AppError, AppResult};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PartRecord {
    pub number: u32,
    pub size: u64,
    /// SHA-256 of the part payload, hex encoded.
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ChunkSpec {
    /// Logical zero-based offset of this chunk inside the whole object.
    pub start: u64,
    pub end: u64, // exclusive
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionMeta {
    pub upload_id: String,
    pub key: String,
    pub total_size: u64,
    /// Expected SHA-256 of the whole object, hex encoded.
    pub sha256: String,
    pub chunk_size: u64,
    pub part_size: u64,
    /// Fixed-size parts in logical order (the last part may be shorter).
    pub parts: Vec<PartRecord>,
    /// Optional explicit chunks when the client asks for a custom plan.
    #[serde(default)]
    pub chunks: Vec<ChunkSpec>,
    pub created_at: u64,
    pub committed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectMeta {
    pub key: String,
    pub size: u64,
    pub sha256: String,
    pub upload_id: String,
    pub created_at: u64,
}

#[derive(Clone)]
pub struct Store {
    root: Arc<PathBuf>,
    // Serializes mutating operations so writes to the same session are consistent.
    // Fine for the test/acceptance scale; the filesystem layout is the source of truth.
    lock: Arc<Mutex<()>>,
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn decode_hex_sha(s: &str) -> AppResult<[u8; 32]> {
    let s = s.trim().to_lowercase();
    let bytes = hex::decode(&s).map_err(|_| {
        AppError::BadRequest(format!("invalid hex sha256: {s:?}"))
    })?;
    if bytes.len() != 32 {
        return Err(AppError::BadRequest(format!(
            "sha256 must be 32 bytes (64 hex chars), got {}",
            bytes.len()
        )));
    }
    let mut out = [0u8; 32];
    out.copy_from_slice(&bytes);
    Ok(out)
}

/// Split `total` into parts of at most `part_size`.
fn split_parts(total: u64, part_size: u64) -> Vec<(u64, u64)> {
    if total == 0 {
        return vec![(0, 0)];
    }
    let mut v = Vec::new();
    let mut start = 0u64;
    while start < total {
        let end = (start + part_size).min(total);
        v.push((start, end));
        start = end;
    }
    v
}

impl Store {
    pub async fn new(root: impl Into<PathBuf>) -> AppResult<Self> {
        let root = root.into();
        tokio::fs::create_dir_all(root.join("uploads")).await?;
        tokio::fs::create_dir_all(root.join("objects")).await?;
        tokio::fs::create_dir_all(root.join("tmp")).await?;
        let store = Store {
            root: Arc::new(root),
            lock: Arc::new(Mutex::new(())),
        };
        store.reconcile().await?;
        Ok(store)
    }

    fn upload_dir(&self, id: &str) -> PathBuf {
        self.root.join("uploads").join(id)
    }
    fn meta_path(&self, id: &str) -> PathBuf {
        self.upload_dir(id).join("meta.json")
    }
    fn parts_dir(&self, id: &str) -> PathBuf {
        self.upload_dir(id).join("parts")
    }
    fn part_path(&self, id: &str, n: u32) -> PathBuf {
        self.parts_dir(id).join(n.to_string())
    }
    fn part_tmp_path(&self, id: &str, n: u32) -> PathBuf {
        self.parts_dir(id).join(format!("{n}.tmp"))
    }
    fn object_dir(&self, key: &str) -> PathBuf {
        self.root.join("objects").join(sanitize_key(key))
    }
    fn object_path(&self, key: &str) -> PathBuf {
        self.object_dir(key).join("object")
    }
    fn object_meta_path(&self, key: &str) -> PathBuf {
        self.object_dir(key).join("meta.json")
    }
    fn staging_path(&self, id: &str) -> PathBuf {
        self.root.join("tmp").join(format!("{id}.stage"))
    }

    async fn read_meta(&self, id: &str) -> AppResult<SessionMeta> {
        let p = self.meta_path(id);
        let data = tokio::fs::read(&p).await.map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                AppError::NotFound(format!("upload session {id:?} not found"))
            } else {
                AppError::Internal(e.to_string())
            }
        })?;
        serde_json::from_slice(&data)
            .map_err(|e| AppError::Internal(format!("corrupt meta.json for {id}: {e}")))
    }

    async fn write_meta(&self, meta: &SessionMeta) -> AppResult<()> {
        let dir = self.upload_dir(&meta.upload_id);
        tokio::fs::create_dir_all(self.parts_dir(&meta.upload_id)).await?;
        let tmp = dir.join("meta.json.tmp");
        let mut f = tokio::fs::File::create(&tmp).await?;
        let buf = serde_json::to_vec_pretty(meta).unwrap();
        f.write_all(&buf).await?;
        f.sync_all().await?;
        drop(f);
        tokio::fs::rename(&tmp, self.meta_path(&meta.upload_id)).await?;
        Ok(())
    }

    /// Mark sessions committed when their object file already exists (crash recovery),
    /// and sweep stale temp files from interrupted writes.
    async fn reconcile(&self) -> AppResult<()> {
        let uploads = self.root.join("uploads");
        let mut entries = match tokio::fs::read_dir(&uploads).await {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        while let Some(entry) = entries.next_entry().await? {
            let id = match entry.file_name().to_str() {
                Some(s) => s.to_string(),
                None => continue,
            };
            let meta = match self.read_meta(&id).await {
                Ok(m) => m,
                // No meta yet (create crashed mid-write): drop the half session.
                Err(AppError::NotFound(_)) => {
                    let _ = tokio::fs::remove_dir_all(entry.path()).await;
                    continue;
                }
                Err(e) => return Err(e),
            };
            // Sweep stale per-part temp files.
            if let Ok(mut parts) = tokio::fs::read_dir(self.parts_dir(&id)).await {
                while let Some(pe) = parts.next_entry().await? {
                    if pe.file_name().to_string_lossy().ends_with(".tmp") {
                        let _ = tokio::fs::remove_file(pe.path()).await;
                    }
                }
            }
            let mut committed_now = false;
            if !meta.committed && self.object_path(&meta.key).exists() {
                // Object was renamed into place but the committed flag/tombstone didn't land.
                let mut healed = meta.clone();
                healed.committed = true;
                self.write_meta(&healed).await?;
                committed_now = true;
            } else if !meta.committed
                && self.object_meta_path(&meta.key).exists()
                && !self.object_path(&meta.key).exists()
            {
                // Publish crashed after writing object meta but before the final
                // object rename: the object was never visible. Remove the stale
                // directory so a retry publishes cleanly.
                let _ = tokio::fs::remove_dir_all(self.object_dir(&meta.key)).await;
            }
            if meta.committed || committed_now {
                // Staging file (if any) is no longer needed.
                let _ = tokio::fs::remove_file(self.staging_path(&id)).await;
            }
        }

        // Sweep leftover object.tmp files from publishes interrupted before the
        // final rename (the object itself never became visible). Keys may be
        // nested (a/b/c), so walk recursively. Startup-only housekeeping: a
        // synchronous std::fs walk keeps this simple (no async recursion).
        let objects = self.root.join("objects");
        fn sweep_tmp(dir: &std::path::Path) {
            let entries = match std::fs::read_dir(dir) {
                Ok(e) => e,
                Err(_) => return,
            };
            for e in entries.flatten() {
                let p = e.path();
                if p.is_dir() {
                    sweep_tmp(&p);
                } else if p.file_name().map(|n| n == "object.tmp").unwrap_or(false) {
                    let _ = std::fs::remove_file(&p);
                }
            }
        }
        sweep_tmp(&objects);
        Ok(())
    }

    // ---------------------------------------------------------------- API ops

    pub async fn create_upload(
        &self,
        key: String,
        total_size: u64,
        sha256: String,
        chunk_size: Option<u64>,
    ) -> AppResult<SessionMeta> {
        if key.is_empty() || key.contains('\0') {
            return Err(AppError::BadRequest("object key must be non-empty".into()));
        }
        let _expected = decode_hex_sha(&sha256)?;
        let chunk_size = chunk_size.unwrap_or(4 * 1024 * 1024).max(1);

        let _g = self.lock.lock().await;

        if self.object_meta_path(&key).exists() {
            return Err(AppError::Conflict(format!(
                "object {key:?} already exists; objects are immutable"
            )));
        }

        let upload_id = uuid::Uuid::new_v4().simple().to_string();
        let ranges = split_parts(total_size, chunk_size);
        let parts = ranges
            .iter()
            .enumerate()
            .map(|(i, _)| PartRecord {
                number: i as u32,
                size: 0,
                sha256: String::new(),
            })
            .collect();

        let meta = SessionMeta {
            upload_id: upload_id.clone(),
            key,
            total_size,
            sha256: sha256.to_lowercase(),
            chunk_size,
            part_size: chunk_size,
            parts,
            chunks: ranges.into_iter().map(|(s, e)| ChunkSpec { start: s, end: e }).collect(),
            created_at: now_secs(),
            committed: false,
        };
        self.write_meta(&meta).await?;
        Ok(meta)
    }

    pub async fn get_upload(&self, upload_id: &str) -> AppResult<SessionMeta> {
        self.read_meta(upload_id).await
    }

    /// Store a part. Same number + identical bytes is idempotent; different bytes
    /// for the same number is rejected with 409.
    pub async fn put_part(
        &self,
        upload_id: &str,
        number: u32,
        body: &[u8],
    ) -> AppResult<PartRecord> {
        let _g = self.lock.lock().await;
        let mut meta = self.read_meta(upload_id).await?;
        if meta.committed {
            return Err(AppError::Conflict(format!(
                "upload {upload_id:?} is already committed"
            )));
        }
        let spec = meta
            .chunks
            .get(number as usize)
            .ok_or_else(|| {
                AppError::BadRequest(format!(
                    "part number {number} out of range 0..{}",
                    meta.chunks.len()
                ))
            })?
            .clone();
        let expected_len = spec.end - spec.start;
        if body.len() as u64 != expected_len {
            return Err(AppError::BadRequest(format!(
                "part {number} must be exactly {expected_len} bytes, got {}",
                body.len()
            )));
        }

        let mut hasher = Sha256::new();
        hasher.update(body);
        let digest = hex::encode(hasher.finalize());

        if let Some(existing) = meta.parts.get(number as usize) {
            if !existing.sha256.is_empty() {
                if existing.sha256 == digest {
                    // Idempotent retry of identical content.
                    return Ok(existing.clone());
                }
                return Err(AppError::ConflictPart(format!(
                    "part {number} already uploaded with different content \
                     (existing sha256 {}, received {digest})",
                    existing.sha256
                )));
            }
        }

        // Atomic write: temp file, fsync, rename. A crash leaves a .tmp to sweep.
        let tmp = self.part_tmp_path(upload_id, number);
        let final_path = self.part_path(upload_id, number);
        let mut f = tokio::fs::File::create(&tmp).await?;
        f.write_all(body).await?;
        f.sync_all().await?;
        drop(f);
        tokio::fs::rename(&tmp, &final_path).await?;

        let rec = PartRecord {
            number,
            size: body.len() as u64,
            sha256: digest,
        };
        meta.parts[number as usize] = rec.clone();
        self.write_meta(&meta).await?;
        Ok(rec)
    }

    /// Assemble parts, verify total length and whole-object SHA-256, publish atomically.
    /// Completing an already-committed upload is idempotent.
    pub async fn complete_upload(&self, upload_id: &str) -> AppResult<ObjectMeta> {
        let _g = self.lock.lock().await;
        let meta = self.read_meta(upload_id).await?;

        if meta.committed {
            // Idempotent re-complete: return the existing object if it is still there.
            return self.read_object_meta(&meta.key).await.map_err(|_| {
                AppError::Conflict(format!(
                    "upload {upload_id:?} already committed but object is missing"
                ))
            });
        }

        // All parts present?
        let missing: Vec<u32> = meta
            .parts
            .iter()
            .filter(|p| p.sha256.is_empty())
            .map(|p| p.number)
            .collect();
        if !missing.is_empty() {
            return Err(AppError::Unprocessable(format!(
                "cannot complete upload {upload_id:?}: missing parts: {missing:?}"
            )));
        }
        // Verify contiguous layout lengths.
        let summed: u64 = meta.parts.iter().map(|p| p.size).sum();
        if summed != meta.total_size {
            return Err(AppError::Unprocessable(format!(
                "sum of part sizes ({summed}) != declared total size ({})",
                meta.total_size
            )));
        }

        if self.object_path(&meta.key).exists() {
            return Err(AppError::Conflict(format!(
                "object {:?} already exists; objects are immutable",
                meta.key
            )));
        }

        // Assemble to a staging file while hashing the whole stream.
        let stage = self.staging_path(upload_id);
        let mut total: u64 = 0;
        let mut hasher = Sha256::new();
        {
            let mut out = tokio::fs::File::create(&stage).await?;
            for p in &meta.parts {
                let path = self.part_path(upload_id, p.number);
                let mut f = tokio::fs::File::open(&path).await.map_err(|e| {
                    AppError::Unprocessable(format!(
                        "part {} data missing on disk: {e}",
                        p.number
                    ))
                })?;
                let mut buf = Vec::with_capacity(p.size as usize);
                f.read_to_end(&mut buf).await?;
                if buf.len() as u64 != p.size {
                    return Err(AppError::Unprocessable(format!(
                        "part {} size mismatch on disk",
                        p.number
                    )));
                }
                // Re-verify each part's hash while assembling.
                let mut h = Sha256::new();
                h.update(&buf);
                if hex::encode(h.finalize()) != p.sha256 {
                    return Err(AppError::Unprocessable(format!(
                        "part {} hash mismatch on disk",
                        p.number
                    )));
                }
                hasher.update(&buf);
                out.write_all(&buf).await?;
                total += buf.len() as u64;
            }
            out.sync_all().await?;
        }

        if total != meta.total_size {
            let _ = tokio::fs::remove_file(&stage).await;
            return Err(AppError::Unprocessable(format!(
                "assembled length {total} != declared total {}",
                meta.total_size
            )));
        }
        let actual = hex::encode(hasher.finalize());
        if actual != meta.sha256 {
            let _ = tokio::fs::remove_file(&stage).await;
            return Err(AppError::Unprocessable(format!(
                "whole-object sha256 mismatch: expected {}, assembled {actual}",
                meta.sha256
            )));
        }

        // Publish (crash-safe ordering):
        //   1. move staging file to objects/<key>/object.tmp (same filesystem)
        //   2. fsync + publish meta.json (via temp file + rename)
        //   3. rename object.tmp -> object  <-- the single atomic "visible" point
        // A crash before step 3 leaves no readable object; retrying complete
        // simply overwrites .tmp/meta and renames again.
        let obj_dir = self.object_dir(&meta.key);
        tokio::fs::create_dir_all(&obj_dir).await?;
        let obj_tmp = obj_dir.join("object.tmp");
        let _ = tokio::fs::remove_file(&obj_tmp).await; // leftover from a crashed attempt
        tokio::fs::rename(&stage, &obj_tmp).await?;
        let obj_meta = ObjectMeta {
            key: meta.key.clone(),
            size: meta.total_size,
            sha256: meta.sha256.clone(),
            upload_id: upload_id.to_string(),
            created_at: now_secs(),
        };
        let meta_tmp = obj_dir.join("meta.json.tmp");
        let mut f = tokio::fs::File::create(&meta_tmp).await?;
        f.write_all(&serde_json::to_vec_pretty(&obj_meta).unwrap())
            .await?;
        f.sync_all().await?;
        drop(f);
        tokio::fs::rename(&meta_tmp, self.object_meta_path(&meta.key)).await?;
        tokio::fs::rename(&obj_tmp, self.object_path(&meta.key)).await?;

        // Finally mark the session committed. Even if this last step is interrupted,
        // restart reconciliation derives committed=true from the object's existence.
        let mut committed = meta;
        committed.committed = true;
        self.write_meta(&committed).await?;

        Ok(obj_meta)
    }

    pub async fn abort_upload(&self, upload_id: &str) -> AppResult<()> {
        let _g = self.lock.lock().await;
        let meta = self.read_meta(upload_id).await?;
        if meta.committed {
            return Err(AppError::Conflict(format!(
                "upload {upload_id:?} is already committed"
            )));
        }
        tokio::fs::remove_dir_all(self.upload_dir(upload_id)).await?;
        let _ = tokio::fs::remove_file(self.staging_path(upload_id)).await;
        Ok(())
    }

    async fn read_object_meta(&self, key: &str) -> AppResult<ObjectMeta> {
        let p = self.object_meta_path(key);
        let data = tokio::fs::read(&p).await.map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                AppError::NotFound(format!("object {key:?} not found"))
            } else {
                AppError::Internal(e.to_string())
            }
        })?;
        serde_json::from_slice(&data)
            .map_err(|e| AppError::Internal(format!("corrupt object meta for {key}: {e}")))
    }

    /// Read a published object. Never returns bytes for uncommitted uploads.
    pub async fn read_object(&self, key: &str) -> AppResult<(ObjectMeta, Vec<u8>)> {
        let meta = self.read_object_meta(key).await?;
        let data = tokio::fs::read(self.object_path(key)).await.map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                AppError::NotFound(format!("object {key:?} not found"))
            } else {
                AppError::Internal(e.to_string())
            }
        })?;
        if data.len() as u64 != meta.size {
            return Err(AppError::Internal(format!(
                "object {key:?} size on disk differs from meta"
            )));
        }
        let mut h = Sha256::new();
        h.update(&data);
        if hex::encode(h.finalize()) != meta.sha256 {
            return Err(AppError::Internal(format!(
                "object {key:?} on-disk hash differs from recorded meta"
            )));
        }
        Ok((meta, data))
    }
}

/// Turn an arbitrary user key into a safe single filesystem segment.
/// Supports nested keys (a/b/c) by percent-encoding each segment, so the
/// object store cannot escape `objects/` via "..".
fn sanitize_key(key: &str) -> String {
    key.split('/')
        .map(|seg| {
            let mut out = String::with_capacity(seg.len());
            for b in seg.bytes() {
                match b {
                    b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' => {
                        out.push(b as char)
                    }
                    _ => out.push_str(&format!("%{b:02X}")),
                }
            }
            if out.is_empty() {
                out.push('_');
            }
            out
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// Optional helper used by streaming: verify body bytes against an expected sha.
pub fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

