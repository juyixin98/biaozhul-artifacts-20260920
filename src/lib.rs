//! # tls-record-observer
//!
//! 纯后端、无 I/O 依赖的 TLS 记录观察器库：增量字节解析 TLS 记录头与**明文**
//! ClientHello，跨记录重组握手消息，输出 SNI、ALPN 与未知扩展。
//!
//! * 不解密、不实现 TLS 握手、不发送任何网络数据；
//! * 核心解析仅基于 Rust `std` 手写，不使用现成 TLS 协议解析库；
//! * 明确的支持子集、长度上限（见 [`Config`]）与错误类型（见 [`ParseError`]）。
//!
//! 入口类型是 [`Observer`]：任意切分地 [`Observer::feed`] 字节，
//! 最后 [`Observer::finish`] 取结论。

pub mod client_hello;
pub mod config;
pub mod error;
pub mod grease;
pub mod json;
pub mod observer;
pub mod reader;
pub mod record;
pub mod server;
pub mod test_support;

pub use client_hello::{ClientHelloInfo, UnknownExtension};
pub use config::Config;
pub use error::ParseError;
pub use observer::{Conclusion, NoHelloReason, Observer, RecordObservation};
pub use record::RecordHeader;
