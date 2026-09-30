//! Error contract shared across the typed-batch, operator, resource and HTTP layers.
//!
//! Every fallible operation in the backend returns [`Error`]. The top-level
//! [`ErrorKind`] is stable and serialised over the wire so that clients (and the
//! verification entry point) can distinguish, without parsing prose, between:
//!
//! * bad caller input (`Input`) — the request is malformed and *may* be retried
//!   after correction,
//! * conflicting engine/job state (`State`) — e.g. unknown job id, fetching an
//!   unfinished job,
//! * resource exhaustion (`Resource`) — an explicit budget/limit was exceeded,
//! * an internal computation failure (`Compute`).
//!
//! `Input` maps to HTTP 400, `State` to 404/409, `Resource` to 413/507 and
//! `Compute` to 500.

use serde::Serialize;

/// Stable, wire-safe category of a backend error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Malformed request: schema/type mismatch, unknown column, bad predicate,
    /// unsupported datatype, parse failure.
    Input,
    /// Resource budget exceeded: output row cap, candidate-probe cap,
    /// memory cap, result-set truncation.
    Resource,
    /// State conflict: unknown/duplicated job, poll before completion,
    /// invalid state transition.
    State,
    /// Internal computation failure that is not the caller's fault.
    Compute,
}

impl ErrorKind {
    /// HTTP status code used by the API layer.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::Input => 400,
            ErrorKind::State => 409,
            ErrorKind::Resource => 507,
            ErrorKind::Compute => 500,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::Input => "input",
            ErrorKind::Resource => "resource",
            ErrorKind::State => "state",
            ErrorKind::Compute => "compute",
        }
    }
}

/// Structured error type carried by every `Result` in the crate.
#[derive(Debug, Clone, Serialize)]
pub struct Error {
    pub kind: ErrorKind,
    /// Short machine-readable code, e.g. `unknown_column`.
    pub code: String,
    /// Human-readable explanation.
    pub message: String,
}

impl Error {
    pub fn new(kind: ErrorKind, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind,
            code: code.into(),
            message: message.into(),
        }
    }

    pub fn input(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Input, code, message)
    }

    pub fn resource(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Resource, code, message)
    }

    pub fn state(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::State, code, message)
    }

    pub fn compute(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Compute, "compute_failure", message)
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}: {}", self.kind.as_str(), self.code, self.message)
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;
