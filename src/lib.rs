//! 增量构建依赖图规划服务（库 crate）。
//!
//! - [`graph`]：依赖图模型、静态校验、环定位、拓扑排序；
//! - [`engine`]：缓存键、变更分类（内容 vs 仅时间戳）、受影响传播、计划生成；
//! - [`api`]：Axum HTTP 路由。

pub mod api;
pub mod engine;
pub mod graph;
