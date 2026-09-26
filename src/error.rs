//! 统一错误类型。编解码与 JSON 控制入口共用。

use std::fmt;

/// 编解码及控制入口可能出现的全部错误。
#[derive(Debug)]
pub enum Error {
    /// 头部魔数不是 "LZSW"。
    InvalidMagic,
    /// 不支持的格式版本。
    UnsupportedVersion(u8),
    /// 头部字段非法（flags/reserved 非零、window 为 0、max_match 不符）。
    InvalidHeader(&'static str),
    /// 编码器窗口配置越界。
    InvalidWindow(usize),
    /// 匹配回距为 0。
    DistanceZero,
    /// 匹配回距超过头部声明的窗口大小。
    DistanceBeyondWindow { distance: usize, window: usize },
    /// 匹配回距越过已有输出起点（引用尚未产生的字节）。
    DistanceBeforeOutput { distance: usize, emitted: u64 },
    /// 解码输出超出预算（压缩炸弹防护）。
    OutputLimitExceeded { limit: u64 },
    /// 输入在 token 中途结束（截断）。
    Truncated,
    /// 编码器已 finish 后仍继续喂数据。
    EncoderFinished,
    /// JSON 解析错误。
    Json(String),
    /// Base64 解码错误。
    Base64(String),
    /// 请求缺少必填字段。
    MissingField(&'static str),
    /// 请求字段类型或取值非法。
    InvalidField(&'static str),
    /// 未知操作。
    UnknownOp(String),
    /// IO 错误。
    Io(std::io::Error),
}

impl Error {
    /// 稳定的机器可读错误类别，用于 JSON 响应的 `error_kind` 字段。
    pub fn kind(&self) -> &'static str {
        match self {
            Error::InvalidMagic => "invalid_magic",
            Error::UnsupportedVersion(_) => "unsupported_version",
            Error::InvalidHeader(_) => "invalid_header",
            Error::InvalidWindow(_) => "invalid_window",
            Error::DistanceZero => "distance_zero",
            Error::DistanceBeyondWindow { .. } => "distance_beyond_window",
            Error::DistanceBeforeOutput { .. } => "distance_before_output",
            Error::OutputLimitExceeded { .. } => "output_limit_exceeded",
            Error::Truncated => "truncated",
            Error::EncoderFinished => "encoder_finished",
            Error::Json(_) => "json_error",
            Error::Base64(_) => "base64_error",
            Error::MissingField(_) => "missing_field",
            Error::InvalidField(_) => "invalid_field",
            Error::UnknownOp(_) => "unknown_op",
            Error::Io(_) => "io_error",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidMagic => write!(f, "invalid magic: expected \"LZSW\""),
            Error::UnsupportedVersion(v) => write!(f, "unsupported format version: {v}"),
            Error::InvalidHeader(why) => write!(f, "invalid header: {why}"),
            Error::InvalidWindow(w) => {
                write!(f, "invalid window size {w}: must be in 1..=65535")
            }
            Error::DistanceZero => write!(f, "match distance must be >= 1"),
            Error::DistanceBeyondWindow { distance, window } => write!(
                f,
                "match distance {distance} exceeds declared window {window}"
            ),
            Error::DistanceBeforeOutput { distance, emitted } => write!(
                f,
                "match distance {distance} reaches before output start (only {emitted} bytes emitted)"
            ),
            Error::OutputLimitExceeded { limit } => {
                write!(f, "decoded output exceeds budget of {limit} bytes")
            }
            Error::Truncated => write!(f, "input truncated in the middle of a token"),
            Error::EncoderFinished => write!(f, "encoder already finished"),
            Error::Json(msg) => write!(f, "json error: {msg}"),
            Error::Base64(msg) => write!(f, "base64 error: {msg}"),
            Error::MissingField(name) => write!(f, "missing required field: {name}"),
            Error::InvalidField(name) => write!(f, "invalid field: {name}"),
            Error::UnknownOp(op) => write!(f, "unknown op: {op}"),
            Error::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
