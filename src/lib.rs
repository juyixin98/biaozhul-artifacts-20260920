//! Build output-tree merge service — library crate.
//!
//! Modules:
//! - [`path`]: relative path and symlink-target normalization
//! - [`model`]: domain types
//! - [`planner`]: side-effect-free conflict detection and plan building
//! - [`apply`]: atomic filesystem application of a conflict-free plan
//! - [`api`]: Axum HTTP interface

pub mod api;
pub mod apply;
pub mod model;
pub mod path;
pub mod planner;
