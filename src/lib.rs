//! 区间锁（range lock）服务核心库。
//!
//! 模块划分：
//! - [`engine`]：与传输层无关的锁引擎（区间锁、等待队列、等待图、死锁检测）。
//! - [`http`]：基于 Axum 的 HTTP 接口。

pub mod engine;
pub mod http;

pub use engine::{
    AcquireReport, DeadlockInfo, EdgeView, Engine, EngineError, LockView, Mode, ResourceView,
    TxnView, WaiterView, WaitsView,
};
