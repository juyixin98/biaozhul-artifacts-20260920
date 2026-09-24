//! 内存存储与状态机核心。
//!
//! 设计要点：
//! * 内容寻址存储（CAS）：blob 以摘要为键；同摘要重复注册只增加引用计数与
//!   `build_count` 引用计数语义（见 [`Store::register``]），**不重新写入内容**。
//! * 回退/晋级只改 `stage` 指针并追加事件，绝不触碰 blob，即"不重新构建产物"。
//! * 全部变更在一个 `Mutex` 临界区内完成，并发请求被串行裁决：
//!   重复晋级、并发回退只有一个请求能成功，其余得到 409。

use std::collections::HashMap;

use crate::digest::{normalize_digest, sha256_digest};
use crate::error::{AppError, AppResult};
use crate::models::{
    ArtifactView, BlobInfo, Event, HistoryView, PromoteRequest, Proof, ProofRequest,
    RollbackRequest, Stage,
};

/// 当前 Unix 毫秒时间。
fn now_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// 制品记录。
pub struct ArtifactRecord {
    pub id: String,
    /// 注册即绑定，终身不变的摘要。
    pub digest: String,
    pub content_len: usize,
    pub stage: Stage,
    pub proofs: Vec<Proof>,
    /// 该身份对应内容被写入 CAS 的次数（同摘要内容第二次注册不同制品时可观测）。
    pub build_count: u32,
    pub created_at_ms: u128,
    pub history: Vec<Event>,
}

/// 内容寻址存储槽。
struct BlobSlot {
    content: Vec<u8>,
    /// 被多少制品引用。
    references: usize,
    /// 实际写入次数（同内容重复 put 不会增加；仅首次写入）。
    writes: u32,
}

/// 发布所需的必要证明类型（可配置）。
#[derive(Debug, Clone)]
pub struct Config {
    pub required_proof_kinds: Vec<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            required_proof_kinds: vec![
                "unit-test".to_string(),
                "integration-test".to_string(),
                "security-scan".to_string(),
            ],
        }
    }
}

#[derive(Default)]
pub struct Store {
    artifacts: HashMap<String, ArtifactRecord>,
    blobs: HashMap<String, BlobSlot>,
    config: Config,
}

impl Store {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_config(config: Config) -> Self {
        Self {
            config,
            ..Self::default()
        }
    }

    // ---------- 读取 ----------

    fn get(&self, id: &str) -> AppResult<&ArtifactRecord> {
        self.artifacts
            .get(id)
            .ok_or_else(|| AppError::not_found(format!("artifact {id} not found")))
    }

    fn get_mut(&mut self, id: &str) -> AppResult<&mut ArtifactRecord> {
        self.artifacts
            .get_mut(id)
            .ok_or_else(|| AppError::not_found(format!("artifact {id} not found")))
    }

    pub fn view(&self, id: &str) -> AppResult<ArtifactView> {
        let a = self.get(id)?;
        Ok(ArtifactView {
            id: a.id.clone(),
            digest: a.digest.clone(),
            content_len: a.content_len,
            stage: a.stage,
            proofs: a.proofs.clone(),
            build_count: a.build_count,
            required_proof_kinds: self.config.required_proof_kinds.clone(),
            created_at_ms: a.created_at_ms,
        })
    }

    pub fn list(&self) -> Vec<ArtifactView> {
        let mut views: Vec<_> = self
            .artifacts
            .keys()
            .map(|id| self.view(id).unwrap())
            .collect();
        views.sort_by(|a, b| a.created_at_ms.cmp(&b.created_at_ms).then(a.id.cmp(&b.id)));
        views
    }

    pub fn history(&self, id: &str) -> AppResult<HistoryView> {
        let a = self.get(id)?;
        Ok(HistoryView {
            id: a.id.clone(),
            digest: a.digest.clone(),
            stage: a.stage,
            history: a.history.clone(),
        })
    }

    pub fn blob_info(&self, digest: &str) -> AppResult<BlobInfo> {
        let digest = normalize_digest(digest)?;
        match self.blobs.get(&digest) {
            Some(slot) => Ok(BlobInfo {
                digest,
                content_len: slot.content.len(),
                reference_count: slot.references,
                stored: true,
            }),
            None => Ok(BlobInfo {
                digest,
                content_len: 0,
                reference_count: 0,
                stored: false,
            }),
        }
    }

    pub fn blob_content(&self, digest: &str) -> AppResult<Vec<u8>> {
        let digest = normalize_digest(digest)?;
        self.blobs
            .get(&digest)
            .map(|s| s.content.clone())
            .ok_or_else(|| AppError::not_found(format!("blob {digest} not found in storage")))
    }

    pub fn artifact_content(&self, id: &str) -> AppResult<(String, Vec<u8>)> {
        let a = self.get(id)?;
        let content = self
            .blobs
            .get(&a.digest)
            .map(|s| s.content.clone())
            .ok_or_else(|| {
                AppError::not_found(format!(
                    "backing blob {} for artifact {} is missing",
                    a.digest, a.id
                ))
            })?;
        Ok((a.digest.clone(), content))
    }

