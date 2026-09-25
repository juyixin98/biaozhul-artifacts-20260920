//! 解析错误类型。每种非法输入对应一个**明确**的错误，便于服务端映射状态码、便于调用方区分。

use std::fmt;

/// multipart 解析过程中可能出现的全部错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// boundary 非法：为空、超过 70 字节、含非 bchar 字符（包括 CR/LF/空格）等。
    InvalidBoundary,
    /// 流没有以 `--boundary` 起始（本实现不支持 RFC 2046 preamble）。
    MissingStartBoundary,
    /// 输入在完整结束边界之前结束（`finish()` 时仍处于等待后续字节的状态）。
    Truncated,
    /// 分隔行不合法：`--boundary` 后面既不是 CRLF 也不是 `--`，
    /// 或结束边界后出现了 CRLF 之外的字节（含不支持的 transport padding / epilogue）。
    MalformedStream,
    /// 某个 part 没有头，或头块里没有合法的
    /// `Content-Disposition: form-data; name="..."`。
    MissingDisposition,
    /// 头部语法错误（非法头名、无冒号、折行、畸形参数/引号等）。
    MalformedHeaders,
    /// 单个 part 头部累计字节数超过 [`crate::Limits::max_headers_size`]。
    HeaderTooLarge,
    /// part 数量超过 [`crate::Limits::max_parts`]。
    TooManyParts,
    /// 单个 part 正文累计字节数超过 [`crate::Limits::max_part_size`]。
    PartTooLarge,
    /// 整个 multipart 流（含边界与头）累计字节数超过
    /// [`crate::Limits::max_total_size`]。
    TotalTooLarge,
}

impl Error {
    /// 机器可读的稳定短标签，服务端 JSON 与日志使用。
    pub fn tag(&self) -> &'static str {
        match self {
            Error::InvalidBoundary => "invalid_boundary",
            Error::MissingStartBoundary => "missing_start_boundary",
            Error::Truncated => "truncated",
            Error::MalformedStream => "malformed_stream",
            Error::MissingDisposition => "missing_disposition",
            Error::MalformedHeaders => "malformed_headers",
            Error::HeaderTooLarge => "header_too_large",
            Error::TooManyParts => "too_many_parts",
            Error::PartTooLarge => "part_too_large",
            Error::TotalTooLarge => "total_too_large",
        }
    }

    /// 该错误是否属于“资源超限”类（服务端映射为 HTTP 413）。
    pub fn is_size_limit(&self) -> bool {
        matches!(
            self,
            Error::HeaderTooLarge
                | Error::TooManyParts
                | Error::PartTooLarge
                | Error::TotalTooLarge
        )
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.tag())
    }
}

impl std::error::Error for Error {}
