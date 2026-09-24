//! 制品分阶段晋级（Artifact Promotion）—— 纯后端库。

pub mod app;
pub mod digest;
pub mod error;
pub mod models;
pub mod store;

pub use app::{app, AppState};
pub use store::{Config as StoreConfig, Store};
