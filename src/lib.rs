//! # dns-compress
//!
//! 纯标准库实现的 DNS 压缩名字**增量字节解析**库，附带本地 TCP 测试服务。
//!
//! - [`error::DnsError`]：明确枚举的错误类型（截断、越界标签、指针越界、
//!   指针环、跳转超限、不支持标签类型等）。
//! - [`reader::Reader`]：游标式增量字节读取器（大端整数/切片/受限子区间）。
//! - [`name::Name`]：域名表示与压缩指针解压（跳转上限 [`name::MAX_POINTER_JUMPS`]、
//!   环检测、允许前向指针、总长 255 上限），以及带压缩的编码。
//! - [`message::Message`]：DNS 报文读取与**有限**响应编码；
//!   RDATA 仅结构化支持 A/AAAA/CNAME，未知类型原样保留字节。
//!
//! 本 crate 不依赖、也不包装任何现成 DNS 协议解析器，核心解析全部手写。

pub mod error;
pub mod message;
pub mod name;
pub mod reader;
pub mod server;

pub use error::{DnsError, Result};
pub use message::{
    Header, Message, Question, Rdata, Record, CLASS_IN, RCODE_FORMERR, RCODE_NOERROR,
    RCODE_NXDOMAIN, RCODE_REFUSED, RCODE_SERVFAIL, TYPE_A, TYPE_AAAA, TYPE_CNAME,
};
pub use name::{Name, NameTable, MAX_LABEL_LEN, MAX_NAME_WIRE_LEN, MAX_POINTER_JUMPS};
pub use reader::Reader;
