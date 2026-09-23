//! 报文构造工具（测试与样例生成用）。
//!
//! 与解析器完全对称的最小“写入器”，同样基于 `std` 手写；
//! 刻意提供若干直接修改长度字段的入口，用来合成畸形报文。

use crate::client_hello::{EXT_ALPN, EXT_SERVER_NAME, EXT_SUPPORTED_VERSIONS};
use crate::record::CONTENT_HANDSHAKE;

/// 构造一条 TLS 记录：type(1) + version(2) + fragment_len(2) + fragment。
pub fn record(content_type: u8, version: u16, fragment: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(5 + fragment.len());
    out.push(content_type);
    out.extend_from_slice(&version.to_be_bytes());
    out.extend_from_slice(&(fragment.len() as u16).to_be_bytes());
    out.extend_from_slice(fragment);
    out
}

/// 构造一条握手消息：msg_type(1) + length(u24) + body。
pub fn handshake(msg_type: u8, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + body.len());
    assert!(body.len() <= 0xFFFFFF);
    out.push(msg_type);
    let len = body.len() as u32;
    out.extend_from_slice(&len.to_be_bytes()[1..4]);
    out.extend_from_slice(body);
    out
}

/// 直接写入“伪造长度”的握手头（用于长度不符样例）。
pub fn handshake_with_declared_len(msg_type: u8, declared_len: usize, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.push(msg_type);
    assert!(declared_len <= 0xFFFFFF);
    let len = declared_len as u32;
    out.extend_from_slice(&len.to_be_bytes()[1..4]);
    out.extend_from_slice(body);
    out
}

/// 直接写入“伪造 fragment 长度”的记录头（声明长度可与实际 fragment 不一致）。
pub fn record_with_declared_len(
    content_type: u8,
    version: u16,
    declared_len: u16,
    fragment: &[u8],
) -> Vec<u8> {
    let mut out = Vec::new();
    out.push(content_type);
    out.extend_from_slice(&version.to_be_bytes());
    out.extend_from_slice(&declared_len.to_be_bytes());
    out.extend_from_slice(fragment);
    out
}

/// 把一个握手消息按 `cuts` 给定的 fragment 大小切成多条 handshake 记录。
///
/// 切点可落在 4 字节握手头内部；第一条 fragment 从握手头开始，后续直接续接，
/// 记录层不重复握手头（符合真实分片语义）。`cuts` 之后的余量自动作为最后一条。
pub fn split_across_records(msg: &[u8], cuts: &[usize]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut pos = 0;
    let total_cut: usize = cuts.iter().sum();
    let mut sizes: Vec<usize> = cuts.to_vec();
    if msg.len() > total_cut {
        sizes.push(msg.len() - total_cut);
    }
    for sz in sizes {
        if sz == 0 {
            continue;
        }
        let end = (pos + sz).min(msg.len());
        out.extend_from_slice(&record(CONTENT_HANDSHAKE, 0x0301, &msg[pos..end]));
        pos = end;
        if pos >= msg.len() {
            break;
        }
    }
    out
}

/// 一个扩展（type + data）。
pub fn extension(ext_type: u16, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&ext_type.to_be_bytes());
    out.extend_from_slice(&(data.len() as u16).to_be_bytes());
    out.extend_from_slice(data);
    out
}

/// SNI(host_name) 扩展数据（含两层长度前缀）。
pub fn sni_extension(hostname: &str) -> Vec<u8> {
    // name_entry: name_type=1B(0) + name_len=2B + name
    let mut entry = Vec::new();
    entry.push(0);
    entry.extend_from_slice(&(hostname.len() as u16).to_be_bytes());
    entry.extend_from_slice(hostname.as_bytes());
    // server_name_list: 2B 长度 + entries
    let mut list = Vec::new();
    list.extend_from_slice(&(entry.len() as u16).to_be_bytes());
    list.extend_from_slice(&entry);
    extension(EXT_SERVER_NAME, &list)
}

