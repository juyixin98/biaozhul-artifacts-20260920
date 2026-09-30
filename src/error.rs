//! Engine-wide error type with explicit failure categories.
//!
//! The [`ErrorKind`] is what tests assert against and what the API surfaces as
//! `error_kind`; it never collapses to "something failed".

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Syntactic parse failure.
    Parse,
    /// Semantic validation failure (unknown table/column, type mismatch).
    Validation,
    /// SQL form the service deliberately does not support, e.g. NOT IN.
    UnsupportedForm,
    /// A restricted-scalar subquery produced more than one row.
    CardinalityViolation,
    /// Resource limit exceeded.
    LimitExceeded,
    /// Invariant violated inside the engine (bug, not bad input).
    Internal,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::Parse => "parse_error",
            ErrorKind::Validation => "validation_error",
            ErrorKind::UnsupportedForm => "unsupported_form",
            ErrorKind::CardinalityViolation => "cardinality_violation",
            ErrorKind::LimitExceeded => "limit_exceeded",
            ErrorKind::Internal => "internal_error",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct EngineError {
    pub kind: ErrorKind,
    pub message: String,
}

impl EngineError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        EngineError {
            kind,
            message: message.into(),
        }
    }

    pub fn validation(msg: impl Into<String>) -> Self {
        Self::new(ErrorKind::Validation, msg)
    }

    pub fn unsupported(msg: impl Into<String>) -> Self {
        Self::new(ErrorKind::UnsupportedForm, msg)
    }

    pub fn internal(msg: impl Into<String>) -> Self {
        Self::new(ErrorKind::Internal, msg)
    }
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.kind, self.message)
    }
}

impl std::error::Error for EngineError {}

pub type EngineResult<T> = Result<T, EngineError>;
