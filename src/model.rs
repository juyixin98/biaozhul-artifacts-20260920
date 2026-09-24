//! HTTP 请求/响应数据模型（全部使用 serde，JSON 线格式见 README）。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// 请求模型
// ---------------------------------------------------------------------------

/// 构建工具声明：缓存键必须覆盖工具名与版本。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tool {
    /// 工具名称，如 "gcc"
    pub name: String,
    /// 工具版本，如 "13.2.0"
    pub version: String,
}

/// 节点构建规则。
///
/// `environment` 是声明环境（如 `CC`、`CFLAGS`），使用 [`BTreeMap`] 保证键序确定，
/// 从而规则指纹不受 JSON 字段顺序影响。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct Rule {
    /// 实际执行的命令（规则内容的一部分）
    #[serde(default)]
    pub command: String,
    /// 使用的工具（名称+版本）
    #[serde(default)]
    pub tool: Option<Tool>,
    /// 声明环境变量
    #[serde(default)]
    pub environment: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NodeKind {
    /// 输入文件（叶子）
    Input,
    /// 构建目标
    Target,
}

/// 依赖图节点。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphNode {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: NodeKind,
    /// 依赖的节点 id 列表（被依赖节点先构建）
    #[serde(default)]
    pub depends_on: Vec<String>,
    /// 仅 target 节点需要规则
    #[serde(default)]
    pub rule: Option<Rule>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BuildGraph {
    pub nodes: Vec<GraphNode>,
}

/// 单个文件的摘要。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileSummary {
    /// 文件内容哈希（hex），由调用方提供
    pub content_hash: String,
    /// 可选修改时间（Unix 秒）。仅参与“内容未变但时间戳变了”的判定，不进入缓存键。
    #[serde(default)]
    pub mtime: Option<i64>,
}

/// 上一次构建后的快照（由服务在响应中返回，客户端持久化后随下次请求原样带回）。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// 输入节点 id -> 上次的文件摘要
    #[serde(default)]
    pub files: BTreeMap<String, FileSummary>,
    /// 目标节点 id -> 上次使用的规则
    #[serde(default)]
    pub rules: BTreeMap<String, Rule>,
    /// 目标节点 id -> 上次计算出的缓存键
    #[serde(default)]
    pub target_keys: BTreeMap<String, String>,
}

/// `POST /plan` 请求体。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PlanRequest {
    pub graph: BuildGraph,
    /// 当前文件摘要：文件路径/逻辑名 -> 摘要
    pub files: BTreeMap<String, FileSummary>,
    /// 首次构建传 null / 省略；增量构建回带上一次响应里的 next_snapshot
    #[serde(default)]
    pub baseline: Option<Snapshot>,
}

// ---------------------------------------------------------------------------
// 响应模型
// ---------------------------------------------------------------------------

/// 输入文件的变更分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputChangeKind {
    /// 内容哈希变化（含新增文件）
    ContentChanged,
    /// 内容哈希未变，仅 mtime 变化（不触发重建）
    TimestampOnly,
    /// 上次构建时不存在该输入
    NewFile,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InputChange {
    pub node: String,
    pub kind: InputChangeKind,
    pub from: Option<FileSummary>,
    pub to: FileSummary,
}

/// 规则变更的具体差异项。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleChangeDetail {
    CommandChanged,
    ToolNameChanged,
    ToolVersionChanged,
    EnvironmentChanged,
    /// 首次构建没有基线规则
    NewRule,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuleChange {
    pub node: String,
    pub details: Vec<RuleChangeDetail>,
    pub from: Option<Rule>,
    pub to: Rule,
}

/// 单个构建步骤及其被纳入本次增量构建的直接原因。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Step {
    pub node: String,
    /// 本次（增量）重建的直接原因
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub cache_key: String,
    pub rule_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanViewDto {
    /// 按拓扑顺序排列的构建步骤
    pub steps: Vec<Step>,
    pub step_count: usize,
    /// 缓存命中（跳过）的目标节点
    pub skipped: Vec<String>,
    pub skipped_count: usize,
    /// 模拟得到的各目标产物摘要
    pub artifacts: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SimulationDto {
    /// 增量方案模拟产物（跳过的节点沿用基线产物）
    pub incremental_artifacts: BTreeMap<String, String>,
    /// 不使用任何缓存、全部重新构建的模拟产物
    pub full_artifacts: BTreeMap<String, String>,
    /// 增量结果与全量结果的产物摘要是否逐节点一致
    pub equivalent: bool,
    /// 若不一致，列出差异节点（正常情况下恒为空）
    pub mismatches: Vec<String>,
    /// 所有跳过节点的基线产物与当前重算键是否一致（缓存有效性）
    pub cache_hits_valid: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EdgeDto {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CycleDto {
    /// 组成环的节点，起点在末尾重复一次，如 ["a","b","c","a"]
    pub nodes: Vec<String>,
    /// 组成环的边
    pub edges: Vec<EdgeDto>,
}

/// 成功响应（HTTP 200）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanResponse {    /// "full_build"（冷启动，无基线）或 "incremental"
    pub status: String,
    /// 去环后的全图拓扑顺序
    pub topological_order: Vec<String>,
    pub changed_inputs: Vec<InputChange>,
    /// 仅时间戳变更的输入（不触发重建）
    pub timestamp_only_inputs: Vec<String>,
    pub rule_changes: Vec<RuleChange>,
    /// 因自身变更或上游变更而必须重建的目标节点（拓扑顺序）
    pub affected_targets: Vec<String>,
    /// 本次增量构建方案
    pub incremental: PlanViewDto,
    /// 对照用的全量构建方案
    pub full_build: PlanViewDto,
    pub simulation: SimulationDto,
    /// 下次请求应原样带回的快照
    pub next_snapshot: Snapshot,
    /// files 中存在但不属于图中任何 input 节点的条目
    pub ignored_files: Vec<String>,
    /// 非致命提示（如目标节点上带了文件摘要）
    pub warnings: Vec<String>,
}

/// 规划失败（映射为 400 / 422）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum PlanError {
    /// 请求/图校验失败（HTTP 400）
    InvalidRequest { errors: Vec<String> },
    /// 依赖图中存在环（HTTP 422），携带可定位的环路径
    CycleDetected { cycle: CycleDto },
}
