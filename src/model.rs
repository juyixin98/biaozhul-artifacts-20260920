//! 线上数据结构：构建证明（statement）、签名信封与摘要。
//!
//! 字段顺序同时是 [`crate::crypto::canonical_message`] 使用的逻辑顺序，
//! 因此不要随意调整重名字段的语义；序列化兼容性以 JSON 字段名为准。

use serde::{Deserialize, Serialize};

/// 内容摘要。演示环境只支持 `sha256`。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Digest {
    pub alg: String,
    /// 小写十六进制摘要。
    pub hex: String,
}

/// 源提交：源码仓库地址 + 不可变修订号（git 提交哈希）。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct SourceCommit {
    pub repo: String,
    pub revision: String,
}

/// 一项构建材料：来源 URI 与内容摘要。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Material {
    pub uri: String,
    pub digest: Digest,
}

/// 构建输出（制品）的摘要。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Output {
    pub digest: Digest,
}

/// 构建证明主体（未签名部分）。
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Statement {
    /// 声明执行构建的构建器标识。
    pub builder_id: String,
    /// 源提交。
    pub source_commit: SourceCommit,
    /// 构建时记录的全部材料。
    pub materials: Vec<Material>,
    /// 构建输出摘要。
    pub output: Output,
}

/// 签名信封：证明 + 签名者公钥标识 + 十六进制 Ed25519 签名。
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Envelope {
    pub payload: Statement,
    /// 签名者公钥（hex）。验证方据此验签，再与策略中注册的公钥比对。
    pub key_id: String,
    pub signature_hex: String,
}
