//! 领域模型：阶段状态机、事件（只追加历史）、请求与响应视图。

use serde::{Deserialize, Serialize};

/// 晋级阶段（顺序即晋级方向）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    /// 开发
    Development,
    /// 验证
    Validation,
    /// 发布
    Release,
}

impl Stage {
    /// 按顺序解析阶段名（development / validation / release）。
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "development" => Some(Self::Development),
            "validation" => Some(Self::Validation),
            "release" => Some(Self::Release),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Development => "development",
            Self::Validation => "validation",
            Self::Release => "release",
        }
    }

    /// 正常晋级的下一阶段；Release 已到终点返回 None。
    pub fn next(&self) -> Option<Stage> {
        match self {
            Self::Development => Some(Self::Validation),
            Self::Validation => Some(Self::Release),
            Self::Release => None,
        }
    }

    /// 回退的上一阶段；Development 无法再回退，返回 None。
    pub fn prev(&self) -> Option<Stage> {
        match self {
            Self::Development => None,
            Self::Validation => Some(Self::Development),
            Self::Release => Some(Self::Validation),
        }
    }

    /// 从 Development 到达该阶段需要经过的晋级步数（Development=0）。
    pub fn level(&self) -> u8 {
        match self {
            Self::Development => 0,
            Self::Validation => 1,
            Self::Release => 2,
        }
    }
}

/// 一条测试/扫描证明，必须引用制品摘要。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Proof {
    /// 证明类型，如 unit-test / integration-test / security-scan。
    pub kind: String,
    /// 证明所引用的制品摘要（规范化后的 `sha256:<hex>`）。
    pub digest: String,
    /// 证明结论。
    pub detail: String,
    /// 该证明是否通过；只有 passed=true 才满足发布门禁。
    pub passed: bool,
    /// 记录时间（Unix 毫秒）。
    pub recorded_at_ms: u128,
}

/// 追加式事件流中的一条事件。历史永不删除、永不修改。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
pub enum Event {
    /// 注册制品：内容写入内容寻址存储，摘要在此绑定。
    Registered {
        digest: String,
        content_len: usize,
        at_ms: u128,
    },
    /// 记录证明。
    ProofRecorded {
        kind: String,
        digest: String,
        passed: bool,
        detail: String,
        at_ms: u128,
    },
    /// 晋级。
    Promoted {
        from: Stage,
        to: Stage,
        approved: bool,
        approver: Option<String>,
        at_ms: u128,
    },
    /// 回退：只移动当前阶段指针，不重建内容。
    RolledBack {
        from: Stage,
        to: Stage,
        reason: String,
        at_ms: u128,
    },
}

// ---------- 请求体 ----------

#[derive(Debug, Deserialize)]
pub struct PromoteRequest {
    /// 是否带审批。晋级到 release 必须为 true 且 approver 非空。
    #[serde(default)]
    pub approved: bool,
    /// 审批人。
    #[serde(default)]
    pub approver: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct RollbackRequest {
    /// 回退原因（可选）。
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ProofRequest {
    /// 证明类型。
    pub kind: String,
    /// 必须与制品当前绑定摘要一致，否则拒绝（测试必须引用该制品摘要）。
    pub digest: String,
    #[serde(default)]
    pub detail: String,
    /// 是否通过；缺省为 false，防止"登记即通过"。
    #[serde(default)]
    pub passed: bool,
}

// ---------- 响应视图 ----------

#[derive(Debug, Serialize)]
pub struct ArtifactView {
    pub id: String,
    /// 制品全程绑定的不可变摘要。
    pub digest: String,
    pub content_len: usize,
    pub stage: Stage,
    pub proofs: Vec<Proof>,
    /// 注册后内容寻址存储里同一摘要的写入次数；回退/晋级不应使其增加。
    pub build_count: u32,
    pub required_proof_kinds: Vec<String>,
    pub created_at_ms: u128,
}

#[derive(Debug, Serialize)]
pub struct HistoryView {
    pub id: String,
    pub digest: String,
    pub stage: Stage,
    pub history: Vec<Event>,
}

#[derive(Debug, Serialize)]
pub struct BlobInfo {
    pub digest: String,
    pub content_len: usize,
    /// 有多少个制品（含历史身份）引用此内容；写入次数另在制品视图中体现。
    pub reference_count: usize,
    pub stored: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_progression_is_linear() {
        assert_eq!(Stage::Development.next(), Some(Stage::Validation));
        assert_eq!(Stage::Validation.next(), Some(Stage::Release));
        assert_eq!(Stage::Release.next(), None);
    }

    #[test]
    fn stage_rollback_is_one_step() {
        assert_eq!(Stage::Release.prev(), Some(Stage::Validation));
        assert_eq!(Stage::Validation.prev(), Some(Stage::Development));
        assert_eq!(Stage::Development.prev(), None);
    }

    #[test]
    fn stage_parse_roundtrip() {
        for s in [Stage::Development, Stage::Validation, Stage::Release] {
            assert_eq!(Stage::parse(s.as_str()), Some(s));
        }
        assert_eq!(Stage::parse("whatever"), None);
    }
}
