//! Error types with explicit, machine-readable failure categories.
//!
//! Every rejected request carries a [`ReasonCode`] so callers (and the
//! diagnostics log) can tell *why* something was refused instead of getting a
//! generic string.

use axum::http::StatusCode;
use serde::Serialize;

/// Coarse decision recorded for every request.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    /// Request was valid and a full answer was produced.
    Accepted,
    /// Request was valid but the output/access budget stopped enumeration.
    /// The answer is *not known to be complete*; a resume cursor is returned.
    Incomplete,
    /// Request was structurally or semantically invalid.
    Rejected,
}

/// Machine-readable failure categories.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasonCode {
    /// Atom references a relation that is not in the catalog.
    UnknownRelation,
    /// Atom variable count differs from relation arity.
    ArityMismatch,
    /// One atom binds the same variable name twice, e.g. `R(a, a)`.
    RepeatedVariable,
    /// Atom orders contradict each other, e.g. `R(a,b)` vs `S(b,a)`: no
    /// global variable order exists that both tries can share.
    IncompatibleOrder,
    /// A caller-supplied variable `order` is not consistent with the atoms.
    BadOrderHint,
    /// A row contained NULL while the policy was `reject`.
    NullRowRejected,
    /// Batch shape was invalid (jagged rows, bad column name, ...).
    MalformedBatch,
    /// Value type outside the supported typed domain.
    UnsupportedType,
    /// Duplicate column name in a typed batch.
    DuplicateColumn,
    /// `limit` was zero or larger than the configured ceiling.
    InvalidLimit,
    /// Opaque cursor could not be decoded.
    CursorDecode,
    /// Cursor was issued against a different catalog version (data changed).
    CursorVersion,
    /// Cursor was issued for a different query shape.
    CursorPlan,
    /// Join has more variables than the engine is configured to support.
    TooManyVariables,
    /// Request body could not be parsed as JSON.
    MalformedJson,
    /// Request body exceeded the configured size cap.
    PayloadTooLarge,
    /// Query contained no atoms.
    EmptyQuery,
    /// Relation already loaded and `replace` was not set.
    RelationExists,
    /// Relation name is empty or contains unsupported characters.
    InvalidRelationName,
}

impl ReasonCode {
    pub fn http_status(self) -> StatusCode {
        match self {
            ReasonCode::UnknownRelation
            | ReasonCode::NullRowRejected
            | ReasonCode::MalformedBatch
            | ReasonCode::UnsupportedType
            | ReasonCode::DuplicateColumn
            | ReasonCode::InvalidRelationName
            | ReasonCode::MalformedJson => StatusCode::BAD_REQUEST,
            ReasonCode::CursorVersion | ReasonCode::CursorPlan | ReasonCode::RelationExists => {
                StatusCode::CONFLICT
            }
            ReasonCode::PayloadTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            ReasonCode::ArityMismatch
            | ReasonCode::RepeatedVariable
            | ReasonCode::IncompatibleOrder
            | ReasonCode::BadOrderHint
            | ReasonCode::InvalidLimit
            | ReasonCode::CursorDecode
            | ReasonCode::EmptyQuery
            | ReasonCode::TooManyVariables => StatusCode::UNPROCESSABLE_ENTITY,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Reason {
    pub code: ReasonCode,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl Reason {
    pub fn new(code: ReasonCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{reason}")]
    Validation { reason: Reason },
    #[error("execution stopped before the answer was known complete: {0}")]
    Budget(String),
    #[error("arrow conversion failed: {0}")]
    Arrow(String),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

impl Error {
    pub fn validation(code: ReasonCode, message: impl Into<String>) -> Self {
        Error::Validation {
            reason: Reason::new(code, message),
        }
    }

    pub fn reason(&self) -> Option<&Reason> {
        match self {
            Error::Validation { reason } => Some(reason),
            _ => None,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
