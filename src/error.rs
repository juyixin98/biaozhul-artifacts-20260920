//! Error taxonomy shared by every layer of the backend.
//!
//! Four mutually exclusive failure categories are distinguished so that logs
//! and HTTP responses can tell apart:
//!
//! * [`ErrorCategory::Input`] — malformed request / schema / predicate.
//! * [`ErrorCategory::StateConflict`] — request conflicts with server state
//!   (unknown run, stale/over-ahead cursor, schema drift on append).
//! * [`ErrorCategory::ResourceExhausted`] — a hard resource budget was hit
//!   (row cap; per-batch candidate/word budget when truncation is disabled).
//! * [`ErrorCategory::ComputeFailure`] — failure discovered *during*
//!   execution rather than validation (dynamic comparison failure in the
//!   nested-loop reference engine; Arrow encoding failure).
//!
//! Budgeted output truncation is **not** an error: it is reported as
//! `complete = false` plus a machine-readable reason on the successful result.

use std::fmt;

use serde::Serialize;

/// Coarse failure category, serialized as a stable snake_case string.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCategory {
    Input,
    StateConflict,
    ResourceExhausted,
    ComputeFailure,
}

impl ErrorCategory {
    /// HTTP status used by the Axum layer.
    #[must_use]
    pub fn http_status(self) -> u16 {
        match self {
            ErrorCategory::Input => 400,
            ErrorCategory::StateConflict => 409,
            ErrorCategory::ResourceExhausted => 413,
            ErrorCategory::ComputeFailure => 500,
        }
    }

    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCategory::Input => "input",
            ErrorCategory::StateConflict => "state_conflict",
            ErrorCategory::ResourceExhausted => "resource_exhausted",
            ErrorCategory::ComputeFailure => "compute_failure",
        }
    }
}

/// Structured error. `code` is a stable machine identifier; `context` carries
/// replay-safe coordinates (row ids, column names, observed values, ...).
#[derive(Debug, Clone, Serialize)]
pub struct JoinError {
    pub category: ErrorCategory,
    pub code: &'static str,
    pub message: String,
    #[serde(skip_serializing_if = "serde_json::Map::is_empty")]
    pub context: serde_json::Map<String, serde_json::Value>,
}

impl JoinError {
    #[must_use]
    pub fn new(category: ErrorCategory, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            category,
            code,
            message: message.into(),
            context: serde_json::Map::new(),
        }
    }

    /// Attach one replay-safe context field.
    #[must_use]
    pub fn with(mut self, key: &'static str, value: impl Serialize) -> Self {
        if let Ok(v) = serde_json::to_value(value) {
            self.context.insert(key.to_owned(), v);
        }
        self
    }

    /// Attach several replay-safe context fields at once.
    #[must_use]
    pub fn with_context(mut self, extra: serde_json::Map<String, serde_json::Value>) -> Self {
        self.context.extend(extra);
        self
    }

    // -- Convenience constructors for the four categories -----------------

    pub fn input(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(ErrorCategory::Input, code, message)
    }

    pub fn state(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(ErrorCategory::StateConflict, code, message)
    }

    pub fn resource(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(ErrorCategory::ResourceExhausted, code, message)
    }

    pub fn compute(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(ErrorCategory::ComputeFailure, code, message)
    }
}

impl fmt::Display for JoinError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}: {}", self.category.as_str(), self.code, self.message)
    }
}

impl std::error::Error for JoinError {}

/// Crate-wide result alias.
pub type Result<T> = std::result::Result<T, JoinError>;
