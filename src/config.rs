//! 观察器配置：明确声明所支持的子集与各类长度上限。

/// 长度上限与子集开关。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// TLSPlaintext.fragment 的最大字节数。
    /// RFC 8446 规定明文记录最大 2^14=16384 字节（默认值）。
    pub max_record_fragment: usize,
    /// 单个 Handshake 消息的最大字节数（u24 长度字段声明值）。
    /// TLS 1.3 允许分片记录承载最大 1MB 的握手消息。
    pub max_handshake_message: usize,
    /// ClientHello 消息体的最大字节数（在握手消息上限之内再收紧）。
    pub max_client_hello: usize,
    /// “未知扩展” data_preview 保留的前缀字节数。
    pub max_extension_preview: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            max_record_fragment: 16_384,
            max_handshake_message: 1 << 20,
            max_client_hello: 1 << 16,
            max_extension_preview: 16,
        }
    }
}

impl Config {
    /// 更严格的配置（例如把记录上限压到 2^14，拒绝任何放大）。
    pub fn strict() -> Self {
        Config::default()
    }
}
