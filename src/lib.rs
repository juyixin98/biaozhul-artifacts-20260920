//! # lsm-kv
//!
//! 一个简化的 LSM-Tree KV 服务：
//! - 内存表（mutable memtable）+ 不可变内存表（immutable memtables）
//! - 不可变有序段（每个 level 内按 key 有序、段内唯一）
//! - 点查与范围扫描（多版本按 seq 取最新，墓碑遮蔽旧值）
//! - 合并时按版本选值；墓碑只有在“确认所有更老层都不存在同键”时才丢弃
//! - 新段发布通过“临时文件 + fsync + rename + 目录 fsync”原子切换清单

pub mod config;
pub mod db;
pub mod http;
pub mod manifest;
pub mod segment;

pub use config::Config;
pub use db::{CrashPoint, Db, StateSnapshot};
pub use segment::Entry;

use std::sync::Arc;

/// 供 `main.rs` 与集成测试共用的路由构造函数。
pub fn app(db: Arc<Db>) -> axum::Router {
    http::router(db)
}
