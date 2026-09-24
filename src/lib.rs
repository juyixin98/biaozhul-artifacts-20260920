//! 构建来源证明验证服务（纯后端库）。
//!
//! 模块：
//! - [`types`]：证明陈述、DSSE 信封、请求/报告数据模型
//! - [`crypto`]：Ed25519 签名/验签与 SHA-256
//! - [`policy`]：策略引擎（可信构建器、材料来源白名单等逐条判定）
//! - [`fixtures`]：本地夹具（策略、公钥注册表、材料库）加载
//! - [`server`]：Axum HTTP 路由

pub mod crypto;
pub mod fixtures;
pub mod policy;
pub mod server;
pub mod types;

pub use crypto::{sha256, KeyRegistry, SignerIdentity};
pub use fixtures::load_fixtures;
pub use policy::{MaterialStore, Policy, Verifier};
pub use types::{Envelope, Statement, VerificationReport, VerifyRequest};
