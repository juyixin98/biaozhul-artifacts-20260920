//! 版本化 Gas 计量器 —— 离线 WASM 执行沙箱。
//!
//! 参见 README。核心模块：
//! * [`versions`]：计量版本与计价规则（确定性的版本化承诺）
//! * [`sandbox`]：Wasmtime 沙箱、白名单、燃料/内存限制、事务缓冲
//! * [`state`]：宿主 KV 的快照隔离与原子发布
//! * [`receipt`]：可复现的执行收据与可解释计量
//! * [`api`]：Axum HTTP 接口
//!
//! **重要声明**：本项目使用的燃料（fuel）是 Wasmtime 的抽象计量单位，
//! 不是任何区块链的真实 Gas，也不声称二者之间存在换算关系。

pub mod api;
pub mod receipt;
pub mod samples;
pub mod sandbox;
pub mod state;
pub mod versions;

pub use api::{router, AppState};
pub use receipt::{ChargeItem, ExecStatus, Receipt};
pub use sandbox::{
    execute, Engines, ExecRequest, DEFAULT_FUEL_LIMIT, DEFAULT_MEMORY_LIMIT, DEFAULT_TABLE_LIMIT,
};
pub use state::HostState;
pub use versions::MeteringVersion;
