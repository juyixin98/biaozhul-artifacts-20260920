//! 磁盘页式 B+ 树整数键索引服务。
//!
//! 模块划分：
//! - [`pager`]：固定大小页的磁盘读写、元数据页与空闲页链表
//! - [`node`]：叶/内部节点的内存模型、定长编解码、页大小→容量推导
//! - [`tree`]：插入分裂、删除借位/合并、根坍缩、范围读
//! - [`verify`]：分隔键 / 占用率 / 叶链全量校验与统计
//! - [`http`]：Axum HTTP 接口

pub mod http;
pub mod node;
pub mod pager;
pub mod tree;
pub mod verify;

pub use http::{build_router, AppState};
pub use tree::{BpTree, DelOutcome, PutOutcome};
pub use verify::VerifyReport;
