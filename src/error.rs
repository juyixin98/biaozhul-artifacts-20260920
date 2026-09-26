use std::fmt;

/// Error type carrying a stable machine-readable `code`.
#[derive(Debug)]
pub enum IfixError {
    /// Underlying I/O failure.
    Io(std::io::Error),
    /// Malformed JSON control request or tree input.
    Json(String),
    /// Structural violation of the IFIX format or an exceeded safety limit.
    Format { code: &'static str, message: String },
    /// Requested path does not exist in the index.
    NotFound { path: String },
}

impl IfixError {
    pub fn format(code: &'static str, message: impl Into<String>) -> Self {
        IfixError::Format {
            code,
            message: message.into(),
        }
    }

    pub fn code(&self) -> &'static str {
        match self {
            IfixError::Io(_) => "IO",
            IfixError::Json(_) => "JSON",
            IfixError::Format { code, .. } => code,
            IfixError::NotFound { .. } => "NOT_FOUND",
        }
    }
}

impl fmt::Display for IfixError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IfixError::Io(e) => write!(f, "I/O error: {e}"),
            IfixError::Json(m) => write!(f, "JSON error: {m}"),
            IfixError::Format { code, message } => write!(f, "{code}: {message}"),
            IfixError::NotFound { path } => write!(f, "path not found: {path}"),
        }
    }
}

impl std::error::Error for IfixError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            IfixError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for IfixError {
    fn from(e: std::io::Error) -> Self {
        IfixError::Io(e)
    }
}

impl From<serde_json::Error> for IfixError {
    fn from(e: serde_json::Error) -> Self {
        IfixError::Json(e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, IfixError>;
