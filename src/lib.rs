//! # minimultipart
//!
//! 手写的、仅依赖 `std` 的 **增量（流式）** `multipart/form-data` 解析库，
//! 外加一个用于本地验收的极简 HTTP/1.1 TCP 服务（见 [`server`] 模块 / `multipart-server` 二进制）。
//!
//! ## 支持的子集
//!
//! - Content-Type: `multipart/form-data; boundary=...`（boundary 校验遵循 RFC 2046：
//!   1..=70 个 bchar，允许带引号；空白不做边界外补白）。
//! - 每个 part 必须带 `Content-Disposition: form-data; name="..."`，可带 `filename="..."`；
//!   其余头原样收集。
//! - 正文按 RFC 2046 的分隔规则切分：`CRLF "--" boundary` 后接 CRLF（后续 part）
//!   或 `--`（结束），结束边界之后只允许可选 CRLF，不解析 epilogue/preamble。
//! - 二进制安全：正文里的 `--boundary`、`--boundary-`、`--boundary--` 若**缺少前导 CRLF**
//!   或后缀不符，一律是正文；近似边界不会误判。
//!
//! ## 流式语义
//!
//! - 任意大小的 `chunk` 都可以喂给 [`reader::MultipartReader::feed`]，边界可以跨任意输入块。
//! - 解析器**不缓存整个正文**：任何时刻内部保留的数据不超过
//!   `max(max_headers_size, delimiter_len)` 量级（见 [`reader::MultipartReader::buffered_len`]）。
//! - 超出限制立即返回对应的 [`Error`]，调用方应当中止连接。
//!
//! ```
//! use minimultipart::Limits;
//! use minimultipart::reader::MultipartReader;
//!
//! let body = b"--X\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nv\r\n--X--\r\n";
//! let mut reader = MultipartReader::new(b"X", Limits::default())?;
//! // 每次只喂 3 个字节，模拟最坏的分包
//! let mut events = Vec::new();
//! for c in body.chunks(3) {
//!     events.extend(reader.feed(c)?);
//! }
//! events.extend(reader.finish()?);
//! # Ok::<(), minimultipart::Error>(())
//! ```
#![forbid(unsafe_code)]

pub mod error;
pub mod event;
pub mod http;
pub mod limits;
pub mod mime;
pub mod reader;
pub mod server;
pub mod sha256;

pub use error::Error;
pub use event::{Event, PartMeta};
pub use limits::Limits;
pub use reader::MultipartReader;
