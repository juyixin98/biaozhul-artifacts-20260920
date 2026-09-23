//! MQTT 3.1.1 控制报文的数据模型（仅本子集需要的字段）。

/// MQTT 控制报文类型（固定头高 4 位，MQTT 3.1.1 §2.2.1）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum PacketType {
    Connect = 1,
    Connack = 2,
    Publish = 3,
    Puback = 4,
    Subscribe = 8,
    Suback = 9,
    Pingreq = 12,
    Pingresp = 13,
    Disconnect = 14,
}

impl PacketType {
    pub fn as_u8(self) -> u8 {
        self as u8
    }

    /// 仅解析本子集接受的报文类型；保留值 0、15 与未实现类型返回错误。
    pub fn from_u8(value: u8) -> Option<PacketType> {
        Some(match value {
            1 => PacketType::Connect,
            2 => PacketType::Connack,
            3 => PacketType::Publish,
            4 => PacketType::Puback,
            8 => PacketType::Subscribe,
            9 => PacketType::Suback,
            12 => PacketType::Pingreq,
            13 => PacketType::Pingresp,
            14 => PacketType::Disconnect,
            _ => return None,
        })
    }
}

/// 固定头（MQTT 3.1.1 §2.2）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FixedHeader {
    pub packet_type: PacketType,
    /// 低 4 位标志位（按报文类型有不同含义）。
    pub flags: u8,
    /// 剩余长度（已解码，不含固定头自身）。
    pub remaining_length: usize,
}

impl FixedHeader {
    /// PUBLISH DUP 位（bit3）。
    pub fn dup(&self) -> bool {
        self.flags & 0b1000 != 0
    }
    /// PUBLISH QoS（bit2-1）。
    pub fn qos(&self) -> u8 {
        (self.flags >> 1) & 0b11
    }
    /// PUBLISH RETAIN 位（bit0）。
    pub fn retain(&self) -> bool {
        self.flags & 0b0001 != 0
    }
}

/// CONNECT 报文（§3.1）。本子集拒绝 Will，但保留解析出的字段以便给出明确错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Connect {
    pub clean_session: bool,
    /// Keep Alive 秒（§3.1.2.10）；0 表示关闭保活机制。
    pub keep_alive_secs: u16,
    pub client_id: String,
    pub username: Option<String>,
    pub password: Option<Vec<u8>>,
    /// Will 标志 —— 子集不支持，服务端以 CONNACK 0x03 拒绝。
    pub will_flag: bool,
    pub will_retain: bool,
    pub will_qos: u8,
}

/// 解码后的控制报文（入站方向）。
///
/// 增量解析器在消费完整帧后会从内部缓冲中丢弃已读字节，因此这里的载荷采用
/// owned 类型（String/Vec）而非借用切片；这也让报文可以跨缓冲压缩安全持有。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Packet {
    Connect(Connect),
    Publish(Publish),
    Puback(u16),
    /// SUBSCRIBE：(包标识符, (过滤器, 最大QoS) 列表)。
    Subscribe(u16, Vec<(String, u8)>),
    Pingreq,
    Disconnect,
}

/// PUBLISH 报文字段（§3.3）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Publish {
    pub topic: String,
    pub packet_id: Option<u16>,
    pub payload: Vec<u8>,
    pub qos: u8,
    pub dup: bool,
    pub retain: bool,
}

/// SUBACK 失败返回码（§3.9.3）；0/1/2 = 授予 QoS，0x80 = 失败。
pub const SUBACK_FAILURE: u8 = 0x80;
