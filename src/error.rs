//! Crate-wide error type. Every variant names a concrete failure category so
//! that callers (tests, HTTP layer, CLI) can assert on *what* failed instead
//! of receiving a blanket success or a stringly-typed message.

use serde::Serialize;

#[derive(Debug, thiserror::Error)]
pub enum TritmapError {
    #[error("bitmap error: {0}")]
    Bitmap(#[from] crate::bitset::BitmapError),

    #[error("partition error: {0}")]
    Partition(#[from] crate::partition::PartitionError),

    #[error("unknown column '{0}'")]
    UnknownColumn(String),

    #[error("column '{column}' has type {actual}, but literal {literal} was supplied")]
    TypeMismatch {
        column: String,
        actual: String,
        literal: String,
    },

    #[error("unsupported arrow/logical type: {0}")]
    UnsupportedType(String),

    #[error("schema mismatch: {0}")]
    SchemaMismatch(String),

    #[error("unknown table '{0}'")]
    UnknownTable(String),

    #[error("row {row} does not exist in table '{table}' (row space 0..{n})")]
    RowNotFound {
        table: String,
        row: usize,
        n: usize,
    },

    #[error("row {row} already deleted at version {deleted_at}; re-delete at version {version} rejected")]
    RowAlreadyDeleted {
        row: usize,
        deleted_at: u64,
        version: u64,
    },

    #[error("version must advance: current {current}, got {got}")]
    VersionOutOfOrder { current: u64, got: u64 },

    #[error(
        "incompatible index snapshots: '{left_col}' pinned to v{left_version} (domain {left_domain} rows) \
         vs '{right_col}' v{right_version} (domain {right_domain} rows); op {op}"
    )]
    IncompatibleIndexVersion {
        left_col: String,
        left_version: u64,
        left_domain: usize,
        right_col: String,
        right_version: u64,
        right_domain: usize,
        op: String,
    },

    #[error("query parse error at position {pos}: {message}")]
    QueryParse { pos: usize, message: String },

    #[error("config error: {0}")]
    Config(String),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("CSV error: {0}")]
    Csv(String),

    #[error("{0}")]
    Other(String),
}

impl TritmapError {
    pub fn internal(msg: impl Into<String>) -> Self {
        TritmapError::Other(msg.into())
    }

    /// Stable machine-readable category code, used in API responses and logs.
    pub fn code(&self) -> &'static str {
        match self {
            TritmapError::Bitmap(_) => "BITMAP_ERROR",
            TritmapError::Partition(_) => "PARTITION_ERROR",
            TritmapError::UnknownColumn(_) => "UNKNOWN_COLUMN",
            TritmapError::TypeMismatch { .. } => "TYPE_MISMATCH",
            TritmapError::UnsupportedType(_) => "UNSUPPORTED_TYPE",
            TritmapError::SchemaMismatch(_) => "SCHEMA_MISMATCH",
            TritmapError::UnknownTable(_) => "UNKNOWN_TABLE",
            TritmapError::RowNotFound { .. } => "ROW_NOT_FOUND",
            TritmapError::RowAlreadyDeleted { .. } => "ROW_ALREADY_DELETED",
            TritmapError::VersionOutOfOrder { .. } => "VERSION_OUT_OF_ORDER",
            TritmapError::IncompatibleIndexVersion { .. } => "INCOMPATIBLE_INDEX_VERSION",
            TritmapError::QueryParse { .. } => "QUERY_PARSE_ERROR",
            TritmapError::Config(_) => "CONFIG_ERROR",
            TritmapError::Io(_) => "IO_ERROR",
            TritmapError::Csv(_) => "CSV_ERROR",
            TritmapError::Other(_) => "OTHER_ERROR",
        }
    }
}

/// Wire representation of an error. Unknown/exceptional states are reported as
/// failures with a category — never folded into a success envelope.
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub ok: bool,
    pub error_code: &'static str,
    pub error: String,
}

impl From<&TritmapError> for ErrorBody {
    fn from(e: &TritmapError) -> Self {
        ErrorBody {
            ok: false,
            error_code: e.code(),
            error: e.to_string(),
        }
    }
}

pub type Result<T> = std::result::Result<T, TritmapError>;
