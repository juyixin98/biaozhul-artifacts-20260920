//! 磁盘存储层：内容寻址存储 (CAS) 与动作缓存 (AC)。
//!
//! 目录布局：
//!
//! ```text
//! <root>/
//!   cas/aa/bb/<full-hash>   # 不可变内容块；内容缺失或损坏等价于“不存在”
//!   tmp/                    # 上传临时文件，rename 落盘（同文件系统原子替换）
//!   ac/<full-hash>.json     # 动作结果（规范 JSON），发布即不可变
//!   ac/tmp/                 # 动作结果临时文件
//! ```
//!
//! 关键不变量：
//! 1. CAS 块不可变：键即内容摘要。上传时全量哈希 + 长度校验，落盘用
//!    `rename` 原子发布，同键并发上传串行化（按哈希加锁），已存在则复核。
//! 2. 动作结果发布前，必须确认其引用的 **每个** CAS 摘要都存在且哈希一致，
//!    否则返回 [`StoreError::MissingBlobs`] / [`Corrupted`]，绝不写入 AC。
//! 3. 读取动作结果时同样逐个复核引用对象，任何损坏都返回 [`Corrupted`]，
//!    绝不返回看似成功的缓存命中。
//!
//! [`Corrupted`]: StoreError::Corrupted

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sha2::{Digest as _, Sha256};
use tokio::fs;
use tokio::io::AsyncReadExt;
use tokio::sync::Mutex;

use crate::hash::is_valid_hash;
use crate::models::{canonical_json, Action, ActionResult, Digest};

#[derive(Debug)]
pub enum StoreError {
    /// 摘要格式非法。
    InvalidDigest(String),
    /// 上传内容与声明摘要不符（哈希或长度）。
    HashMismatch {
        claimed: String,
        actual: String,
    },
    /// 动作字节的摘要与 URL 中的动作键不符。
    ActionKeyMismatch {
        url_key: String,
        actual: String,
    },
    /// 清单引用的对象在 CAS 中不存在（含发布前缺块）。
    MissingBlobs(Vec<Digest>),
    /// CAS 中存在对象但内容损坏（长度或哈希不符）。
    Corrupted {
        hash: String,
        detail: String,
    },
    /// 动作缓存中已有不可变结果，且与提交内容不同。
    ActionAlreadyExists(String),
    /// IO 错误（被视为服务端故障，不对外伪装成缓存语义）。
    Io(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::InvalidDigest(h) => write!(f, "invalid digest: {h}"),
            StoreError::HashMismatch { claimed, actual } => write!(
                f,
                "content hash mismatch: claimed {claimed}, actual {actual}"
            ),
            StoreError::ActionKeyMismatch { url_key, actual } => write!(
                f,
                "action digest mismatch: url key {url_key}, actual {actual}"
            ),
            StoreError::MissingBlobs(ds) => write!(
                f,
                "{} referenced blob(s) missing from CAS: {}",
                ds.len(),
                ds.iter()
                    .map(|d| format!("{}/{}", d.hash, d.size_bytes))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            StoreError::Corrupted { hash, detail } => {
                write!(f, "corrupted blob {hash}: {detail}")
            }
            StoreError::ActionAlreadyExists(h) => {
                write!(f, "immutable action result already exists: {h}")
            }
            StoreError::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e.to_string())
    }
}

/// 本地磁盘缓存。所有状态可通过 [`AppState`](crate::api::AppState) 共享。
pub struct LocalStore {
    root: PathBuf,
    cas_dir: PathBuf,
    tmp_dir: PathBuf,
    ac_dir: PathBuf,
    ac_tmp_dir: PathBuf,
    /// 按 blob 哈希串行化同键并发上传；不同哈希互不阻塞。
    blob_locks: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    /// 串行化同一动作键的并发发布。
    action_locks: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
}

