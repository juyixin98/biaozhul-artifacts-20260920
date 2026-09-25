//! 编解码统一错误类型。

use std::fmt;
use std::io;

/// 编解码错误。
#[derive(Debug)]
pub enum Error {
    /// 底层 I/O 错误。
    Io(io::Error),
    /// 编码输入超过 `Limits::max_input_bytes`。
    InputLimitExceeded,
    /// 编码或解码输出超过 `Limits::max_output_bytes`。
    OutputLimitExceeded,
    /// 输入耗尽后仍未解出 EOF 符号（补读零比特数超限），判定为截断流。
    TruncatedStream,
}

impl Error {
    /// 把底层 I/O 错误映射为库错误；`UnexpectedEof` 视为截断流。
    pub(crate) fn io(err: io::Error) -> Self {
        if err.kind() == io::ErrorKind::UnexpectedEof {
            Error::TruncatedStream
        } else {
            Error::Io(err)
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::InputLimitExceeded => write!(f, "input size limit exceeded"),
            Error::OutputLimitExceeded => write!(f, "output size limit exceeded"),
            Error::TruncatedStream => write!(
                f,
                "truncated stream: EOF symbol not reached before input exhausted"
            ),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(err: io::Error) -> Self {
        Error::io(err)
    }
}
