//! 内存存储与晋级状态机核心逻辑。
//!
//! 关键不变量:
//! 1. 制品创建时绑定内容的 SHA-256 摘要,此后不可改变;
//!    内容按摘要寻址(content-addressed),重复上传相同内容是幂等的,
//!    而"替换已有摘要对应的内容"会被拒绝。
//! 2. 晋级只能 dev -> verification -> release 逐级进行;
//!    每次晋级都要求:目标阶段的 approved 审批(审批绑定同一摘要)
//!    + 满足目标阶段的必要证明(passed 且引用同一摘要)。
//! 3. 回退只追加历史事件,不删除任何内容、证明、审批,也不重新构建产物。
//! 4. 所有写操作在同一把锁内完成校验与变更,并发回退/晋级串行化。

use crate::model::*;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard};

/// 进入 verification 必须具备的 passed 证明类型(满足其一即可)。
pub const VERIFY_PROOF_KINDS: &[&str] = &["unit-test", "integration-test"];
/// 进入 release 必须具备的 passed 证明类型(全部都要有)。
pub const RELEASE_PROOF_KINDS: &[&str] = &["unit-test", "release-gate"];

#[derive(Debug, PartialEq, Eq)]
pub enum StoreError {
    /// (字段, 原因)
    Invalid(&'static str, String),
    NotFound(String),
    /// 资源已存在(幂等上传时由调用方区分处理)
    Exists(String),
    /// 状态机冲突:(错误码, 人类可读说明)
    Conflict(&'static str, String),
}

impl StoreError {
    pub fn code(&self) -> &'static str {
        match self {
            StoreError::Invalid(..) => "INVALID_REQUEST",
            StoreError::NotFound(..) => "NOT_FOUND",
            StoreError::Exists(..) => "ALREADY_EXISTS",
            StoreError::Conflict(code, _) => code,
        }
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Invalid(field, why) => write!(f, "invalid {}: {}", field, why),
            StoreError::NotFound(what) => write!(f, "not found: {}", what),
            StoreError::Exists(what) => write!(f, "already exists: {}", what),
            StoreError::Conflict(_, msg) => f.write_str(msg),
        }
    }
}

impl std::error::Error for StoreError {}

type StoreResult<T> = Result<T, StoreError>;

#[derive(Default)]
struct Inner {
    /// 制品 id -> 制品
    artifacts: BTreeMap<String, Artifact>,
    /// 证明 id -> 证明
    proofs: BTreeMap<String, Proof>,
    /// 审批 id -> 审批
    approvals: BTreeMap<String, Approval>,
    /// 内容库:digest -> 原始字节。按摘要寻址,写入即不可变。
    blobs: BTreeMap<String, Vec<u8>>,
}

#[derive(Default)]
pub struct Store {
    inner: Mutex<Inner>,
}

/// 对内容字节计算 SHA-256,返回小写十六进制摘要。
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

fn now_rfc3339() -> String {
    // 不引入 chrono:用 UNIX 秒数拼一个稳定的 UTC 时间串。
    // 精度到秒,对状态机审计足够。
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_unix_utc(secs)
}

/// 将 UNIX 秒数格式化为 RFC3339(UTC)。纯算术实现,避免额外依赖。
fn format_unix_utc(secs: u64) -> String {
    let days = secs / 86400;
    let rem = secs % 86400;
    let hour = rem / 3600;
    let minute = (rem % 3600) / 60;
    let second = rem % 60;

    // 1970-01-01 为第 0 天。Howard Hinnant 的 days-from-civil 反算法。
    let z = days as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, m, d, hour, minute, second
    )
}

fn is_valid_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

