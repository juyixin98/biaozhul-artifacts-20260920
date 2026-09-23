//! # dns_compress
//!
//! DNS 报文（RFC 1035）解析与有限编码库，核心是压缩名字（§4.1.4）的安全增量解码。
//!
//! - [`parser`]：游标式字节解析器与所有可配置上限（[`parser::Limits`]）。
//! - [`error::DnsError`]：明确的、可枚举的错误类型。
//! - [`name::Name`]：域名类型与压缩指针解码（限跳数、检环）。
//! - [`message`]：DNS 报文结构、解析与重新编码。
//! - [`frame`]：RFC 1035 §4.2.2 的 TCP 两字节长度前缀帧。
//! - [`server`]：本地 TCP 测试服务的应答构造逻辑。

pub mod error;
pub mod frame;
pub mod message;
pub mod name;
pub mod parser;
pub mod server;

pub use error::DnsError;
pub use frame::FrameError;
pub use message::{Flags, Message, Question, Rdata, ResourceRecord};
pub use name::Name;
pub use parser::Limits;
