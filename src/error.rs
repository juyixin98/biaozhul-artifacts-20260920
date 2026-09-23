//! 显式错误类型：解析错误与 CONNACK 返回码。
//!
//! MQTT 3.1.1 (OASIS) §3.2.2.3 CONNACK 返回码只有 0–5。连接建立前发现的
//! 协议级错误无法用 CONNACK 表达（此时还无法确定对端是合法 MQTT 客户端），
//! 按规范 §4.13 直接关闭 TCP 连接；连接建立后的协议错误同样直接关闭连接。

use core::fmt;

/// 解析 / 编码阶段的错误类型（显式枚举，见 README §3）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodecError {
    /// 剩余字节不足，调用方应继续从 TCP 流读取字节后再喂给解析器。
    /// 这是增量解析器的正常「未就绪」状态，不属于协议错误。
    NeedMoreData,
    /// 剩余长度字段编码超过 MQTT 允许的 4 字节（§2.2.3）。
    MalformedRemainingLength,
    /// 单包剩余长度超过本实现配置的上限（默认 256 KiB）。
    PayloadTooLarge { limit: usize, actual: usize },
    /// 长度前缀声称的字节数超出剩余缓冲区。
    LengthExceedsBuffer { wanted: usize, available: usize },
    /// 报文类型不是 MQTT 3.1.1 定义的 1–15（或为保留值 0/15）。
    InvalidPacketType(u8),
    /// CONNECT 协议名不是 "MQTT"（MQTT 3.1.1 固定为 "MQTT"，4字节）。
    InvalidProtocolName,
    /// 协议级别不是 4（MQTT 3.1.1）。
    UnsupportedProtocolLevel(u8),
    /// 保留标志位取值不符合规范（如 CONNECT 保留位非 0）。
    InvalidReservedFlag(&'static str),
    /// CONNECT 报文缺少必要字段 / 固定头组合非法。
    MalformedConnect(&'static str),
    /// 其它报文的结构性错误（剩余长度不符、包标识符为 0 等）。
    MalformedPacket(&'static str),
    /// UTF-8 字符串非法。
    InvalidUtf8,
    /// QoS 值非法（>2）或出现在不允许的报文中（如 SUBSCRIBE 固定头 QoS 必须为 1）。
    InvalidQoS(u8),
    /// PUBLISH 主题为空或含非法字符（`+` / `#` 不允许出现在发布主题中）。
    InvalidTopicName,
    /// SUBSCRIBE 的过滤器列表为空。
    EmptySubscriptionList,
    /// SUBSCRIBE 过滤器非法（`#` 必须为最后一级，空过滤器等，§4.7）。
    InvalidTopicFilter,
    /// 服务质量字段与报文矛盾，例如 QoS0 PUBLISH 携带了包标识符。
    PacketIdOnQos0,
    /// 需要包标识符的报文（PUBLISH QoS>0、PUBACK、SUBSCRIBE）未提供。
    MissingPacketId,
}

impl fmt::Display for CodecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CodecError::NeedMoreData => write!(f, "need more data (incomplete packet)"),
            CodecError::MalformedRemainingLength => {
                write!(
                    f,
                    "malformed remaining length (>4 bytes or bad continuation)"
                )
            }
            CodecError::PayloadTooLarge { limit, actual } => write!(
                f,
                "packet too large: remaining length {actual} exceeds configured limit {limit}"
            ),
            CodecError::LengthExceedsBuffer { wanted, available } => write!(
                f,
                "length-prefixed field wants {wanted} bytes but only {available} available"
            ),
            CodecError::InvalidPacketType(t) => write!(f, "invalid/reserved packet type {t}"),
            CodecError::InvalidProtocolName => write!(f, "protocol name is not \"MQTT\""),
            CodecError::UnsupportedProtocolLevel(l) => {
                write!(
                    f,
                    "unsupported protocol level {l} (only MQTT 3.1.1 / level 4)"
                )
            }
            CodecError::InvalidReservedFlag(ctx) => {
                write!(f, "invalid reserved flag bit in {ctx}")
            }
            CodecError::MalformedConnect(ctx) => write!(f, "malformed CONNECT: {ctx}"),
            CodecError::MalformedPacket(ctx) => write!(f, "malformed packet: {ctx}"),
            CodecError::InvalidUtf8 => write!(f, "invalid UTF-8 string"),
            CodecError::InvalidQoS(q) => write!(f, "invalid QoS value {q}"),
            CodecError::InvalidTopicName => write!(f, "invalid topic name"),
            CodecError::EmptySubscriptionList => write!(f, "SUBSCRIBE with no topic filters"),
            CodecError::InvalidTopicFilter => write!(f, "invalid topic filter"),
            CodecError::PacketIdOnQos0 => {
                write!(f, "QoS 0 PUBLISH must not carry a packet identifier")
            }
            CodecError::MissingPacketId => write!(f, "missing packet identifier"),
        }
    }
}

impl std::error::Error for CodecError {}

/// CONNACK 返回码（MQTT 3.1.1 §3.2.2.3 Connect Return Code）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum ConAckReason {
    /// 0x00 接受连接。
    Accepted = 0x00,
    /// 0x01 服务端不支持该协议版本。
    UnacceptableProtocolVersion = 0x01,
    /// 0x02 客户端标识符不合格（为空且未使用 CleanSession）。
    IdentifierRejected = 0x02,
    /// 0x03 网络连接已建立，但 MQTT 服务不可用。
    /// 本子集中：遗嘱（Will）/QoS2 发布等不支持的能力也统一以该码拒绝。
    ServerUnavailable = 0x03,
    /// 0x04 用户名或密码的数据格式错误。
    BadUserNameOrPassword = 0x04,
    /// 0x05 未授权。
    NotAuthorized = 0x05,
}

impl ConAckReason {
    pub fn as_u8(self) -> u8 {
        self as u8
    }
}

impl fmt::Display for ConAckReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            ConAckReason::Accepted => "Connection Accepted",
            ConAckReason::UnacceptableProtocolVersion => "Unacceptable Protocol Version",
            ConAckReason::IdentifierRejected => "Client Identifier rejected",
            ConAckReason::ServerUnavailable => "Server unavailable (subset feature refused)",
            ConAckReason::BadUserNameOrPassword => "Bad User Name or Password",
            ConAckReason::NotAuthorized => "Not authorized",
        };
        write!(f, "{s}")
    }
}