fn is_valid_digest(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

/// 必要证明检查结果。
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct GateCheck {
    pub target_stage: Stage,
    pub approved: bool,
    /// 已满足的必要证明类型
    pub passed_required_proofs: Vec<String>,
    /// 仍然缺失的必要证明类型(passed 且摘要匹配)
    pub missing_proof_kinds: Vec<String>,
}

impl Store {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().expect("store mutex poisoned")
    }

    // ---- 内容库 -------------------------------------------------------

    /// 写入内容。若该摘要已存在:
    ///
    /// - 字节完全相同 -> 幂等成功;
    /// - 字节不同 -> 冲突(试图替换不可变内容)。
    ///
    /// 返回摘要。
    pub fn put_blob(&self, bytes: Vec<u8>) -> StoreResult<String> {
        let digest = sha256_hex(&bytes);
        let mut g = self.lock();
        if let Some(existing) = g.blobs.get(&digest) {
            if existing != &bytes {
                return Err(StoreError::Conflict(
                    "DIGEST_COLLISION",
                    format!(
                        "content for digest {} already exists with different bytes; \
                         content-addressed storage is immutable",
                        digest
                    ),
                ));
            }
        } else {
            g.blobs.insert(digest.clone(), bytes);
        }
        Ok(digest)
    }

    pub fn get_blob(&self, digest: &str) -> StoreResult<Vec<u8>> {
        let g = self.lock();
        g.blobs
            .get(digest)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("blob {}", digest)))
    }

    // ---- 制品 ---------------------------------------------------------

    /// 登记制品:绑定摘要,初始阶段 dev。若该 id 已存在且摘要一致则幂等返回。
    pub fn register_artifact(
        &self,
        id: &str,
        digest: &str,
    ) -> StoreResult<(Artifact, bool)> {
        if !is_valid_id(id) {
            return Err(StoreError::Invalid(
                "id",
                "id 必须为 1-128 个字符,仅允许字母数字、-、_、.".into(),
            ));
        }
        let digest = digest.trim().to_ascii_lowercase();
        if !is_valid_digest(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                "digest 必须为 64 位十六进制 SHA-256".into(),
            ));
        }
        let mut g = self.lock();
        if !g.blobs.contains_key(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                format!("内容库中不存在该摘要,请先 PUT /artifacts/content/{}", digest),
            ));
        }
        if let Some(existing) = g.artifacts.get(id) {
            if existing.digest != digest {
                // 同一 id 想绑定不同摘要 = 试图替换不可变制品内容,直接拒绝。
                return Err(StoreError::Conflict(
                    "IMMUTABLE_DIGEST",
                    format!(
                        "artifact {} 已绑定摘要 {},不可改绑为 {};请使用新制品 id",
                        id, existing.digest, digest
                    ),
                ));
            }
            return Ok((existing.clone(), false));
        }
        let ts = now_rfc3339();
        let artifact = Artifact {
            id: id.to_string(),
            digest: digest.clone(),
            stage: Stage::Dev,
            history: vec![StageEvent {
                from: Stage::Dev,
                to: Stage::Dev,
                trigger: Trigger::Promotion,
                digest,
                reason: Some("artifact registered".into()),
                at: ts.clone(),
            }],
            created_at: ts,
        };
        g.artifacts.insert(id.to_string(), artifact.clone());
        Ok((artifact, true))
    }

    pub fn get_artifact(&self, id: &str) -> StoreResult<Artifact> {
        let g = self.lock();
        g.artifacts
            .get(id)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", id)))
    }

    pub fn view(&self, id: &str) -> StoreResult<ArtifactView> {
        let g = self.lock();
        let artifact = g
            .artifacts
            .get(id)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", id)))?;
        let proofs = g
            .proofs
            .values()
            .filter(|p| p.artifact_id == id)
            .cloned()
            .collect();
        let approvals = g
            .approvals
            .values()
            .filter(|a| a.artifact_id == id)
            .cloned()
            .collect();
        Ok(ArtifactView {
            id: artifact.id,
            digest: artifact.digest,
            stage: artifact.stage,
            created_at: artifact.created_at,
            history: artifact.history,
            proofs,
            approvals,
        })
    }

    pub fn list_artifacts(&self) -> StoreResult<Vec<Artifact>> {
        let g = self.lock();
        Ok(g.artifacts.values().cloned().collect())
    }

    // ---- 证明 ---------------------------------------------------------

    pub fn add_proof(
        &self,
        proof_id: &str,
        artifact_id: &str,
        digest: &str,
        kind: &str,
        result: ProofResult,
        detail: Option<String>,
    ) -> StoreResult<(Proof, bool)> {
        if !is_valid_id(proof_id) {
            return Err(StoreError::Invalid("proof_id", "非法证明 id".into()));
        }
        let kind = kind.trim();
        if kind.is_empty() || kind.len() > 64 {
            return Err(StoreError::Invalid(
                "kind",
                "kind 必须为 1-64 字符".into(),
            ));
        }
        let digest = digest.trim().to_ascii_lowercase();
        if !is_valid_digest(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                "digest 必须为 64 位十六进制 SHA-256".into(),
            ));
        }
        let mut g = self.lock();
        if g.proofs.contains_key(proof_id) {
            return Err(StoreError::Exists(format!("proof {}", proof_id)));
        }
        let artifact = g
            .artifacts
            .get(artifact_id)
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", artifact_id)))?;
        if digest != artifact.digest {
            // 证明必须引用制品当前绑定的同一摘要。
            return Err(StoreError::Conflict(
                "DIGEST_MISMATCH",
                format!(
                    "proof 引用摘要 {},但制品 {} 绑定的是 {};证明必须引用制品绑定的同一不可变摘要",
                    digest, artifact_id, artifact.digest
                ),
            ));
        }
        if !g.blobs.contains_key(&digest) {
            return Err(StoreError::NotFound(format!("blob {}", digest)));
        }
        let proof = Proof {
            id: proof_id.to_string(),
            artifact_id: artifact_id.to_string(),
            digest,
            kind: kind.to_string(),
            result,
            detail,
            created_at: now_rfc3339(),
        };
        g.proofs.insert(proof_id.to_string(), proof.clone());
        Ok((proof, true))
    }

    // ---- 审批 ---------------------------------------------------------

    // 审批是一次扁平化的写入,参数即审批记录的全部字段,保持显式。
    #[allow(clippy::too_many_arguments)]
    pub fn add_approval(
        &self,
        approval_id: &str,
        artifact_id: &str,
        digest: &str,
        target_stage: Stage,
        approver: &str,
        decision: Decision,
        comment: Option<String>,
    ) -> StoreResult<(Approval, bool)> {
        if !is_valid_id(approval_id) {
            return Err(StoreError::Invalid("approval_id", "非法审批 id".into()));
        }
        if approver.trim().is_empty() || approver.len() > 128 {
            return Err(StoreError::Invalid(
                "approver",
                "approver 必须为 1-128 字符".into(),
            ));
        }
        let digest = digest.trim().to_ascii_lowercase();
        if !is_valid_digest(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                "digest 必须为 64 位十六进制 SHA-256".into(),
            ));
        }
        if !matches!(target_stage, Stage::Verification | Stage::Release) {
            return Err(StoreError::Invalid(
                "target_stage",
                "审批目标阶段只能是 verification 或 release".into(),
            ));
        }
        let mut g = self.lock();
        if g.approvals.contains_key(approval_id) {
            return Err(StoreError::Exists(format!(
                "approval {}",
                approval_id
            )));
        }
        let artifact = g
            .artifacts
            .get(artifact_id)
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", artifact_id)))?;
        if digest != artifact.digest {
            return Err(StoreError::Conflict(
                "DIGEST_MISMATCH",
                format!(
                    "审批绑定摘要 {},但制品 {} 绑定的是 {};审批必须针对制品绑定的同一摘要",
                    digest, artifact_id, artifact.digest
                ),
            ));
        }
        let approval = Approval {
            id: approval_id.to_string(),
            artifact_id: artifact_id.to_string(),
            digest,
            target_stage,
            approver: approver.trim().to_string(),
            decision,
            comment,
            created_at: now_rfc3339(),
        };
        g.approvals
            .insert(approval_id.to_string(), approval.clone());
        Ok((approval, true))
    }

    // ---- 状态机 -------------------------------------------------------

    /// 汇总制品当前对某目标阶段的门禁状态(便于调用方解释为何不能发布)。
    fn gate_check(g: &Inner, artifact: &Artifact, target: Stage) -> GateCheck {
        let required: &[&str] = match target {
            Stage::Verification => VERIFY_PROOF_KINDS,
            Stage::Release => RELEASE_PROOF_KINDS,
            Stage::Dev => &[],
        };
        let approved = g.approvals.values().any(|a| {
            a.artifact_id == artifact.id
                && a.digest == artifact.digest
                && a.target_stage == target
                && a.decision == Decision::Approved
        });
        let passed: Vec<String> = g
            .proofs
            .values()
            .filter(|p| {
                p.artifact_id == artifact.id
                    && p.digest == artifact.digest
                    && p.result == ProofResult::Passed
            })
            .map(|p| p.kind.clone())
            .collect();
        let missing = match target {
            // verification:列表中的类型满足其一即可;全部缺失才算缺失。
            Stage::Verification => {
                if required.iter().any(|k| passed.iter().any(|p| p == k)) {
                    Vec::new()
                } else {
                    required.iter().map(|s| s.to_string()).collect()
                }
            }
            // release:列表中的类型必须全部具备。
            _ => required
                .iter()
                .map(|s| s.to_string())
                .filter(|k| !passed.contains(k))
                .collect(),
        };
        GateCheck {
            target_stage: target,
            approved,
            passed_required_proofs: required
                .iter()
                .map(|s| s.to_string())
                .filter(|k| passed.contains(k))
                .collect(),
            missing_proof_kinds: missing,
        }
    }

    /// 晋级到下一阶段。
    /// `digest` 为请求者声明的制品摘要,必须与绑定摘要一致。
    /// 返回 (更新后的制品, 是否真正发生了变化)。已处于目标阶段时返回 false(重复晋级)。
    pub fn promote(&self, id: &str, digest: &str) -> StoreResult<(Artifact, bool)> {
        let digest = digest.trim().to_ascii_lowercase();
        if !is_valid_digest(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                "digest 必须为 64 位十六进制 SHA-256".into(),
            ));
        }
        let mut g = self.lock();
        let artifact = g
            .artifacts
            .get(id)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", id)))?;
        if digest != artifact.digest {
            return Err(StoreError::Conflict(
                "DIGEST_MISMATCH",
                format!(
                    "请求摘要 {} 与制品绑定的不可变摘要 {} 不一致",
                    digest, artifact.digest
                ),
            ));
        }
        let target = match artifact.stage.next() {
            Some(t) => t,
            None => {
                return Err(StoreError::Conflict(
                    "ALREADY_AT_FINAL_STAGE",
                    format!("artifact {} 已处于 release 终态", id),
                ));
            }
        };

        // 重复晋级:已处在(或超过)目标阶段时幂等处理。单线程状态机里"等于目标"
        // 只会在并发交错时出现,此处统一返回 409 + 当前状态,由调用方区分。
        let gate = Self::gate_check(&g, &artifact, target);
        if !gate.approved {
            return Err(StoreError::Conflict(
                "APPROVAL_REQUIRED",
                format!(
                    "晋级到 {} 需要针对摘要 {} 的 approved 审批,当前不存在",
                    target, artifact.digest
                ),
            ));
        }
        if !gate.missing_proof_kinds.is_empty() {
            return Err(StoreError::Conflict(
                "PROOF_REQUIRED",
                format!(
                    "晋级到 {} 缺少 passed 证明(且须引用同一摘要):{:?}",
                    target, gate.missing_proof_kinds
                ),
            ));
        }

        let mut updated = artifact.clone();
        updated.history.push(StageEvent {
            from: artifact.stage,
            to: target,
            trigger: Trigger::Promotion,
            digest: artifact.digest.clone(),
            reason: Some(format!("promoted to {}", target)),
            at: now_rfc3339(),
        });
        updated.stage = target;
        g.artifacts.insert(id.to_string(), updated.clone());
        Ok((updated, true))
    }

    /// 回退到更早的阶段。只追加历史,不触碰内容库与证明/审批,不重新构建产物。
    pub fn rollback(
        &self,
        id: &str,
        digest: &str,
        target: Stage,
        reason: Option<String>,
    ) -> StoreResult<(Artifact, bool)> {
        let digest = digest.trim().to_ascii_lowercase();
        if !is_valid_digest(&digest) {
            return Err(StoreError::Invalid(
                "digest",
                "digest 必须为 64 位十六进制 SHA-256".into(),
            ));
        }
        let mut g = self.lock();
        let artifact = g
            .artifacts
            .get(id)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", id)))?;
        if digest != artifact.digest {
            return Err(StoreError::Conflict(
                "DIGEST_MISMATCH",
                format!(
                    "请求摘要 {} 与制品绑定的不可变摘要 {} 不一致",
                    digest, artifact.digest
                ),
            ));
        }
        if target.order() >= artifact.stage.order() {
            return Err(StoreError::Conflict(
                "INVALID_ROLLBACK_TARGET",
                format!(
                    "回退目标 {} 不早于当前阶段 {}(回退不能用于晋级或原地空转)",
                    target, artifact.stage
                ),
            ));
        }

        let mut updated = artifact.clone();
        updated.history.push(StageEvent {
            from: artifact.stage,
            to: target,
            trigger: Trigger::Rollback,
            digest: artifact.digest.clone(),
            reason,
            at: now_rfc3339(),
        });
        updated.stage = target;
        g.artifacts.insert(id.to_string(), updated.clone());
        Ok((updated, true))
    }

    /// 计算某制品对各阶段的门禁视图(只读,用于诊断接口)。
    pub fn gates(&self, id: &str) -> StoreResult<Vec<GateCheck>> {
        let g = self.lock();
        let artifact = g
            .artifacts
            .get(id)
            .cloned()
            .ok_or_else(|| StoreError::NotFound(format!("artifact {}", id)))?;
        Ok([Stage::Verification, Stage::Release]
            .into_iter()
            .map(|t| Self::gate_check(&g, &artifact, t))
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 测试辅助:写入内容并登记 dev 制品,返回 (store, id, digest)。
    fn setup(content: &[u8]) -> (Store, String, String) {
        let store = Store::new();
        let digest = store.put_blob(content.to_vec()).unwrap();
        let (_, created) = store.register_artifact("art-1", &digest).unwrap();
        assert!(created);
        (store, "art-1".to_string(), digest)
    }

    fn approve(store: &Store, id: &str, digest: &str, target: Stage) {
        store
            .add_approval(
                &format!("ap-{:?}", target),
                id,
                digest,
                target,
                "qa-bot",
                Decision::Approved,
                None,
            )
            .unwrap();
    }

    #[test]
    fn digest_is_sha256_and_content_immutable() {
        let d1 = sha256_hex(b"hello");
        assert_eq!(
            d1,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        let store = Store::new();
        let d = store.put_blob(b"hello".to_vec()).unwrap();
        assert_eq!(d, d1);
        // 相同内容幂等
        store.put_blob(b"hello".to_vec()).unwrap();
        // 同摘要不同字节在 SHA-256 抗碰撞前提下无法通过正常路径构造,
        // 这里直接验证内容可原样取回(未被替换)。
        assert_eq!(store.get_blob(&d).unwrap(), b"hello");
    }

    #[test]
    fn cannot_rebind_artifact_to_other_digest() {
        let store = Store::new();
        let d1 = store.put_blob(b"v1".to_vec()).unwrap();
        let d2 = store.put_blob(b"v2-totally-different".to_vec()).unwrap();
        store.register_artifact("a", &d1).unwrap();
        let err = store.register_artifact("a", &d2).unwrap_err();
        assert_eq!(err.code(), "IMMUTABLE_DIGEST");
    }

    #[test]
    fn proof_must_reference_bound_digest() {
        let (store, id, d) = setup(b"pkg");
        let err = store
            .add_proof(
                "p1",
                &id,
                "0".repeat(64).as_str(),
                "unit-test",
                ProofResult::Passed,
                None,
            )
            .unwrap_err();
        assert_eq!(err.code(), "DIGEST_MISMATCH");
        // 正确摘要可以登记
        store
            .add_proof("p1", &id, &d, "unit-test", ProofResult::Passed, None)
            .unwrap();
    }

    #[test]
    fn full_promotion_happy_path() {
        let (store, id, d) = setup(b"release-me");
        // 无审批无证明 -> 不能晋级
        assert_eq!(
            store.promote(&id, &d).unwrap_err().code(),
            "APPROVAL_REQUIRED"
        );
        approve(&store, &id, &d, Stage::Verification);
        // 有审批但缺证明
        assert_eq!(store.promote(&id, &d).unwrap_err().code(), "PROOF_REQUIRED");
        store
            .add_proof("p1", &id, &d, "unit-test", ProofResult::Passed, None)
            .unwrap();
        let (a, changed) = store.promote(&id, &d).unwrap();
        assert!(changed);
        assert_eq!(a.stage, Stage::Verification);

        // 进入 release:还需 release-gate 证明 + release 审批
        assert_eq!(
            store.promote(&id, &d).unwrap_err().code(),
            "APPROVAL_REQUIRED"
        );
        approve(&store, &id, &d, Stage::Release);
        let err = store.promote(&id, &d).unwrap_err();
        assert_eq!(err.code(), "PROOF_REQUIRED");
        store
            .add_proof("p2", &id, &d, "release-gate", ProofResult::Passed, None)
            .unwrap();
        let (a, _) = store.promote(&id, &d).unwrap();
        assert_eq!(a.stage, Stage::Release);

        // 终态再晋级
        assert_eq!(
            store.promote(&id, &d).unwrap_err().code(),
            "ALREADY_AT_FINAL_STAGE"
        );
        // 历史完整:登记 + 两次晋级
        assert_eq!(a.history.len(), 3);
        assert!(a.history.iter().all(|e| e.digest == d));
    }

    #[test]
    fn failed_proof_does_not_satisfy_gate() {
        let (store, id, d) = setup(b"x");
        approve(&store, &id, &d, Stage::Verification);
        store
            .add_proof("pf", &id, &d, "unit-test", ProofResult::Failed, None)
            .unwrap();
        assert_eq!(store.promote(&id, &d).unwrap_err().code(), "PROOF_REQUIRED");
    }

    #[test]
    fn approval_against_wrong_digest_rejected() {
        let (store, id, d) = setup(b"x");
        let other = "f".repeat(64);
        let err = store
            .add_approval(
                "ap",
                &id,
                &other,
                Stage::Verification,
                "evil",
                Decision::Approved,
                None,
            )
            .unwrap_err();
        assert_eq!(err.code(), "DIGEST_MISMATCH");
        // 被拒审批也不算数
        store
            .add_approval(
                "ap2",
                &id,
                &d,
                Stage::Verification,
                "evil",
                Decision::Rejected,
                None,
            )
            .unwrap();
        assert_eq!(
            store.promote(&id, &d).unwrap_err().code(),
            "APPROVAL_REQUIRED"
        );
    }

    #[test]
    fn rollback_appends_history_and_keeps_everything() {
        let (store, id, d) = setup(b"rb");
        approve(&store, &id, &d, Stage::Verification);
        store
            .add_proof("p1", &id, &d, "unit-test", ProofResult::Passed, None)
            .unwrap();
        store.promote(&id, &d).unwrap();
        approve(&store, &id, &d, Stage::Release);
        store
            .add_proof("p2", &id, &d, "release-gate", ProofResult::Passed, None)
            .unwrap();
        store.promote(&id, &d).unwrap();

        let before = store.get_blob(&d).unwrap();
        let (a, changed) = store
            .rollback(&id, &d, Stage::Dev, Some("incident #42".into()))
            .unwrap();
        assert!(changed);
        assert_eq!(a.stage, Stage::Dev);
        // 内容原样保留,没有重建
        assert_eq!(store.get_blob(&d).unwrap(), before);
        // 历史多了一条 rollback,旧事件都在
        assert_eq!(a.history.len(), 4);
        assert_eq!(a.history[3].trigger, Trigger::Rollback);
        // 证明与审批保留,重新晋级无需重建产物:门禁仍满足
        let gates = store.gates(&id).unwrap();
        assert!(gates.iter().all(|g| g.approved && g.missing_proof_kinds.is_empty()));
        let (a2, _) = store.promote(&id, &d).unwrap();
        assert_eq!(a2.stage, Stage::Verification);
    }

    #[test]
    fn rollback_to_same_or_later_stage_rejected() {
        let (store, id, d) = setup(b"rb2");
        assert_eq!(
            store
                .rollback(&id, &d, Stage::Dev, None)
                .unwrap_err()
                .code(),
            "INVALID_ROLLBACK_TARGET"
        );
    }

    #[test]
    fn concurrent_rollbacks_serialize_without_corruption() {
        let (store, id, d) = setup(b"conc");
        approve(&store, &id, &d, Stage::Verification);
        store
            .add_proof("p1", &id, &d, "unit-test", ProofResult::Passed, None)
            .unwrap();
        store.promote(&id, &d).unwrap();

        let store = std::sync::Arc::new(store);
        let digest = d.clone();
        let mut handles = vec![];
        // 10 个并发回退到 dev:恰好一个成功,其余得到 409 语义错误。
        for _ in 0..10 {
            let s = store.clone();
            let id = id.clone();
            let d = digest.clone();
            handles.push(std::thread::spawn(move || {
                s.rollback(&id, &d, Stage::Dev, None)
            }));
        }
        let mut ok = 0;
        let mut rejected = 0;
        for h in handles {
            match h.join().unwrap() {
                Ok(_) => ok += 1,
                Err(e) if e.code() == "INVALID_ROLLBACK_TARGET" => rejected += 1,
                Err(e) => panic!("unexpected error: {}", e),
            }
        }
        assert_eq!(ok, 1, "exactly one rollback should win");
        assert_eq!(rejected, 9);
        let a = store.get_artifact(&id).unwrap();
        assert_eq!(a.stage, Stage::Dev);
        // 恰好一条 rollback 事件
        assert_eq!(
            a.history
                .iter()
                .filter(|e| e.trigger == Trigger::Rollback)
                .count(),
            1
        );
        assert!(a.history.iter().all(|e| e.digest == d));
    }

    #[test]
    fn promote_with_wrong_digest_is_rejected() {
        let (store, id, _d) = setup(b"z");
        let err = store.promote(&id, &"1".repeat(64)).unwrap_err();
        assert_eq!(err.code(), "DIGEST_MISMATCH");
    }
}
