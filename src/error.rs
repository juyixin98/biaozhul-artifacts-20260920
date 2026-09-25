//! 错误类型与关闭码（RFC 6455 §7.4）。

use std::fmt;

/// WebSocket 关闭码（仅包含本实现会使用到的子集）。
///
/// `#[repr(u16)]`，可通过 [`CloseCode::code`] 取网络序值。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum CloseCode {
    /// 1000 正常关闭
    Normal = 1000,
    /// 1001 端点离开（关闭/页面跳转）
    GoingAway = 1001,
    /// 1002 协议错误
    ProtocolError = 1002,
    /// 1003 不支持的数据类型（如本端不收 binary）
    UnsupportedData = 1003,
    /// 1007 有效载荷不是合法 UTF-8
    InvalidPayloadData = 1007,
    /// 1008 策略违规（本实现未使用，保留定义）
    PolicyViolation = 1008,
    /// 1009 消息过大
    MessageTooBig = 1009,
    /// 1010 扩展协商失败（本实现未使用，保留定义）
    MissingExtension = 1010,
}

impl CloseCode {
    pub fn code(self) -> u16 {
        self as u16
    }

    /// 关闭帧正文中允许出现的码（RFC 6455 §7.4.2）。
    ///
    /// 3000-3999（私有）、4000-4999（私有）由调用方自行处理；
    /// 本服务端在 0-2999 范围内只接受 RFC 定义的标准码。
    pub fn is_allowed_on_wire(value: u16) -> bool {
        matches!(
            value,
            1000 | 1001 | 1002 | 1003 | 1007 | 1008 | 1009 | 1010 | 1011 | 1012 | 1013 | 1014
        ) || (3000..=4999).contains(&value)
    }
}

impl fmt::Display for CloseCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.code())
    }
}

/// 解析 / 重组过程中的全部错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// 帧头声称自己未掩码，但本端要求所有客户端帧必须掩码（RFC 6455 §5.1）。
    UnmaskedClientFrame,
    /// 服务端发来的帧反而带掩码（§5.1 要求服务端帧必须不掩码）。
    MaskedServerFrame,
    /// RSV1/2/3 中存在非 0 位，但本端没有协商任何扩展。
    RsvBitsSet,
    /// 操作码保留 / 未定义（3、7、B、C-F）。
    ReservedOpcode(u8),
    /// 控制帧（Close/Ping/Pong）的 FIN 位为 0：控制帧不允许分片（§5.5）。
    FragmentedControlFrame,
    /// 控制帧有效载荷长度超过 125 字节（§5.5）。
    ControlFrameTooLong,
    /// 单帧有效载荷超过配置上限。
    FrameTooLarge(usize),
    /// 重组后的消息超过配置上限。
    MessageTooLarge,
    /// 在没有未结束分片时收到 continuation 帧（§5.4）。
    UnexpectedContinuation,
    /// 已存在未结束的数据分片时，又收到一个新的数据起始帧（text/binary）。
    NestedMessageStart,
    /// UTF-8 字节序列非法（可能跨越多个分片）。
    InvalidUtf8,
    /// Close 帧正文格式非法（长度为 1，或前两字节不是合法关闭码）。
    InvalidCloseFrame,
    /// 对端在消息分片尚未结束时发起关闭握手（§5.5.1：服务端应先回 Close）。
    ClosingDuringFragment,
    /// 连接被对端关闭 / 流结束。
    ConnectionClosed,
    /// 底层 I/O 错误（仅服务端使用）。
    Io(String),
}

impl Error {
    /// 该错误对应的 WebSocket 关闭码；`None` 表示不产生关闭帧（如连接已断）。
    pub fn close_code(&self) -> Option<CloseCode> {
        match self {
            Error::MessageTooLarge | Error::FrameTooLarge(_) => Some(CloseCode::MessageTooBig),
            Error::InvalidUtf8 => Some(CloseCode::InvalidPayloadData),
            Error::ConnectionClosed | Error::Io(_) => None,
            _ => Some(CloseCode::ProtocolError),
        }
    }

    /// 是否属于"协议违规"类错误（收到后必须发 Close 帧）。
    pub fn is_protocol_violation(&self) -> bool {
        matches!(
            self,
            Error::UnmaskedClientFrame
                | Error::MaskedServerFrame
                | Error::RsvBitsSet
                | Error::ReservedOpcode(_)
                | Error::FragmentedControlFrame
                | Error::ControlFrameTooLong
                | Error::FrameTooLarge(_)
                | Error::UnexpectedContinuation
                | Error::NestedMessageStart
                | Error::InvalidCloseFrame
        )
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::UnmaskedClientFrame => write!(f, "client frame is not masked (RFC 6455 5.1)"),
            Error::MaskedServerFrame => write!(f, "server frame must not be masked (RFC 6455 5.1)"),
            Error::RsvBitsSet => write!(f, "RSV bits set without negotiated extension"),
            Error::ReservedOpcode(op) => write!(f, "reserved opcode {op:#x}"),
            Error::FragmentedControlFrame => {
                write!(f, "control frame must not be fragmented (FIN=0)")
            }
            Error::ControlFrameTooLong => write!(f, "control frame payload exceeds 125 bytes"),
            Error::FrameTooLarge(n) => {
                write!(f, "frame payload of {n} bytes exceeds configured limit")
            }
            Error::MessageTooLarge => write!(f, "reassembled message exceeds configured limit"),
            Error::UnexpectedContinuation => {
                write!(f, "continuation frame without a started message")
            }
            Error::NestedMessageStart => {
                write!(
                    f,
                    "new data frame while previous fragmented message is unfinished"
                )
            }
            Error::InvalidUtf8 => write!(f, "payload is not valid UTF-8 (may span fragments)"),
            Error::InvalidCloseFrame => write!(f, "malformed close frame body"),
            Error::ClosingDuringFragment => {
                write!(f, "close initiated while a fragmented message is open")
            }
            Error::ConnectionClosed => write!(f, "connection closed by peer"),
            Error::Io(s) => write!(f, "I/O error: {s}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e.to_string())
    }
}
