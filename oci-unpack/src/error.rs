//! Error type shared by the unpack pipeline and the HTTP layer.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use std::fmt;

/// All failure modes for fixture loading, layer verification, application and
/// publishing. Each variant carries enough detail for an honest error report.
#[derive(Debug)]
pub enum Error {
    NotFound(String),
    BadName(String),
    InvalidImage(String),
    BadRequest(String),
    Io(String),
    Tar {
        context: String,
        source: String,
    },
    Gzip {
        context: String,
        source: String,
    },
    DigestMismatch {
        layer_index: usize,
        expected: String,
        actual: String,
    },
    /// Only `sha256:<hex>` descriptors are accepted.
    UnsupportedDigest(String),
    PathTraversal(String),
    LinkEscape {
        link: String,
        target: String,
    },
    AncestorIsSymlink(String),
    /// char/block devices, fifos, sockets, setuid...
    UnsupportedEntryType(String),
    CorruptArchive(String),
    ResourceLimitExceeded(String),
    Conflict(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::NotFound(n) => write!(f, "fixture not found: {n}"),
            Error::BadName(n) => write!(f, "invalid fixture name: {n:?}"),
            Error::InvalidImage(m) => write!(f, "invalid OCI image: {m}"),
            Error::BadRequest(m) => write!(f, "invalid request: {m}"),
            Error::Io(m) => write!(f, "io error: {m}"),
            Error::Tar { context, source } => write!(f, "tar decode error in {context}: {source}"),
            Error::Gzip { context, source } => {
                write!(f, "gzip decode error in {context}: {source}")
            }
            Error::DigestMismatch {
                layer_index,
                expected,
                actual,
            } => write!(
                f,
                "digest mismatch in layer #{layer_index}: expected {expected}, computed {actual}"
            ),
            Error::UnsupportedDigest(d) => {
                write!(
                    f,
                    "unsupported digest algorithm: {d} (only sha256 is accepted)"
                )
            }
            Error::PathTraversal(p) => write!(f, "path traversal rejected: {p:?}"),
            Error::LinkEscape { link, target } => {
                write!(
                    f,
                    "symlink target escapes root: link={link:?} target={target:?}"
                )
            }
            Error::AncestorIsSymlink(p) => {
                write!(f, "entry rejected because an ancestor is a symlink: {p:?}")
            }
            Error::UnsupportedEntryType(p) => {
                write!(
                    f,
                    "forbidden entry type (device/fifo/socket/long-link): {p:?}"
                )
            }
            Error::CorruptArchive(m) => write!(f, "corrupt or truncated archive: {m}"),
            Error::ResourceLimitExceeded(m) => write!(f, "resource limit exceeded: {m}"),
            Error::Conflict(p) => write!(f, "conflicting filesystem entry at {p:?}"),
        }
    }
}

impl std::error::Error for Error {}

impl Error {
    pub fn io(e: impl std::fmt::Display) -> Self {
        Error::Io(e.to_string())
    }

    pub fn tar(context: impl Into<String>, source: impl std::fmt::Display) -> Self {
        Error::Tar {
            context: context.into(),
            source: source.to_string(),
        }
    }

    pub fn gzip(context: impl Into<String>, source: impl std::fmt::Display) -> Self {
        Error::Gzip {
            context: context.into(),
            source: source.to_string(),
        }
    }

    pub fn limit(msg: impl Into<String>) -> Self {
        Error::ResourceLimitExceeded(msg.into())
    }

    pub fn corrupt(msg: impl Into<String>) -> Self {
        Error::CorruptArchive(msg.into())
    }

    /// HTTP status mapping. Any failure during verification/application is a
    /// 422 so clients can distinguish "image rejected" from transport errors.
    pub fn status(&self) -> StatusCode {
        match self {
            Error::NotFound(_) => StatusCode::NOT_FOUND,
            Error::BadName(_) | Error::BadRequest(_) => StatusCode::BAD_REQUEST,
            _ => StatusCode::UNPROCESSABLE_ENTITY,
        }
    }

    pub fn code(&self) -> &'static str {
        match self {
            Error::NotFound(_) => "not_found",
            Error::BadName(_) => "bad_name",
            Error::InvalidImage(_) => "invalid_image",
            Error::BadRequest(_) => "bad_request",
            Error::Io(_) => "io_error",
            Error::Tar { .. } => "tar_error",
            Error::Gzip { .. } => "gzip_error",
            Error::DigestMismatch { .. } => "digest_mismatch",
            Error::UnsupportedDigest(_) => "unsupported_digest",
            Error::PathTraversal(_) => "path_traversal",
            Error::LinkEscape { .. } => "link_escape",
            Error::AncestorIsSymlink(_) => "ancestor_symlink",
            Error::UnsupportedEntryType(_) => "unsafe_entry_type",
            Error::CorruptArchive(_) => "corrupt_archive",
            Error::ResourceLimitExceeded(_) => "limit_exceeded",
            Error::Conflict(_) => "conflict",
        }
    }
}

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let status = self.status();
        let body = Json(json!({
            "error": {
                "code": self.code(),
                "message": self.to_string(),
            }
        }));
        (status, body).into_response()
    }
}

pub type Result<T> = std::result::Result<T, Error>;
