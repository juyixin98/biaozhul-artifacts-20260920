//! # sse-resume
//!
//! 纯标准库实现的 SSE（Server-Sent Events）**增量字节解析库**与**断线续流
//! 测试服务**。核心解析器为手写状态机（见 [`decode`] 模块文档），没有使用任何
//! 现成的完整协议解析器。
//!
//! 模块分工：
//!
//! * [`decode`] — 客户端侧：字节块 → [`decode::Event`] 的增量解析。
//! * [`encode`] — 服务端侧：[`encode::OutEvent`] → SSE 帧字节。
//! * [`server`] — 本地 TCP 测试服务：有限历史、`Last-Event-ID` 续流、
//!   过期/未知游标的明确重置事件。
//! * [`error`] — 明确支持的错误类型与长度上限配置。

#![forbid(unsafe_code)]

pub mod decode;
pub mod encode;
pub mod error;
pub mod server;

pub use decode::{Decoder, Event};
pub use encode::OutEvent;
pub use error::{DecodeError, EncodeError, Limits};
