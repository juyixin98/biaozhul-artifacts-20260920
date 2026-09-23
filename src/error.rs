use std::fmt;

/// 统一错误类型。`Corruption` 表示磁盘字节不符合格式规范（校验和不匹配、
/// 重启点非法、键序错误等）；`Io` 表示底层 I/O 失败（含注入的故障）。
#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    Corruption(String),
    BadRequest(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

impl Error {
    pub fn corruption(msg: impl Into<String>) -> Self {
        Error::Corruption(msg.into())
    }
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Error::BadRequest(msg.into())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Corruption(m) => write!(f, "corruption: {m}"),
            Error::BadRequest(m) => write!(f, "bad request: {m}"),
        }
    }
}

impl std::error::Error for Error {}
