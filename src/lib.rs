//! # wsframe
//!
//! 一个**从零手写**的 WebSocket（RFC 6455）增量字节解析库，不含任何第三方依赖，
//! 也没有使用现成的 WebSocket 协议解析器：帧解析、掩码校验、控制帧/分片状态机、
//! 跨分片 UTF-8 校验、握手所需的 SHA-1 + Base64 均为自行实现。
//!
//! 模块构成：
//! - [`error`]：错误类型与 RFC 6455 关闭码映射
//! - [`frame`]：帧头解析、掩码去除、服务端帧编码
//! - [`utf8`]：可跨分片续写的增量 UTF-8 校验器
//! - [`reassemble`]：逐字节喂入的帧/消息重组状态机（核心）
//! - [`sha1`]：FIPS 180-4 SHA-1（仅用于握手）
//! - [`handshake`]：HTTP 升级握手（请求样例见 `samples/handshake_request.txt`）
//! - [`server`]：本地 TCP 回声测试服务（`wsecho` 二进制）

pub mod error;
pub mod frame;
pub mod handshake;
pub mod reassemble;
pub mod server;
pub mod sha1;
pub mod utf8;

pub use error::{WsError, WsResult};
pub use frame::{parse_header, FrameHeader, Opcode, Limits};
pub use reassemble::{Event, Reassembler};
