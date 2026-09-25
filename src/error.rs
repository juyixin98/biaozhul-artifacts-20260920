//! 错误类型：解析错误、协议违规与 I/O 错误分开建模。

use std::fmt;

#[derive(Debug)]
pub enum MqttError {
    /// 报文字节不符合 MQTT 3.1.1 格式（含保留位/标志位非法）。
    Malformed(&'static str),
    /// 报文总长度超过配置上限。
    PacketTooLarge { max: usize, got: usize },
    /// 合法但本子集不支持的报文类型（如 PUBREC=5、UNSUBSCRIBE=10）。
    UnsupportedPacketType(u8),
    /// 不支持的 QoS 等级（本实现支持 0/1，拒绝 2）。
    UnsupportedQos(u8),
    /// 协议名或协议级别不是 MQTT 3.1.1。
    UnsupportedProtocol { name: String, level: u8 },
    /// 会话层面的协议违规（如首个报文不是 CONNECT、包 ID 为 0）。
    ProtocolViolation(&'static str),
    /// 底层 I/O 错误。
    Io(std::io::Error),
}

impl fmt::Display for MqttError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MqttError::Malformed(why) => write!(f, "malformed packet: {why}"),
            MqttError::PacketTooLarge { max, got } => {
                write!(f, "packet too large: {got} bytes (max {max})")
            }
            MqttError::UnsupportedPacketType(t) => write!(f, "unsupported packet type {t}"),
            MqttError::UnsupportedQos(q) => write!(f, "unsupported qos {q}"),
            MqttError::UnsupportedProtocol { name, level } => {
                write!(f, "unsupported protocol {name:?} level {level}")
            }
            MqttError::ProtocolViolation(why) => write!(f, "protocol violation: {why}"),
            MqttError::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for MqttError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            MqttError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for MqttError {
    fn from(e: std::io::Error) -> Self {
        MqttError::Io(e)
    }
}

impl PartialEq for MqttError {
    fn eq(&self, other: &Self) -> bool {
        use MqttError::*;
        match (self, other) {
            (Malformed(a), Malformed(b)) => a == b,
            (PacketTooLarge { max: m1, got: g1 }, PacketTooLarge { max: m2, got: g2 }) => {
                m1 == m2 && g1 == g2
            }
            (UnsupportedPacketType(a), UnsupportedPacketType(b)) => a == b,
            (UnsupportedQos(a), UnsupportedQos(b)) => a == b,
            (ProtocolViolation(a), ProtocolViolation(b)) => a == b,
            _ => false,
        }
    }
}
