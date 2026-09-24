//! 构建来源证明验证服务（纯后端库）。
//!
//! 模块划分：
//! - [`model`]：证明、信封、摘要等线上数据结构
//! - [`crypto`]：规范化编码与 Ed25519 签名/验签（ed25519-dalek）
//! - [`policy`]：可信构建器与材料来源策略
//! - [`verify`]：逐条策略判定，产出验证报告
//! - [`fixture`]：确定性本地夹具（构建器身份、材料、输出摘要）
//! - [`api`]：Axum HTTP 路由

pub mod api;
pub mod crypto;
pub mod fixture;
pub mod model;
pub mod policy;
pub mod verify;
