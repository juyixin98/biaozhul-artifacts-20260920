//! decorr-service: restricted correlated-subquery decorrelation engine.
//!
//! Module responsibilities (see README/ARCHITECTURE):
//!   * [`batch`]      — typed columnar batches on arrow2
//!   * [`ast`]        — restricted logical IR
//!   * [`parser`]     — SQL -> IR lowering and unsupported-form rejection
//!   * [`semantic`]   — validation, name/type resolution, correlation classification
//!   * [`operators`]  — predicate tri-state logic, aggregates, emitter, outer runner
//!   * [`interpreter`]— row-by-row reference executor
//!   * [`decorrel`]   — decorrelating compiler + hash-group executor (+ proof)
//!   * [`state`]      — catalog/resources from local fixtures and inline tables
//!   * [`config`]     — process configuration
//!   * [`service`]    — end-to-end query entry point (parse/validate/compile/run)
//!   * [`http`]       — Axum validation/execution endpoint and logging

pub mod ast;
pub mod batch;
pub mod config;
pub mod decorrel;
pub mod error;
pub mod interpreter;
pub mod operators;
pub mod parser;
pub mod semantic;
pub mod service;
pub mod state;

pub mod http;
