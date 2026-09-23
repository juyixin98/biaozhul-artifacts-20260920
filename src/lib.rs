//! 版本化 Gas 计量器：离线 WASM 执行沙箱（Wasmtime + Axum）。
//!
//! 模块划分见各文件头注释；快速入口：
//!   * [`engine::Engine::execute`] —— 同步执行一次沙箱调用；
//!   * [`api::router`]            —— HTTP API 路由。

pub mod api;
pub mod base64;
pub mod engine;
pub mod error;
pub mod samples;
pub mod schedule;
pub mod state;

pub use engine::{Engine, ExecRequest, ExecResponse, FuelReport, JournalEntry};
pub use error::{Error, TerminationKind};
pub use schedule::{HostCosts, Schedule, VersionRegistry};
pub use state::{CommittedState, Journal};

/// 构建默认依赖（注册表、持久化状态、日志）的引擎。
pub fn build_engine(
    state: std::sync::Arc<CommittedState>,
    journal: std::sync::Arc<Journal>,
) -> anyhow::Result<std::sync::Arc<Engine>> {
    Ok(std::sync::Arc::new(Engine::new(
        VersionRegistry::new(),
        state,
        journal,
    )?))
}