impl LocalStore {
    /// 打开（必要时创建）根目录下的缓存。
    pub async fn open(root: impl AsRef<Path>) -> Result<Self, StoreError> {
        let root = root.as_ref().to_path_buf();
        let cas_dir = root.join("cas");
        let tmp_dir = root.join("tmp");
        let ac_dir = root.join("ac");
        let ac_tmp_dir = ac_dir.join("tmp");
        for d in [&cas_dir, &tmp_dir, &ac_dir, &ac_tmp_dir] {
            fs::create_dir_all(d).await?;
        }
        Ok(Self {
            root,
            cas_dir,
            tmp_dir,
            ac_dir,
            ac_tmp_dir,
            blob_locks: Mutex::new(HashMap::new()),
            action_locks: Mutex::new(HashMap::new()),
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    async fn keyed_lock(
        map: &Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
        key: &str,
    ) -> tokio::sync::OwnedMutexGuard<()> {
        let mut table = map.lock().await;
        let mutex = table
            .entry(key.to_string())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        drop(table);
        mutex.lock_owned().await
    }

    fn cas_path(&self, hash: &str) -> PathBuf {
        // aa/bb/<64hex> 两级分桶，避免单目录过大
        self.cas_dir.join(&hash[0..2]).join(&hash[2..4]).join(hash)
    }

    // ---------------- CAS ----------------

    /// 上传（或确认已存在）一个不可变内容块。
    ///
    /// * 全量计算 SHA-256，并与 `expected.hash`、`expected.size_bytes` 比对；
    /// * 先写临时文件再原子 rename；
    /// * 同键并发由每哈希锁串行化；已有对象会重新读盘复核，损坏则覆盖修复。
    pub async fn put_blob(&self, expected: &Digest, data: &[u8]) -> Result<(), StoreError> {
        validate(expected)?;
        let actual = crate::hash::digest_bytes(data);
        if actual != expected.hash || data.len() as u64 != expected.size_bytes {
            return Err(StoreError::HashMismatch {
                claimed: expected.hash.clone(),
                actual,
            });
        }

        let _guard = Self::keyed_lock(&self.blob_locks, &expected.hash).await;
        let dest = self.cas_path(&expected.hash);

        // 快路径：已存在则逐字节复核。
        if dest.exists() {
            match self.verify_blob(expected).await {
                Ok(_) => return Ok(()),
                // 内容损坏：上传数据已验证哈希等于键，用它原子替换坏文件以自愈。
                Err(StoreError::Corrupted { .. }) => {}
                Err(e) => return Err(e),
            }
        }

        fs::create_dir_all(dest.parent().unwrap()).await?;
        let tmp = self.tmp_dir.join(format!(
            ".put-{}-{}-{}",
            expected.hash,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        fs::write(&tmp, data).await?;
        // POSIX rename 在同文件系统上是原子操作，且会替换既有（损坏）文件。
        match fs::rename(&tmp, &dest).await {
            Ok(()) => Ok(()),
            Err(e) => {
                let _ = fs::remove_file(&tmp).await;
                Err(e.into())
            }
        }
    }

    /// 读取一个块，并在返回前完成长度 + 全量哈希校验。
    pub async fn get_blob(&self, digest: &Digest) -> Result<Vec<u8>, StoreError> {
        validate(digest)?;
        self.verify_blob(digest).await
    }

    /// 仅凭哈希读取块（长度取自磁盘元数据），仍做全量哈希校验。
    /// 供下载方未携带 `size_bytes` 时使用。
    pub async fn get_blob_by_hash(&self, hash: &str) -> Result<Vec<u8>, StoreError> {
        if !is_valid_hash(hash) {
            return Err(StoreError::InvalidDigest(hash.to_string()));
        }
        let path = self.cas_path(hash);
        let metadata = match fs::metadata(&path).await {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(StoreError::MissingBlobs(vec![Digest {
                    hash: hash.to_string(),
                    size_bytes: 0,
                }]));
            }
            Err(e) => return Err(e.into()),
        };
        let digest = Digest {
            hash: hash.to_string(),
            size_bytes: metadata.len(),
        };
        self.verify_blob(&digest).await
    }

    /// 仅凭哈希判断块是否存在且完整（长度取元数据，全量哈希校验）。
    pub async fn blob_exists_by_hash(&self, hash: &str) -> Result<bool, StoreError> {
        match self.get_blob_by_hash(hash).await {
            Ok(_) => Ok(true),
            Err(StoreError::MissingBlobs(_)) | Err(StoreError::Corrupted { .. }) => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// 判断块是否存在 **且完整**。损坏视为不存在（同时把损坏情况上报给调用方）。
    pub async fn blob_exists(&self, digest: &Digest) -> Result<bool, StoreError> {
        match self.verify_blob(digest).await {
            Ok(_) => Ok(true),
            Err(StoreError::MissingBlobs(_)) | Err(StoreError::Corrupted { .. }) => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// 批量返回 CAS 中缺失（或损坏）的摘要，保持请求顺序、去重。
    pub async fn find_missing(&self, digests: &[Digest]) -> Result<Vec<Digest>, StoreError> {
        let mut seen = BTreeSet::new();
        let mut missing = Vec::new();
        for d in digests {
            validate(d)?;
            if !seen.insert(d.hash.clone()) {
                continue;
            }
            if !self.blob_exists(d).await? {
                missing.push(d.clone());
            }
        }
        Ok(missing)
    }

    /// 从磁盘读取块并做全量校验（长度 + SHA-256）。
    async fn verify_blob(&self, digest: &Digest) -> Result<Vec<u8>, StoreError> {
        let path = self.cas_path(&digest.hash);
        let mut file = match fs::File::open(&path).await {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(StoreError::MissingBlobs(vec![digest.clone()]));
            }
            Err(e) => return Err(e.into()),
        };
        let metadata = file.metadata().await?;
        if metadata.len() != digest.size_bytes {
            return Err(StoreError::Corrupted {
                hash: digest.hash.clone(),
                detail: format!(
                    "size mismatch: declared {}, on-disk {}",
                    digest.size_bytes,
                    metadata.len()
                ),
            });
        }
        // 分块流式哈希，避免一次性把大文件读入内存。
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 64 * 1024];
        let mut all = Vec::with_capacity(digest.size_bytes as usize);
        loop {
            let n = file.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
            all.extend_from_slice(&buf[..n]);
        }
        let actual = hex::encode(hasher.finalize());
        if actual != digest.hash {
            return Err(StoreError::Corrupted {
                hash: digest.hash.clone(),
                detail: format!("sha256 mismatch: recomputed {actual}"),
            });
        }
        if all.len() as u64 != digest.size_bytes {
            return Err(StoreError::Corrupted {
                hash: digest.hash.clone(),
                detail: format!(
                    "size changed during read: declared {}, read {}",
                    digest.size_bytes,
                    all.len()
                ),
            });
        }
        Ok(all)
    }

    // ---------------- Action Cache ----------------

    /// 发布动作结果（不可变）。
    ///
    /// 步骤：
    /// 1. 校验 `action` 的规范摘要等于 `action_key`，并把动作字节写入 CAS；
    /// 2. 逐个核验 `result` 引用的全部 CAS 对象（存在且哈希一致）；
    /// 3. 全部通过后原子写入 AC；任一对象缺失/损坏都不产生 AC 记录。
    pub async fn put_action_result(
        &self,
        action_key: &str,
        action: &Action,
        mut result: ActionResult,
    ) -> Result<ActionResult, StoreError> {
        if !is_valid_hash(action_key) {
            return Err(StoreError::InvalidDigest(action_key.to_string()));
        }
        let action_bytes =
            canonical_json(action).map_err(|e| StoreError::Io(e.to_string()))?;
        let actual_key = crate::hash::digest_bytes(&action_bytes);
        if actual_key != action_key {
            return Err(StoreError::ActionKeyMismatch {
                url_key: action_key.to_string(),
                actual: actual_key,
            });
        }

        let _guard = Self::keyed_lock(&self.action_locks, action_key).await;

        // 不可变：已存在则只允许幂等重放（内容完全一致），并同样复核引用。
        let ac_path = self.ac_path(action_key);
        if ac_path.exists() {
            let existing = self.get_action_result(action_key).await?;
            let existing_bytes = canonical_json(&strip_published(&existing))
                .map_err(|e| StoreError::Io(e.to_string()))?;
            let incoming_bytes = canonical_json(&result)
                .map_err(|e| StoreError::Io(e.to_string()))?;
            if existing_bytes != incoming_bytes {
                return Err(StoreError::ActionAlreadyExists(action_key.to_string()));
            }
            return Ok(existing);
        }

        // 动作对象本身入 CAS（已算出摘要；复用校验路径）。
        let action_digest = Digest {
            hash: actual_key.clone(),
            size_bytes: action_bytes.len() as u64,
        };
        self.put_blob(&action_digest, &action_bytes).await?;

        // 核心闸门：全部引用对象核验通过才允许发布。
        self.ensure_all_present(&result).await?;

        result.published_at_ms = Some(now_ms());
        let payload =
            canonical_json(&result).map_err(|e| StoreError::Io(e.to_string()))?;
        let tmp = self
            .ac_tmp_dir
            .join(format!(".ac-{}-{}", action_key, std::process::id()));
        fs::write(&tmp, &payload).await?;
        match fs::rename(&tmp, &ac_path).await {
            Ok(()) => Ok(result),
            Err(e) => {
                let _ = fs::remove_file(&tmp).await;
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    // 并发对端先发布：复核并做幂等判断。
                    let existing = self.get_action_result(action_key).await?;
                    if canonical_json(&strip_published(&existing)).unwrap_or_default()
                        == canonical_json(&strip_published(&result)).unwrap_or_default()
                    {
                        Ok(existing)
                    } else {
                        Err(StoreError::ActionAlreadyExists(action_key.to_string()))
                    }
                } else {
                    Err(e.into())
                }
            }
        }
    }

    /// 读取动作结果。返回前对清单引用的 **每个** 对象重新做全量校验；
    /// 任一对象缺失或损坏都返回错误，绝不返回伪命中。
    pub async fn get_action_result(&self, action_key: &str) -> Result<ActionResult, StoreError> {
        if !is_valid_hash(action_key) {
            return Err(StoreError::InvalidDigest(action_key.to_string()));
        }
        let path = self.ac_path(action_key);
        let bytes = match fs::read(&path).await {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(StoreError::MissingBlobs(vec![Digest {
                    hash: action_key.to_string(),
                    size_bytes: 0,
                }]));
            }
            Err(e) => return Err(e.into()),
        };
        let result: ActionResult = serde_json::from_slice(&bytes)
            .map_err(|e| StoreError::Corrupted {
                hash: action_key.to_string(),
                detail: format!("action result json parse failed: {e}"),
            })?;
        // 命中即验证：已发布清单引用的任何对象缺失或损坏都属于缓存损坏，
        // 一律按失败（503）处理，绝不返回伪命中。
        self.verify_all_present(&result).await?;
        Ok(result)
    }

    fn ac_path(&self, action_key: &str) -> PathBuf {
        self.ac_dir.join(format!("{action_key}.json"))
    }

    /// 核验清单引用的所有对象：存在性 + 长度 + 全量哈希。
    /// 发布路径使用：引用对象缺失 → [`StoreError::MissingBlobs`]（424，客户端可补传）。
    async fn ensure_all_present(&self, result: &ActionResult) -> Result<(), StoreError> {
        let mut missing = Vec::new();
        for d in result.referenced_digests() {
            match self.verify_blob(d).await {
                Ok(_) => {}
                // 发布时缺块：收集后统一报错，绝不发布。
                Err(StoreError::MissingBlobs(_)) => missing.push(d.clone()),
                // 损坏等其它错误直接上抛。
                Err(e) => return Err(e),
            }
        }
        if missing.is_empty() {
            Ok(())
        } else {
            Err(StoreError::MissingBlobs(missing))
        }
    }

    /// 命中路径使用：已发布清单引用的对象缺失（悬空引用）同样视为缓存损坏。
    async fn verify_all_present(&self, result: &ActionResult) -> Result<(), StoreError> {
        for d in result.referenced_digests() {
            match self.verify_blob(d).await {
                Ok(_) => {}
                Err(StoreError::MissingBlobs(_)) => {
                    return Err(StoreError::Corrupted {
                        hash: d.hash.clone(),
                        detail: "referenced blob missing from a published action result"
                            .to_string(),
                    });
                }
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}

fn validate(d: &Digest) -> Result<(), StoreError> {
    if !is_valid_hash(&d.hash) {
        return Err(StoreError::InvalidDigest(d.hash.clone()));
    }
    Ok(())
}

fn strip_published(r: &ActionResult) -> ActionResult {
    let mut r = r.clone();
    r.published_at_ms = None;
    r
}

fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
