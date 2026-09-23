//! 错误类型定义。
//!
//! 本项目明确区分两类错误：
//!
//! * [`DecodeError`]：客户端侧**增量解码**时的错误（长度上限、UTF-8、毒化状态等）。
//! * [`EncodeError`]：服务端侧**帧编码**时的错误（字段值里出现非法换行符）。
//!
//! 这两种错误都实现了 [`std::error::Error`]，可以通过 `?` 在 `fn ... -> Result`
//! 的函数中传播。

use std::error::Error;
use std::fmt;

/// 单次解码可配置的长度上限（单位：字节）。
///
/// 所有上限都针对**单个事件 / 单行**，而不是整个 TCP 流；这样一个超限的坏事件
/// 不会拖垮长期运行的连接（一旦触发错误，解码器进入毒化状态，必须重建）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 一行（含行首字段名，不含行尾 CR/LF）允许的最大字节数。
    ///
    /// 规范本身对行长没有限制，但真实服务必须设防：一个永远不发换行的对端
    /// 会让无限制的解码器把无限多字节缓存在内存里。
    pub max_line_bytes: usize,
    /// 单个事件 `data` 拼接后的最大字节数（含多行之间规范要求插入的 `\n`）。
    pub max_data_bytes: usize,
    /// `id` 字段值的最大字节数。
    pub max_id_bytes: usize,
}

impl Default for Limits {
    /// 默认上限：行 8 KiB、data 64 KiB、id 256 B。
    ///
    /// 取值思路：WHATWG 规范中浏览器把“一行”放进 buffer 没有上限，但服务端推送
    /// 网关（如 Nginx `sub_filter` / EventSource 代理）通常按 8 KiB 左右切块；
    /// 8 KiB 行 / 64 KiB 事件是一个偏保守、足以暴露问题的默认值。
    fn default() -> Self {
        Self {
            max_line_bytes: 8 * 1024,
            max_data_bytes: 64 * 1024,
            max_id_bytes: 256,
        }
    }
}

impl Limits {
    /// 一组故意很小的上限，仅供单元测试/集成测试构造“超长”场景使用。
    pub fn for_test() -> Self {
        Self {
            max_line_bytes: 8,
            max_data_bytes: 16,
            max_id_bytes: 4,
        }
    }
}

/// 增量解码错误。
///
/// 变体即“明确支持的错误子集”——规范中大量“忽略该行”的情况**不**算错误
/// （例如未知字段、非法 retry、含 NUL 的 id），只在真正违反长度/编码约束时报错。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodeError {
    /// 一行（不含行终止符）超过 [`Limits::max_line_bytes`]。
    LineTooLong {
        /// 触发上限时已累计的字节数（必定 > limit）。
        len: usize,
        /// 当时生效的上限。
        limit: usize,
    },
    /// 单个事件 `data` 累积值超过 [`Limits::max_data_bytes`]。
    DataTooLong {
        /// 触发上限时 data 的字节数（含中间插入的 `\n`）。
        len: usize,
        /// 当时生效的上限。
        limit: usize,
    },
    /// `id` 值超过 [`Limits::max_id_bytes`]。
    IdTooLong {
        /// 实际 id 字节数。
        len: usize,
        /// 当时生效的上限。
        limit: usize,
    },
    /// 字段值（或整行）不是合法 UTF-8。
    ///
    /// SSE 规范基于“以 UTF-8 解码字节流”定义；我们按字节做行切分，只在需要把
    /// 字段值交给调用方（event / data / id / retry）时校验 UTF-8。
    InvalidUtf8 {
        /// 触发错误的字段名（`"data"` / `"event"` / `"id"` / `"retry"`）。
        field: &'static str,
    },
    /// 解码器已因前一个错误进入毒化状态，之后再喂任何字节都会返回本错误。
    ///
    /// 续传场景下正确的处理是：断开这条连接、重新建链，并带上最后已知的
    /// `Last-Event-ID`；毒化保证调用方不会“半截事件”混进正常事件流。
    Poisoned,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecodeError::LineTooLong { len, limit } => write!(
                f,
                "SSE 行长度 {len} 字节超过上限 {limit} 字节"
            ),
            DecodeError::DataTooLong { len, limit } => write!(
                f,
                "单个事件 data 长度 {len} 字节超过上限 {limit} 字节"
            ),
            DecodeError::IdTooLong { len, limit } => {
                write!(f, "id 长度 {len} 字节超过上限 {limit} 字节")
            }
            DecodeError::InvalidUtf8 { field } => {
                write!(f, "字段 `{field}` 的值不是合法 UTF-8")
            }
            DecodeError::Poisoned => {
                write!(f, "解码器已因先前的错误进入毒化状态，请重建连接")
            }
        }
    }
}

impl Error for DecodeError {}

/// 帧编码错误（服务端侧）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EncodeError {
    /// 字段值中出现裸 CR 或 LF。
    ///
    /// SSE 以换行分隔字段；如果允许 data/id/event 内含裸换行会导致帧注入
    /// （一个订阅者 A 的消息伪造出 `id:` / `event:` 行，影响共享解析器的下游）。
    /// 多行 data 必须通过传入多个 data 值（或使用本库的多行编码接口）表达。
    ValueContainsNewline {
        /// 出问题的字段名。
        field: &'static str,
    },
    /// 输出缓冲区的写入失败（通常是 `Vec<u8>` 不会触发，`io::Write` 时才可能）。
    Io(String),
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::ValueContainsNewline { field } => {
                write!(f, "字段 `{field}` 的值包含裸 CR/LF，需用多行 data 表达")
            }
            EncodeError::Io(e) => write!(f, "写入输出流失败: {e}"),
        }
    }
}

impl Error for EncodeError {}

impl From<std::io::Error> for EncodeError {
    fn from(e: std::io::Error) -> Self {
        EncodeError::Io(e.to_string())
    }
}

impl EncodeError {
    /// 便于在返回 `io::Result` 的服务端代码中用 `.map_err(EncodeError::io)?`。
    pub fn into_io(self) -> std::io::Error {
        std::io::Error::new(std::io::ErrorKind::InvalidData, self.to_string())
    }
}
