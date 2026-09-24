//! HTTP API 的请求 / 响应数据模型。

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

/// 单个构建动作：一个命名动作声明自己产出的输出条目集合。
#[derive(Debug, Clone, Deserialize)]
pub struct Action {
    /// 动作标识，冲突报告中用它指明来源。同一请求内必须唯一且非空。
    pub id: String,
    /// 该动作产出的输出条目，顺序不影响结果。
    #[serde(default)]
    pub outputs: Vec<OutputEntry>,
}

/// 一个输出条目（文件 / 目录 / 符号链接）。
#[derive(Debug, Clone, Deserialize)]
pub struct OutputEntry {
    /// 相对于输出根的 POSIX 风格路径，例如 `bin/tool`。
    pub path: String,
    #[serde(rename = "type")]
    pub kind: EntryKind,
    /// `file` 条目的内容，标准 base64 编码。
    #[serde(default)]
    pub content_base64: Option<String>,
    /// `symlink` 条目的链接目标（相对路径，规范化后不得逃逸出输出根）。
    #[serde(default)]
    pub target: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum EntryKind {
    File,
    Dir,
    Symlink,
}

/// `/plan` 与 `/apply` 的请求体。
#[derive(Debug, Clone, Deserialize)]
pub struct MergeRequest {
    /// 目标输出根目录（`/plan` 可不做存在性校验；`/apply` 在此目录落盘）。
    pub target_dir: String,
    /// 参与合并的构建动作，至少一个。
    pub actions: Vec<Action>,
}

/// 冲突类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConflictKind {
    /// 文件（或符号链接）与目录在同一路径上互斥。
    FileDirConflict,
    /// 同一路径被声明为不同内容 / 不同链接目标。
    ContentConflict,
    /// 同目录下仅大小写不同的两个名字（大小写折叠碰撞）。
    CaseFoldCollision,
    /// 符号链接目标为绝对路径，或经规范化后逃逸出输出根。
    SymlinkEscape,
    /// 条目自身的路径非法（绝对路径、`..`、空字节等）。
    InvalidPath,
}

/// 一条冲突。`path` 是定位该冲突的最短路径；`actions` 是所有相关来源动作。
#[derive(Debug, Clone, Serialize)]
pub struct Conflict {
    pub kind: ConflictKind,
    pub path: String,
    pub actions: Vec<String>,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannedFile {
    /// sha256 十六进制摘要。
    pub sha256: String,
    /// 共享同一份内容的来源动作（按首次出现排序）。
    pub actions: Vec<String>,
    /// 字节数。
    pub size: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannedSymlink {
    /// 规范化后的链接目标。
    pub target: String,
    pub actions: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlannedDir {
    pub actions: Vec<String>,
}

/// 合并前形成的完整计划。计划只依赖请求内容，不触碰文件系统。
#[derive(Debug, Clone, Serialize, Default)]
pub struct Plan {
    pub target_dir: String,
    pub dirs: IndexMap<String, PlannedDir>,
    pub files: IndexMap<String, PlannedFile>,
    pub symlinks: IndexMap<String, PlannedSymlink>,
    pub conflicts: Vec<Conflict>,
}

impl Plan {
    pub fn ok(&self) -> bool {
        self.conflicts.is_empty()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ApplyResult {
    pub target_dir: String,
    pub files_written: usize,
    pub dirs_created: usize,
    pub symlinks_created: usize,
    /// 去重后的内容对象数（相同内容只存一份，再硬链接到各路径）。
    pub content_objects: usize,
    pub plan: Plan,
}

/// 请求级别的错误（结构问题，而非合并冲突）。
#[derive(Debug, Clone, Serialize)]
pub struct ErrorResponse {
    pub error: String,
}
