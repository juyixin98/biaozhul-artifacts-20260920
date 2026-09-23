//! Error types shared by the storage, chain and HTTP layers.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;

/// Fixed coinbase reward per block (integer atomic units).
pub const COINBASE_SUBSIDY: i64 = 5_000;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("invalid hex: {0}")]
    Hex(#[from] hex::FromHexError),

    #[error("malformed request: {0}")]
    Malformed(String),

    // ---- Block / transaction validation (HTTP 400, block rejected) ----
    #[error("height mismatch: expected {expected}, got {got}")]
    HeightMismatch { expected: i64, got: i64 },

    #[error("previous block hash mismatch")]
    PrevHashMismatch,

    #[error("block contains no transactions")]
    EmptyBlock,

    #[error("first transaction must be a coinbase (empty inputs)")]
    CoinbaseFirst,

    #[error("only the first transaction may be a coinbase")]
    MultipleCoinbase,

    #[error("unsupported block version {0} (expected 1)")]
    BadBlockVersion(i32),

    #[error("unsupported tx version {0} (expected 1)")]
    BadTxVersion(i32),

    #[error("transaction has no outputs")]
    EmptyOutputs,

    #[error("negative output amount")]
    NegativeAmount,

    #[error("output amount out of range")]
    AmountOutOfRange,

    #[error("empty address")]
    EmptyAddress,

    #[error("input references unknown or already-spent output {txid}:{vout}")]
    UnknownInput { txid: String, vout: u32 },

    #[error("input {txid}:{vout} is spent twice inside the block")]
    DoubleSpend { txid: String, vout: u32 },

    #[error("input {txid}:{vout} was created later in the same block")]
    InputInFuture { txid: String, vout: u32 },

    #[error("input sum {inputs} < output sum {outputs}")]
    InsufficientInputs { inputs: i64, outputs: i64 },

    #[error("coinbase pays {paid} but subsidy+fees is {expected}")]
    BadCoinbaseAmount { paid: i64, expected: i64 },

    #[error("transaction {0} appeared twice in the block")]
    DuplicateTx(String),

    #[error("transaction {0} already exists in the canonical chain")]
    TxAlreadyExists(String),

    #[error("txid mismatch for tx: supplied {supplied}, computed {computed}")]
    TxIdMismatch { supplied: String, computed: String },

    // ---- Chain topology ----
    #[error("no block at height {0}")]
    UnknownHeight(i64),

    #[error("outpoint {txid}:{vout} is not an unspent output at height {height}")]
    NoOutpoint {
        txid: String,
        vout: u32,
        height: i64,
    },

    #[error("cannot disconnect below genesis (height 0)")]
    CannotDisconnectGenesis,

    #[error("arithmetic overflow")]
    Overflow,

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),
}

/// Machine-readable code returned in the JSON error body.
impl Error {
    pub fn code(&self) -> &'static str {
        match self {
            Error::Sqlite(_) => "DB_ERROR",
            Error::Hex(_) | Error::Malformed(_) => "MALFORMED",
            Error::HeightMismatch { .. } => "HEIGHT_MISMATCH",
            Error::PrevHashMismatch => "PREV_HASH_MISMATCH",
            Error::EmptyBlock => "EMPTY_BLOCK",
            Error::CoinbaseFirst => "COINBASE_FIRST",
            Error::MultipleCoinbase => "MULTIPLE_COINBASE",
            Error::BadBlockVersion(_) => "BAD_BLOCK_VERSION",
            Error::BadTxVersion(_) => "BAD_TX_VERSION",
            Error::EmptyOutputs => "EMPTY_OUTPUTS",
            Error::NegativeAmount => "NEGATIVE_AMOUNT",
            Error::AmountOutOfRange => "AMOUNT_OUT_OF_RANGE",
            Error::EmptyAddress => "EMPTY_ADDRESS",
            Error::UnknownInput { .. } => "UNKNOWN_INPUT",
            Error::DoubleSpend { .. } => "DOUBLE_SPEND",
            Error::InputInFuture { .. } => "INPUT_IN_FUTURE",
            Error::InsufficientInputs { .. } => "INSUFFICIENT_INPUTS",
            Error::BadCoinbaseAmount { .. } => "BAD_COINBASE_AMOUNT",
            Error::DuplicateTx(_) => "DUPLICATE_TX",
            Error::TxAlreadyExists(_) => "TX_ALREADY_EXISTS",
            Error::TxIdMismatch { .. } => "TXID_MISMATCH",
            Error::UnknownHeight(_) => "UNKNOWN_HEIGHT",
            Error::NoOutpoint { .. } => "NO_OUTPOINT",
            Error::CannotDisconnectGenesis => "CANNOT_DISCONNECT_GENESIS",
            Error::Overflow => "OVERFLOW",
            Error::Json(_) => "MALFORMED_JSON",
        }
    }

    fn status(&self) -> StatusCode {
        match self {
            Error::UnknownHeight(_) | Error::NoOutpoint { .. } => StatusCode::NOT_FOUND,
            // Every block/tx validation error is a 400: the whole block is rejected.
            Error::Sqlite(_) => StatusCode::INTERNAL_SERVER_ERROR,
            _ => StatusCode::BAD_REQUEST,
        }
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error: &'static str,
    message: String,
}

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let body = ErrorBody {
            error: self.code(),
            message: self.to_string(),
        };
        (self.status(), Json(body)).into_response()
    }
}

pub type Result<T> = std::result::Result<T, Error>;
