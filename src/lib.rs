//! mqtt-subset: MQTT 3.1.1 协议子集的纯后端实现。
//!
//! 明确支持的子集：
//! - CONNECT / CONNACK（含 clean session、will 解析与异常断连投递）
//! - SUBSCRIBE / SUBACK（请求的 QoS2 会被降级授予 QoS1）
//! - PUBLISH（QoS0 与 QoS1）/ PUBACK
//! - PINGREQ / PINGRESP、DISCONNECT
//!
//! 明确不支持：QoS2 流程（PUBREC/PUBREL/PUBCOMP）、UNSUBSCRIBE、
//! MQTT 5、WebSocket 传输、认证鉴权。
//!
//! 语义声明：本库实现的是 **至少一次（at-least-once）** 投递，
//! 不宣称业务层面的恰好一次（exactly-once）。

pub mod broker;
pub mod codec;
pub mod error;
pub mod packet;
pub mod server;
pub mod topic;

pub use codec::{Decoder, DEFAULT_MAX_PACKET_SIZE, MQTT_MAX_PACKET_SIZE};
pub use error::MqttError;
pub use packet::{Connect, Packet, Publish, Subscribe};
