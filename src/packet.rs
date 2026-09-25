//! 报文类型定义（仅子集）。

/// CONNECT 报文体。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Connect {
    pub client_id: String,
    pub clean_session: bool,
    pub keep_alive: u16,
    /// 解析出的遗嘱（异常断连时由 broker 投递）。
    pub will: Option<Publish>,
    /// 解析但不做鉴权，仅透传记录。
    pub username: Option<String>,
    pub password: Option<Vec<u8>>,
}

/// CONNACK。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnAck {
    pub session_present: bool,
    /// 0 = accepted；非 0 为拒绝码（见 MQTT 3.1.1 表 3.1）。
    pub return_code: u8,
}

/// PUBLISH（QoS0/QoS1）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Publish {
    pub dup: bool,
    pub qos: u8,
    pub retain: bool,
    pub topic: String,
    /// QoS1 时必须有；QoS0 时为 None。
    pub packet_id: Option<u16>,
    pub payload: Vec<u8>,
}

impl Publish {
    pub fn new(topic: &str, qos: u8, payload: impl Into<Vec<u8>>) -> Self {
        Publish {
            dup: false,
            qos,
            retain: false,
            topic: topic.to_string(),
            packet_id: None,
            payload: payload.into(),
        }
    }
}

/// SUBSCRIBE。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subscribe {
    pub packet_id: u16,
    /// (主题过滤器, 请求的 QoS)
    pub topics: Vec<(String, u8)>,
}

/// SUBACK。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubAck {
    pub packet_id: u16,
    /// 每个过滤器被授予的 QoS（0x80 表示失败）。
    pub granted: Vec<u8>,
}

/// 子集内的报文枚举。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Packet {
    Connect(Connect),
    ConnAck(ConnAck),
    Publish(Publish),
    PubAck(u16),
    Subscribe(Subscribe),
    SubAck(SubAck),
    PingReq,
    PingResp,
    Disconnect,
}