/// ALPN 扩展数据。
pub fn alpn_extension(protocols: &[&str]) -> Vec<u8> {
    let mut names = Vec::new();
    for p in protocols {
        names.push(p.len() as u8);
        names.extend_from_slice(p.as_bytes());
    }
    let mut data = Vec::new();
    data.extend_from_slice(&(names.len() as u16).to_be_bytes());
    data.extend_from_slice(&names);
    extension(EXT_ALPN, &data)
}

/// supported_versions 扩展数据（u8 长度前缀 + u16 列表）。
pub fn supported_versions_extension(versions: &[u16]) -> Vec<u8> {
    let mut list = Vec::new();
    for v in versions {
        list.extend_from_slice(&v.to_be_bytes());
    }
    let mut data = Vec::new();
    data.push(list.len() as u8);
    data.extend_from_slice(&list);
    extension(EXT_SUPPORTED_VERSIONS, &data)
}

/// ClientHello 构造器（默认值接近真实浏览器：TLS 1.3 GREASE + SNI/ALPN/版本）。
#[derive(Debug, Clone)]
pub struct ClientHelloBuilder {
    pub legacy_version: u16,
    pub random: [u8; 32],
    pub session_id: Vec<u8>,
    pub cipher_suites: Vec<u16>,
    pub compression_methods: Vec<u8>,
    pub extensions: Vec<Vec<u8>>,
}

impl Default for ClientHelloBuilder {
    fn default() -> Self {
        let mut random = [0u8; 32];
        for (i, b) in random.iter_mut().enumerate() {
            *b = (i as u8).wrapping_mul(7).wrapping_add(0x11);
        }
        ClientHelloBuilder {
            legacy_version: 0x0303,
            random,
            session_id: vec![0xAB; 32],
            // GREASE 套件排在最前，后随真实套件。
            cipher_suites: vec![0x2A2A, 0x1301, 0x1302, 0x1303, 0xC02F],
            compression_methods: vec![0x00],
            extensions: Vec::new(),
        }
    }
}

impl ClientHelloBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_sni(mut self, host: &str) -> Self {
        self.extensions.push(sni_extension(host));
        self
    }

    pub fn with_alpn(mut self, protocols: &[&str]) -> Self {
        self.extensions.push(alpn_extension(protocols));
        self
    }

    pub fn with_supported_versions(mut self, versions: &[u16]) -> Self {
        self.extensions.push(supported_versions_extension(versions));
        self
    }

    pub fn with_grease_extension(mut self, grease: u16) -> Self {
        self.extensions.push(extension(grease, &[0x2A, 0x2A]));
        self
    }

    pub fn with_unknown_extension(mut self, ext_type: u16, data: &[u8]) -> Self {
        self.extensions.push(extension(ext_type, data));
        self
    }

    pub fn with_raw_extension(mut self, raw: Vec<u8>) -> Self {
        self.extensions.push(raw);
        self
    }

    /// 输出 ClientHello **body**（不含握手头）。
    pub fn build_body(&self) -> Vec<u8> {
        let mut ext_block = Vec::new();
        for e in &self.extensions {
            ext_block.extend_from_slice(e);
        }

        let mut body = Vec::new();
        body.extend_from_slice(&self.legacy_version.to_be_bytes());
        body.extend_from_slice(&self.random);

        body.push(self.session_id.len() as u8);
        body.extend_from_slice(&self.session_id);

        body.extend_from_slice(&((self.cipher_suites.len() * 2) as u16).to_be_bytes());
        for s in &self.cipher_suites {
            body.extend_from_slice(&s.to_be_bytes());
        }

        body.push(self.compression_methods.len() as u8);
        body.extend_from_slice(&self.compression_methods);

        body.extend_from_slice(&(ext_block.len() as u16).to_be_bytes());
        body.extend_from_slice(&ext_block);
        body
    }

    /// 输出完整的 ClientHello 握手消息（含 4 字节握手头）。
    pub fn build_message(&self) -> Vec<u8> {
        handshake(1, &self.build_body())
    }
}
