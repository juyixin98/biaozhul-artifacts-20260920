//! wsframe —— WebSocket 帧重组库（RFC 6455 子集，纯标准库手写实现）。
//!
//! # 支持的子集
//! - 已完成握手之后的服务端帧处理（握手本身也提供 [`handshake`] 模块）
//! - 操作码：Continuation / Text / Binary / Close / Ping / Pong
//! - 分片消息重组（控制帧可插入分片之间）
//! - 跨分片 UTF-8 验证（text 消息与 close 原因）
//! - 掩码校验：客户端帧必须掩码，服务端帧必须不掩码
//! - 长度上限：单帧 [`frame::DEFAULT_MAX_FRAME_PAYLOAD`]，整消息可配置
//!
//! # 不支持（明确排除）
//! - 扩展（permessage-deflate 等）：RSV 位必须全 0
//! - 保留操作码
//! - 客户端角色（库可以解析服务端帧，但服务端实现只接受客户端帧）

pub mod error;
pub mod frame;
pub mod handshake;
pub mod message;
pub mod server;
pub mod utf8;

pub use error::{CloseCode, Error};
pub use frame::{encode_frame, encode_frame_masked, Frame, FrameReader, Opcode, PeerRole};
pub use message::{Assembler, Event, Message};
pub use server::{ServerConfig, WsServer};
pub use utf8::Utf8Validator;
