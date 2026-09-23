//! MQTT 3.1.1 会话子集 —— 纯标准库实现。
//!
//! 支持的子集（见 README §2「子集边界」）：
//! - 入站：CONNECT / PUBLISH(QoS0,QoS1) / PUBACK / SUBSCRIBE / PINGREQ / DISCONNECT
//! - 出站：CONNACK / PUBLISH(QoS0,QoS1) / PUBACK / SUBACK / PINGRESP
//!
//! 明确不支持：QoS2、遗嘱（Will）、UNSUBSCRIBE（在测试服务中返回协议错误/拒绝）。
//!
//! 所有解析均为本 crate 内从零实现的增量字节解析器（见 [`codec`]），
//! 未使用任何现成 MQTT 协议解析库。

pub mod codec;
pub mod error;
pub mod packet;
pub mod topic;

pub mod broker;
pub mod session;

pub use error::{CodecError, ConAckReason};
pub use packet::{FixedHeader, Packet, PacketType, Publish};
