//! 增量构建依赖图规划服务（纯后端）。
//!
//! 模块划分：
//! - [`model`]：HTTP 请求/响应与领域模型
//! - [`graph`]：图校验、环定位、拓扑排序
//! - [`planner`]：缓存键、变更识别、增量规划与全量对照模拟
//! - [`api`]：Axum 路由与处理器

pub mod api;
pub mod graph;
pub mod model;
pub mod planner;
