//! Error taxonomy.
//!
//! Every failure has a stable, machine-readable [`ErrorKind`] so that callers
//! (including the independent test suite and the HTTP layer) can assert the
//! *exact* failure category. Failures are never collapsed into a generic
//! success or into `unknown`.

use std::fmt;

/// Stable failure categories. The numeric/string representation is part of the
/// public API contract and is mirrored 1:1 by the HTTP error body `kind` field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// Referenced column does not exist in the typed schema.
    ColumnNotFound,
    /// A literal or column value was used with an incompatible logical type.
    TypeMismatch,
    /// Predicate JSON contains an operator this engine does not know.
    UnknownOperator,
    /// Predicate JSON is structurally invalid (empty `and`/`or`, missing field, ...).
    InvalidPredicate,
    /// A row index is outside the physical document universe `[0, row_count)`.
    RowIndexOutOfBounds,
    /// A deletion was requested for a row that is already deleted at that version.
    RowAlreadyDeleted,
    /// Delete events must arrive in strictly increasing version order.
    VersionOutOfOrder,
    /// Two bitmaps of different universes were combined.
    LengthMismatch,
    /// A bitmap/tri-partition invariant was violated (overlapping T/F, dirty tail bits, ...).
    InvalidBitmap,
    /// An index with the same (possibly composite) key already exists with a conflicting shape.
    IndexConflict,
    /// Requested a table id that is not present in the catalog.
    TableNotFound,
    /// Requested an execution strategy that is not `single`|`combined`|`auto`.
    UnknownStrategy,
    /// Asked for a read version newer than any version the table has observed.
    ReadVersionInFuture,
    /// Combined-index requested for a predicate its key cannot serve.
    CombinedIndexUnavailable,
    /// Configuration file / environment value could not be parsed.
    InvalidConfig,
    /// Arrow data did not satisfy the declared schema or supported type set.
    ArrowSchemaMismatch,
    /// A table with the same id already exists in the catalog.
    TableExists,
    /// A configured resource limit (e.g. max rows per table) would be exceeded.
    ResourceLimit,
    /// An HTTP request body was not valid JSON or missed required fields.
    MalformedRequest,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::ColumnNotFound => "COLUMN_NOT_FOUND",
            ErrorKind::TypeMismatch => "TYPE_MISMATCH",
            ErrorKind::UnknownOperator => "UNKNOWN_OPERATOR",
            ErrorKind::InvalidPredicate => "INVALID_PREDICATE",
            ErrorKind::RowIndexOutOfBounds => "ROW_INDEX_OUT_OF_BOUNDS",
            ErrorKind::RowAlreadyDeleted => "ROW_ALREADY_DELETED",
            ErrorKind::VersionOutOfOrder => "VERSION_OUT_OF_ORDER",
            ErrorKind::LengthMismatch => "LENGTH_MISMATCH",
            ErrorKind::InvalidBitmap => "INVALID_BITMAP",
            ErrorKind::IndexConflict => "INDEX_CONFLICT",
            ErrorKind::TableNotFound => "TABLE_NOT_FOUND",
            ErrorKind::UnknownStrategy => "UNKNOWN_STRATEGY",
            ErrorKind::ReadVersionInFuture => "READ_VERSION_IN_FUTURE",
            ErrorKind::CombinedIndexUnavailable => "COMBINED_INDEX_UNAVAILABLE",
            ErrorKind::InvalidConfig => "INVALID_CONFIG",
            ErrorKind::ArrowSchemaMismatch => "ARROW_SCHEMA_MISMATCH",
            ErrorKind::TableExists => "TABLE_EXISTS",
            ErrorKind::ResourceLimit => "RESOURCE_LIMIT",
            ErrorKind::MalformedRequest => "MALFORMED_REQUEST",
        }
    }

    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::TableNotFound => 404,
            ErrorKind::ColumnNotFound
            | ErrorKind::TypeMismatch
            | ErrorKind::UnknownOperator
            | ErrorKind::InvalidPredicate
            | ErrorKind::RowIndexOutOfBounds
            | ErrorKind::LengthMismatch
            | ErrorKind::InvalidBitmap
            | ErrorKind::UnknownStrategy
            | ErrorKind::CombinedIndexUnavailable
            | ErrorKind::InvalidConfig
            | ErrorKind::MalformedRequest
            | ErrorKind::ArrowSchemaMismatch => 400,
            ErrorKind::RowAlreadyDeleted
            | ErrorKind::VersionOutOfOrder
            | ErrorKind::ReadVersionInFuture
            | ErrorKind::TableExists
            | ErrorKind::IndexConflict => 409,
            ErrorKind::ResourceLimit => 413,
        }
    }
}

/// Crate-wide error type.
#[derive(Debug, Clone)]
pub struct IndexError {
    pub kind: ErrorKind,
    context: String,
}

impl IndexError {
    pub fn new(kind: ErrorKind, context: impl Into<String>) -> Self {
        Self { kind, context: context.into() }
    }

    pub fn context(&self) -> &str {
        &self.context
    }
}

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.kind.as_str(), self.context)
    }
}

impl std::error::Error for IndexError {}

pub type Result<T> = std::result::Result<T, IndexError>;
