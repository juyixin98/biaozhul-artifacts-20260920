//! Error model shared across validation, execution and the HTTP boundary.
//!
//! Every error carries a stable [`ErrorCode`] so tests can assert the *failure
//! category*, not merely that a call failed.

use std::fmt;

/// Stable, machine-readable failure categories.
///
/// Numbers are part of the public contract and are asserted by the integration
/// tests (`tests/api_contract.rs`); do not renumber existing variants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum ErrorCode {
    /// A referenced relation is not present in the catalog.
    UnknownRelation = 1001,
    /// A referenced variable is not present in any bound relation.
    UnknownVariable = 1002,
    /// A relation lists a variable twice.
    DuplicateVariable = 1003,
    /// A join variable does not occur in at least two relations (cannot be
    /// intersected / the join hypergraph is not connected on that variable).
    UnboundJoinVariable = 1004,
    /// The join hypergraph is disconnected (cartesian-product style query).
    DisconnectedQuery = 1005,
    /// A purely cross-product / single-table "join" was requested while the
    /// backend is restricted to genuine natural joins.
    NotANaturalJoin = 1006,
    /// Incompatible null policy vs. data, or unsupported null policy.
    NullPolicyViolation = 2001,
    /// A column value could not be domain-encoded.
    InvalidValue = 2002,
    /// CSV payload could not be decoded.
    InvalidCsv = 2003,
    /// Column type/arity mismatch while building a relation.
    SchemaMismatch = 2004,
    /// A resume cursor is malformed or for the wrong query/order.
    InvalidCursor = 3001,
    /// Request limit outside the allowed range.
    InvalidLimit = 3002,
    /// Malformed JSON request body.
    InvalidRequestBody = 3003,
    /// Internal invariant violation (a bug, surfaced rather than swallowed).
    Internal = 5000,
}

impl ErrorCode {
    /// Numeric wire representation.
    pub fn as_u16(self) -> u16 {
        self as u16
    }

    /// Short kebab identifier used in JSON responses and logs.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::UnknownRelation => "unknown_relation",
            ErrorCode::UnknownVariable => "unknown_variable",
            ErrorCode::DuplicateVariable => "duplicate_variable",
            ErrorCode::UnboundJoinVariable => "unbound_join_variable",
            ErrorCode::DisconnectedQuery => "disconnected_query",
            ErrorCode::NotANaturalJoin => "not_a_natural_join",
            ErrorCode::NullPolicyViolation => "null_policy_violation",
            ErrorCode::InvalidValue => "invalid_value",
            ErrorCode::InvalidCsv => "invalid_csv",
            ErrorCode::SchemaMismatch => "schema_mismatch",
            ErrorCode::InvalidCursor => "invalid_cursor",
            ErrorCode::InvalidLimit => "invalid_limit",
            ErrorCode::InvalidRequestBody => "invalid_request_body",
            ErrorCode::Internal => "internal",
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The crate-wide error type.
#[derive(Debug, Clone)]
pub struct JoinError {
    pub code: ErrorCode,
    pub message: String,
}

impl JoinError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn unknown_relation(name: impl Into<String>) -> Self {
        Self::new(ErrorCode::UnknownRelation, format!("unknown relation: {}", name.into()))
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::Internal, message.into())
    }
}

impl fmt::Display for JoinError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for JoinError {}

/// Crate-wide result alias.
pub type Result<T> = std::result::Result<T, JoinError>;
