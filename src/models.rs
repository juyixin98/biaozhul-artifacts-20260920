//! 数据模型，对齐 Bazel 远程执行 API (REAPI v2) 的核心概念。
//!
//! * [`Action`]：不可变动作描述（命令 + 输入根摘要 + 平台属性）。
//! * [`ActionResult`]：动作的输出清单（输出文件/目录、退出码、stdout/stderr）。
//! * [`OutputFile`]：输出文件，以内容摘要引用 CAS 中的不可变块。
//!
//! 所有结构体使用 `BTreeMap`/数组等确定性结构，并通过 [`canonical_json`]
//! 序列化为确定的字节串，使“同一动作 ⇒ 同一摘要”可跨进程复现。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// 内容摘要（SHA-256 十六进制 + 字节数）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Digest {
    /// 小写十六进制 SHA-256。
    pub hash: String,
    /// 内容字节数；服务端会与实际长度交叉校验。
    pub size_bytes: u64,
}

/// 输出目录（递归包含文件与子目录）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutputDirectory {
    /// 相对动作工作目录的路径，使用 `/` 分隔。
    pub path: String,
    /// 目录中直接包含的文件（已按 path 排序）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<OutputFile>,
    /// 子目录（已按 path 排序）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub directories: Vec<OutputDirectory>,
}

/// 不可变动作描述。客户端创建后放入 CAS，其摘要即动作缓存键。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Action {
    /// 规范化命令行，例如 `["gcc", "-c", "main.c", "-o", "main.o"]`。
    pub arguments: Vec<String>,
    /// 输入文件的 MERKLE 根摘要（原型中由调用方自行计算并保证对象已上传）。
    /// 为空表示无输入。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_root_digest: Option<Digest>,
    /// 平台属性（有序、确定性），例如 `{"cpu": "x86_64", "os": "linux"}`。
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub platform: BTreeMap<String, String>,
    /// 客户端选择的环境变量（有序、确定性）。
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub environment_variables: BTreeMap<String, String>,
    /// 动作工作目录（相对路径），默认为 "."。
    #[serde(default = "default_working_directory")]
    pub working_directory: String,
}

fn default_working_directory() -> String {
    ".".to_string()
}

/// 动作结果中的单个输出文件。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutputFile {
    /// 相对动作工作目录的路径。
    pub path: String,
    /// 文件内容摘要，指向 CAS 中的不可变块。
    pub digest: Digest,
    /// 是否为可执行文件。
    #[serde(default)]
    pub is_executable: bool,
}

/// 不可变输出清单。只有其中引用的全部对象存在且哈希校验通过时才允许发布。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActionResult {
    /// 进程退出码；0 表示构建成功。
    pub exit_code: i32,
    /// 标准输出：小块可直接内联，大块用摘要引用 CAS。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stdout_digest: Option<Digest>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stdout_raw: Option<String>,
    /// 标准错误，同上。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stderr_digest: Option<Digest>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stderr_raw: Option<String>,
    /// 输出文件（应按 path 排序；服务端不依赖顺序）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub output_files: Vec<OutputFile>,
    /// 输出目录树。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub output_directories: Vec<OutputDirectory>,
    /// 服务端发布时间（Unix 毫秒），仅为可观测性；不参与 Action 摘要。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub published_at_ms: Option<i64>,
}

// ---------- 请求 / 响应体 ----------

/// `POST /find-missing` 请求。
#[derive(Debug, Serialize, Deserialize)]
pub struct FindMissingRequest {
    pub digests: Vec<Digest>,
}

/// `PUT /actions/{hash}` 请求：发布动作结果。
#[derive(Debug, Serialize, Deserialize)]
pub struct PublishActionRequest {
    /// 动作描述。其规范编码的摘要必须等于 URL 中的 `{hash}`，否则 400。
    /// 服务端同时会把动作字节存入 CAS。
    pub action: Action,
    /// 待发布的输出清单。
    pub action_result: ActionResult,
}

/// 错误响应体。
#[derive(Debug, Serialize)]
pub struct ErrorResponse {
    pub error: String,
    pub message: String,
}

/// 把任意可序列化对象编码为确定性的 JSON 字节串。
///
/// 规则：无多余空白、保留 Unicode、不转义非 ASCII，字段顺序由 serde 决定
/// （结构体为声明顺序，map 为 BTreeMap 的键序）。动作摘要以此字节串计算。
pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, serde_json::Error> {
    // serde_json::to_vec 为紧凑输出；结构体字段按声明顺序、BTreeMap 按键序，
    // 因此同一逻辑值总产生同一字节串。
    serde_json::to_vec(value)
}

impl ActionResult {
    /// 遍历清单中引用的全部 CAS 摘要（文件内容、stdout/stderr 块，递归目录）。
    /// 发布与命中时都必须逐个核验这些摘要。
    pub fn referenced_digests(&self) -> Vec<&Digest> {
        let mut out = Vec::new();
        collect_files(&self.output_files, &mut out);
        for dir in &self.output_directories {
            collect_dir(dir, &mut out);
        }
        if let Some(d) = &self.stdout_digest {
            out.push(d);
        }
        if let Some(d) = &self.stderr_digest {
            out.push(d);
        }
        out
    }
}

fn collect_files<'a>(files: &'a [OutputFile], out: &mut Vec<&'a Digest>) {
    for f in files {
        out.push(&f.digest);
    }
}

fn collect_dir<'a>(dir: &'a OutputDirectory, out: &mut Vec<&'a Digest>) {
    collect_files(&dir.files, out);
    for sub in &dir.directories {
        collect_dir(sub, out);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_is_deterministic() {
        let mut env = BTreeMap::new();
        env.insert("PATH".to_string(), "/usr/bin".to_string());
        env.insert("CC".to_string(), "gcc".to_string());
        let a = Action {
            arguments: vec!["echo".into(), "hi".into()],
            input_root_digest: None,
            platform: BTreeMap::new(),
            environment_variables: env,
            working_directory: ".".into(),
        };
        let b1 = canonical_json(&a).unwrap();
        let b2 = canonical_json(&a).unwrap();
        assert_eq!(b1, b2);
        // BTreeMap 键序：CC 在 PATH 之前
        let s = String::from_utf8(b1).unwrap();
        assert!(s.find("CC").unwrap() < s.find("PATH").unwrap());
        // 紧凑无空白
        assert!(!s.contains(" "));
    }

    #[test]
    fn referenced_digests_are_collected_recursively() {
        let d = |h: &str| Digest {
            hash: h.to_string(),
            size_bytes: 1,
        };
        let result = ActionResult {
            exit_code: 0,
            stdout_digest: Some(d("a".repeat(64).as_str())),
            stdout_raw: None,
            stderr_digest: None,
            stderr_raw: None,
            output_files: vec![OutputFile {
                path: "f".into(),
                digest: d("b".repeat(64).as_str()),
                is_executable: false,
            }],
            output_directories: vec![OutputDirectory {
                path: "dir".into(),
                files: vec![OutputFile {
                    path: "dir/g".into(),
                    digest: d("c".repeat(64).as_str()),
                    is_executable: false,
                }],
                directories: vec![],
            }],
            published_at_ms: None,
        };
        let hashes: Vec<_> = result.referenced_digests().iter().map(|x| &x.hash).collect();
        assert_eq!(hashes.len(), 3);
    }
}
