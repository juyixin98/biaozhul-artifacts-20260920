//! 错误类型与 RFC 6455 第 7.4.1 节定义的状态码。

use std::fmt;

/// RFC 6455 §7.4 关闭码常量。
pub mod close_code {
    /// 正常关闭。
    pub const NORMAL: u16 = 1000;
    /// 端点“离开”（如端点被关闭）。
    pub const GOING_AWAY: u16 = 1001;
    /// 因协议错误终止连接。
    pub const PROTOCOL_ERROR: u16 = 1002;
    /// 收到不支持的数据类型。
    pub const UNSUPPORTED_DATA: u16 = 1003;
    /// 收到的数据在消息内不一致（如文本消息含非法 UTF-8）。
    pub const INVALID_PAYLOAD_DATA: u16 = 1007;
    /// 违反策略（通用，找不到更合适的码时）。
    pub const POLICY_VIOLATION: u16 = 1008;
    /// 消息过大，无法处理。
    pub const MESSAGE_TOO_BIG: u16 = 1009;
    /// 缺少扩展（本服务不协商扩展，不会主动发送）。
    pub const MANDATORY_EXT: u16 = 1010;
    /// 服务器遇到意外情况无法完成请求。
    pub const INTERNAL_ERROR: u16 = 1011;
}

/// 解析 / 重组过程中可能出现的错误。
///
/// 所有错误均为**致命**错误：RFC 6455 要求服务端发送对应关闭帧后断开 TCP。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WsError {
    /// 目前喂入的字节还不足以构成完整帧头/载荷；继续喂即可。
    Incomplete,
    /// RSV1/RSV2/RSV3 非零，且没有协商扩展定义其含义。
    RsvBitsSet,
    /// 服务端收到未掩码的客户端帧（RFC 6455 §5.1：客户端→服务端必须掩码）。
    FrameNotMasked,
    /// 单帧 payload 超过配置的 `max_frame_payload`。
    FrameTooLarge(u64),
    /// 重组后的消息超过配置的 `max_message_size`。
    MessageTooLarge(usize),
    /// 控制帧（0x8–0xF）载荷长度超过 125 字节。
    ControlFrameTooLong(u64),
    /// 控制帧 FIN=0（控制帧不允许分片，§5.5）。
    ControlFrameFragmented,
    /// 出现了 3..=7（保留数据）或 0xB..=0xF（保留控制）等未知操作码。
    UnknownOpcode(u8),
    /// 分片序列非法：
    /// - 连接开始就是 continuation；
    /// - 已有未结束的分片消息，却又来一个 fin 或非 fin 的 text/binary（start）；
    /// - continuation 到达时并没有分片消息在进行。
    IllegalFragmentation,
    /// 文本消息（含跨分片）不是合法 UTF-8。
    InvalidUtf8,
    /// Close 帧长度为 1（有半个状态码），或状态码非法，或关闭原因不是 UTF-8。
    InvalidCloseFrame,
    /// 已经收到/发送过 Close 帧（连接处于 CLOSING/CLOSED），又收到帧。
    ConnectionClosed,
    /// 载荷标称 64 位扩展长度，但本平台 usize 容纳不下（理论上 64 位平台不会发生）。
    LengthExceedsPlatform,
}

/// 库内部结果类型。
pub type WsResult<T> = Result<T, WsError>;

impl WsError {
    /// 该错误对应的 RFC 6455 关闭码与关闭原因。
    ///
    /// 返回 `None` 表示这不是协议级致命错误（目前只有 [`WsError::Incomplete`]），
    /// 不应导致关闭。
    pub fn close_code(&self) -> Option<(u16, &'static str)> {
        use close_code::*;
        Some(match self {
            WsError::Incomplete => return None,
            WsError::RsvBitsSet
            | WsError::ControlFrameFragmented
            | WsError::UnknownOpcode(_)
            | WsError::IllegalFragmentation
            | WsError::ConnectionClosed => (PROTOCOL_ERROR, "protocol error"),
            WsError::FrameNotMasked | WsError::InvalidCloseFrame => {
                (PROTOCOL_ERROR, "protocol error")
            }
            WsError::ControlFrameTooLong(_) => (PROTOCOL_ERROR, "control frame too long"),
            WsError::FrameTooLarge(_) | WsError::MessageTooLarge(_) => {
                (MESSAGE_TOO_BIG, "message too big")
            }
            WsError::InvalidUtf8 => (INVALID_PAYLOAD_DATA, "invalid utf-8 payload"),
            WsError::LengthExceedsPlatform => (MESSAGE_TOO_BIG, "length exceeds platform"),
        })
    }
}

impl fmt::Display for WsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WsError::Incomplete => write!(f, "need more bytes to complete a frame"),
            WsError::RsvBitsSet => write!(f, "RSV bits must be zero without negotiated extension"),
            WsError::FrameNotMasked => write!(f, "client frames sent to a server must be masked"),
            WsError::FrameTooLarge(n) => write!(f, "frame payload of {n} bytes exceeds limit"),
            WsError::MessageTooLarge(n) => write!(f, "reassembled message of {n} bytes exceeds limit"),
            WsError::ControlFrameTooLong(n) => {
                write!(f, "control frame payload is {n} bytes, must be <= 125")
            }
            WsError::ControlFrameFragmented => write!(f, "control frames must not be fragmented"),
            WsError::UnknownOpcode(op) => write!(f, "reserved/unknown opcode 0x{op:X}"),
            WsError::IllegalFragmentation => write!(f, "illegal frame fragmentation sequence"),
            WsError::InvalidUtf8 => write!(f, "text message is not valid UTF-8"),
            WsError::InvalidCloseFrame => write!(f, "malformed close frame (code/reason)"),
            WsError::ConnectionClosed => write!(f, "data after close handshake completed"),
            WsError::LengthExceedsPlatform => write!(f, "64-bit payload length exceeds platform usize"),
        }
    }
}

impl std::error::Error for WsError {}

/// 判断 RFC 6455 §7.4.2 意义上的关闭码是否合法。
///
/// 合法集合：1000–1003、1007–1011、1015 以下已注册码中本实现认可的部分，
/// 以及 3000–4999 的私有码。1004/1005/1006 是**保留**码，绝不能出现在线上帧里；
/// 1012–1014、1015 虽已注册但语义属于 WebSocket 层之外（TLS 等），同样不接受。
pub fn is_valid_close_code(code: u16) -> bool {
    matches!(
        code,
        1000..=1003
        | 1007..=1011
        | 3000..=4999
    )
}
