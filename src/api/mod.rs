//! HTTP-facing layer: request/response types, the validation entry point,
//! the orchestration service (validate → rewrite → execute both strategies →
//! verify), and the Axum handlers.
//!
//! Module responsibilities (no hardcoded demo logic lives here):
//! * [`types`]   — wire DTOs
//! * [`validate`] — the validation entry point that turns a request into a
//!   [`crate::query::plan::ValidatedQuery`]
//! * [`service`]  — the pipeline orchestration and response assembly
//! * [`verify`]   — equivalence check between the two execution strategies
//! * [`http`]     — Axum router, handlers, request-id and logging middleware

pub mod http;
pub mod service;
pub mod types;
pub mod validate;
pub mod verify;