    // ---------- 写入（状态机） ----------

    /// 注册制品：计算摘要、写入 CAS、绑定摘要、进入 development。
    pub fn register(&mut self, content: &[u8]) -> ArtifactView {
        let digest = sha256_digest(content);
        let at_ms = now_ms();

        let build_count = match self.blobs.get_mut(&digest) {
            Some(slot) => {
                // 相同内容：不重写，只增加引用。
                slot.references += 1;
                slot.writes
            }
            None => {
                self.blobs.insert(
                    digest.clone(),
                    BlobSlot {
                        content: content.to_vec(),
                        references: 1,
                        writes: 1,
                    },
                );
                1
            }
        };

        let id = uuid::Uuid::new_v4().to_string();
        let record = ArtifactRecord {
            id: id.clone(),
            digest: digest.clone(),
            content_len: content.len(),
            stage: Stage::Development,
            proofs: Vec::new(),
            build_count,
            created_at_ms: at_ms,
            history: vec![Event::Registered {
                digest,
                content_len: content.len(),
                at_ms,
            }],
        };
        self.artifacts.insert(id.clone(), record);
        self.view(&id).unwrap()
    }

    /// 登记测试证明；摘要必须与制品绑定摘要一致。
    pub fn record_proof(&mut self, id: &str, req: ProofRequest) -> AppResult<ArtifactView> {
        let kind = req.kind.trim().to_string();
        if kind.is_empty() {
            return Err(AppError::bad_request("proof kind must not be empty"));
        }
        let digest = normalize_digest(&req.digest)?;
        let at_ms = now_ms();

        let a = self.get_mut(id)?;
        if digest != a.digest {
            return Err(AppError::conflict(format!(
                "proof digest {digest} does not match immutable artifact digest {}",
                a.digest
            )));
        }
        a.proofs.push(Proof {
            kind,
            digest: digest.clone(),
            detail: req.detail,
            passed: req.passed,
            recorded_at_ms: at_ms,
        });
        // 重新取一遍 kind/detail 用于事件（字段已被 move 进 Proof）。
        let (kind, detail, passed) = {
            let p = a.proofs.last().unwrap();
            (p.kind.clone(), p.detail.clone(), p.passed)
        };
        a.history.push(Event::ProofRecorded {
            kind,
            digest,
            passed,
            detail,
            at_ms,
        });
        Ok(self.view(id).unwrap())
    }

    /// 晋级。目标阶段必须恰为当前阶段的下一阶段。
    /// 晋级到 release 需要：审批通过 + 审批人非空 + 全部必要证明均有 passed=true 记录。
    pub fn promote(&mut self, id: &str, req: PromoteRequest) -> AppResult<ArtifactView> {
        // 1) 先在不可变借用下完成状态机与门禁判定，避免与下面的可变借用冲突。
        let (from, to) = {
            let a = self.get(id)?;
            let from = a.stage;
            let to = from.next().ok_or_else(|| {
                AppError::conflict(format!(
                    "artifact {id} is already at terminal stage {}",
                    from.as_str()
                ))
            })?;

            if to == Stage::Release {
                if !req.approved {
                    return Err(AppError::precondition(
                        "promotion to release requires approval (approved=true)",
                    ));
                }
                let approver = req.approver.as_deref().unwrap_or("").trim();
                if approver.is_empty() {
                    return Err(AppError::bad_request(
                        "promotion to release requires a non-empty approver",
                    ));
                }
                let bound_digest = a.digest.clone();
                let missing: Vec<String> = self
                    .config
                    .required_proof_kinds
                    .iter()
                    .filter(|required| {
                        !a.proofs.iter().any(|p| {
                            p.passed && p.kind == required.as_str() && p.digest == bound_digest
                        })
                    })
                    .cloned()
                    .collect();
                if !missing.is_empty() {
                    return Err(AppError::precondition(format!(
                        "release gate failed: missing passing proof(s) for digest {bound_digest}: {}",
                        missing.join(", ")
                    )));
                }
            }
            (from, to)
        };

        // 2) 门禁通过，执行跃迁并追加历史。
        let at_ms = now_ms();
        let approver = req
            .approver
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string);
        let a = self.get_mut(id)?;
        a.stage = to;
        a.history.push(Event::Promoted {
            from,
            to,
            approved: req.approved,
            approver,
            at_ms,
        });
        Ok(self.view(id).unwrap())
    }

    /// 回退一级。只移动阶段指针并追加历史；blob 完全不动。
    pub fn rollback(&mut self, id: &str, req: RollbackRequest) -> AppResult<ArtifactView> {
        let a = self.get_mut(id)?;
        let from = a.stage;
        let to = from.prev().ok_or_else(|| {
            AppError::conflict(format!(
                "artifact {id} is at development and cannot roll back further"
            ))
        })?;
        let at_ms = now_ms();
        a.stage = to;
        a.history.push(Event::RolledBack {
            from,
            to,
            reason: req.reason.unwrap_or_default(),
            at_ms,
        });
        Ok(self.view(id).unwrap())
    }
}
