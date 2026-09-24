//! 领域模型:阶段、制品、证明、审批与历史事件。

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// 晋级阶段(严格有序,只允许逐级前进)。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    /// 开发:制品登记后的初始阶段
    Dev,
    /// 验证:已通过开发侧审批,且存在 passed 测试证明
    Verification,
    /// 发布:已通过发布侧审批,且具备必要的发布门槛证明
    Release,
}

impl Stage {
    pub fn as_str(&self) -> &'static str {
        match self {
            Stage::Dev => "dev",
            Stage::Verification => "verification",
            Stage::Release => "release",
        }
    }

    /// 阶段在晋级链上的序号,用于逐级校验。
    pub fn order(&self) -> u8 {
        match self {
            Stage::Dev => 0,
            Stage::Verification => 1,
            Stage::Release => 2,
        }
    }

    /// 晋级的下一个阶段;Release 已是终点。
    pub fn next(&self) -> Option<Stage> {
        match self {
            Stage::Dev => Some(Stage::Verification),
            Stage::Verification => Some(Stage::Release),
            Stage::Release => None,
        }
    }

    pub fn all() -> [Stage; 3] {
        [Stage::Dev, Stage::Verification, Stage::Release]
    }
}

impl fmt::Display for Stage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Stage {
    type Err = ();
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "dev" => Ok(Stage::Dev),
            "verification" => Ok(Stage::Verification),
            "release" => Ok(Stage::Release),
            _ => Err(()),
        }
    }
}

/// 触发一次阶段变化的动作类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Trigger {
    Promotion,
    Rollback,
}

/// 不可变的阶段历史条目。回退只会追加,从不改写或删除历史。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StageEvent {
    pub from: Stage,
    pub to: Stage,
    pub trigger: Trigger,
    /// 变化瞬间制品绑定的摘要,用于审计"同一不可变摘要"。
    pub digest: String,
    pub reason: Option<String>,
    pub at: String,
}

/// 制品:标识 + 绑定的不可变摘要。内容本身存放在按摘要寻址的内容库中。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Artifact {
    pub id: String,
    pub digest: String,
    pub stage: Stage,
    /// 每次晋级/回退都追加一条;初始登记也记录为 dev 起点。
    pub history: Vec<StageEvent>,
    pub created_at: String,
}

/// 测试证明的结论。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProofResult {
    Passed,
    Failed,
}

impl FromStr for ProofResult {
    type Err = ();
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "passed" => Ok(ProofResult::Passed),
            "failed" => Ok(ProofResult::Failed),
            _ => Err(()),
        }
    }
}

/// 测试证明。`digest` 必须与所引用制品绑定的摘要严格一致,否则不被接受。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Proof {
    pub id: String,
    pub artifact_id: String,
    /// 证明所引用的制品摘要(创建时校验 == artifact.digest)。
    pub digest: String,
    /// 证明类型,例如 "unit-test"、"integration-test"、"release-gate"。
    pub kind: String,
    pub result: ProofResult,
    pub detail: Option<String>,
    pub created_at: String,
}

/// 审批决定。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Decision {
    Approved,
    Rejected,
}

/// 审批记录。审批同样绑定摘要:换了内容(摘要变了)的制品无法复用旧审批。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Approval {
    pub id: String,
    pub artifact_id: String,
    pub digest: String,
    /// 审批所针对的目标阶段:verification 或 release。
    pub target_stage: Stage,
    pub approver: String,
    pub decision: Decision,
    pub comment: Option<String>,
    pub created_at: String,
}

/// 对外返回的制品完整视图(含证明与审批)。
#[derive(Debug, Clone, Serialize)]
pub struct ArtifactView {
    pub id: String,
    pub digest: String,
    pub stage: Stage,
    pub created_at: String,
    pub history: Vec<StageEvent>,
    pub proofs: Vec<Proof>,
    pub approvals: Vec<Approval>,
}
