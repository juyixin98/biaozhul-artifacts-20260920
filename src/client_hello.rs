//! 明文 ClientHello（TLS 1.2 RFC 5246 §7.4.1.2 与 TLS 1.3 RFC 8446 §4.1.2）解析。
//!
//! 只解析观察目标：SNI（server_name, 类型 0）、ALPN（类型 16）、
//! supported_versions（类型 43），其余扩展原样登记为“未知扩展”（带预览）。
//! GREASE 值被识别并过滤，既不算未知，重复出现也不算重复扩展。

use crate::config::Config;
use crate::error::ParseError;
use crate::grease::is_grease;
use crate::reader::Reader;

/// 扩展类型：server_name (SNI)。
pub const EXT_SERVER_NAME: u16 = 0;
/// 扩展类型：application_layer_protocol_negotiation (ALPN)。
pub const EXT_ALPN: u16 = 16;
/// 扩展类型：supported_versions。
pub const EXT_SUPPORTED_VERSIONS: u16 = 43;

/// 一个观察器不认识的扩展（保留类型号与前若干字节预览）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownExtension {
    pub ext_type: u16,
    pub data_len: usize,
    pub data_preview: Vec<u8>,
}

/// 从明文 ClientHello 中观察到的信息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientHelloInfo {
    /// ClientHello.legacy_version（0x0301 TLS 1.0 … 0x0304 TLS 1.2；
    /// TLS 1.3 的真实版本在 supported_versions 扩展里）。
    pub legacy_version: u16,
    pub random: [u8; 32],
    pub session_id: Vec<u8>,
    /// 密码套件列表（已剔除 GREASE 值）。
    pub cipher_suites: Vec<u16>,
    /// 出现在密码套件列表里的 GREASE 值。
    pub grease_cipher_suites: Vec<u16>,
    pub compression_methods: Vec<u8>,
    /// SNI 主机名；无扩展或列表为空时为 None。
    pub sni: Option<String>,
    /// ALPN 协议名（按报文顺序）。
    pub alpn: Vec<String>,
    /// supported_versions 扩展内容（已剔除 GREASE）；无该扩展时为空。
    pub supported_versions: Vec<u16>,
    /// supported_versions 扩展里的 GREASE 值。
    pub grease_versions: Vec<u16>,
    /// 扩展块里的 GREASE 扩展类型（可能有多个，因实现而异）。
    pub grease_extensions: Vec<u16>,
    /// 其余不解析的扩展（按报文顺序）。
    pub unknown_extensions: Vec<UnknownExtension>,
}

impl ClientHelloInfo {
    /// 解析一个 ClientHello 握手消息体（不含 4 字节 Handshake 头）。
    pub fn parse(body: &[u8], config: &Config) -> Result<Self, ParseError> {
        if body.len() > config.max_client_hello {
            return Err(ParseError::ClientHelloTooLarge {
                len: body.len(),
                max: config.max_client_hello,
            });
        }

        let mut info = ClientHelloInfo {
            legacy_version: 0,
            random: [0u8; 32],
            session_id: Vec::new(),
            cipher_suites: Vec::new(),
            grease_cipher_suites: Vec::new(),
            compression_methods: Vec::new(),
            sni: None,
            alpn: Vec::new(),
            supported_versions: Vec::new(),
            grease_versions: Vec::new(),
            grease_extensions: Vec::new(),
            unknown_extensions: Vec::new(),
        };

        // 整体包一层有界块，保证体内声明的长度不越过握手消息边界，
        // 且体内不允许残留字节。
        let mut top = Reader::new(body, "client_hello_body");
        top.enter(body.len(), "client_hello", |r| {
            let legacy_version = r.u16()?;
            if !(0x0301..=0x0304).contains(&legacy_version) {
                return Err(ParseError::BadRecordVersion {
                    version: legacy_version,
                    layer: "client_hello",
                });
            }
            info.legacy_version = legacy_version;
            info.random = r.array32()?;

            // session_id: opaque<1..2^8-1>，长度不得超过 32。
            let sid = r.vec_u8_len("session_id")?;
            if sid.len() > 32 {
                return Err(ParseError::InvalidValue {
                    field: "session_id",
                    reason: "session id longer than 32 bytes",
                });
            }
            info.session_id = sid.to_vec();

            // cipher_suites: 1..2^16-2 字节的 u16 数组，偶数长度、至少一个套件。
            let suites = r.vec_u16_len("cipher_suites")?;
            if suites.len() % 2 != 0 || suites.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "cipher_suites",
                    reason: "cipher_suites length must be a non-zero multiple of 2",
                });
            }
            let (chunks, remainder) = suites.as_chunks::<2>();
            if !remainder.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "cipher_suites",
                    reason: "cipher_suites length must be a non-zero multiple of 2",
                });
            }
            for [hi, lo] in chunks {
                let suite = u16::from_be_bytes([*hi, *lo]);
                if is_grease(suite) {
                    info.grease_cipher_suites.push(suite);
                } else {
                    info.cipher_suites.push(suite);
                }
            }
            if info.cipher_suites.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "cipher_suites",
                    reason: "no non-GREASE cipher suite offered",
                });
            }

            // compression_methods: 1..2^8-1 字节。
            let comp = r.vec_u8_len("compression_methods")?;
            if comp.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "compression_methods",
                    reason: "at least one compression method is required",
                });
            }
            info.compression_methods = comp.to_vec();

            // 扩展块：TLS 1.2+ 的 ClientHello 必须携带。
            if r.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "extensions",
                    reason: "ClientHello has no extensions block",
                });
            }
            r.sub_u16("extensions", |er| parse_extensions(er, &mut info, config))?;

            r.expect_empty("client_hello")
        })?;

        Ok(info)
    }
}

fn parse_extensions(
    r: &mut Reader<'_>,
    info: &mut ClientHelloInfo,
    config: &Config,
) -> Result<(), ParseError> {
    let mut seen: Vec<u16> = Vec::new();
    while !r.is_empty() {
        let ext_type = r.u16()?;
        // 扩展数据由 u16 长度前缀界定；外层装不下即为长度不符。
        let data = r.vec_u16_len("extension_data")?;

        if is_grease(ext_type) {
            // GREASE 扩展：合法占位，不参与去重、不登记为未知。
            info.grease_extensions.push(ext_type);
            continue;
        }
        if seen.contains(&ext_type) {
            return Err(ParseError::DuplicateExtension { ext_type });
        }
        seen.push(ext_type);

        match ext_type {
            EXT_SERVER_NAME => {
                info.sni = Some(parse_sni(data)?);
            }
            EXT_ALPN => {
                info.alpn = parse_alpn(data)?;
            }
            EXT_SUPPORTED_VERSIONS => {
                let (versions, grease) = parse_supported_versions(data)?;
                info.supported_versions = versions;
                info.grease_versions = grease;
            }
            other => {
                let n = data.len().min(config.max_extension_preview);
                info.unknown_extensions.push(UnknownExtension {
                    ext_type: other,
                    data_len: data.len(),
                    data_preview: data[..n].to_vec(),
                });
            }
        }
    }
    Ok(())
}

/// 解析 server_name 扩展。仅识别 host_name（name_type = 0）。
fn parse_sni(data: &[u8]) -> Result<String, ParseError> {
    let mut list = Reader::new(data, "server_name");
    list.sub_u16("server_name_list", |r| {
        let mut host: Option<String> = None;
        while !r.is_empty() {
            let name_type = r.u8()?;
            let name = r.vec_u16_len("server_name_entry")?;
            if name_type != 0 {
                // 其它 name_type 不支持，跳过即可（长度前缀已界定）。
                continue;
            }
            if host.is_some() {
                return Err(ParseError::InvalidValue {
                    field: "server_name",
                    reason: "more than one host_name entry",
                });
            }
            host = Some(validate_hostname(name)?);
        }
        host.ok_or(ParseError::InvalidValue {
            field: "server_name",
            reason: "server_name_list contains no host_name entry",
        })
    })
}

fn validate_hostname(name: &[u8]) -> Result<String, ParseError> {
    if name.is_empty() || name.len() > 253 {
        return Err(ParseError::InvalidValue {
            field: "server_name",
            reason: "host_name length out of range 1..=253",
        });
    }
    // SNI 主机名是 ASCII (RFC 5890)；拒绝非 ASCII 与控制字符。
    if !name
        .iter()
        .all(|&b| b.is_ascii_graphic() || b == b'-' || b == b'.')
    {
        return Err(ParseError::InvalidValue {
            field: "server_name",
            reason: "host_name contains non-ASCII or control bytes",
        });
    }
    // 上面已保证全部 ASCII，from_utf8 不会失败。
    Ok(String::from_utf8(name.to_vec()).expect("validated ASCII"))
}

/// 解析 ALPN 扩展：ProtocolNameList。
fn parse_alpn(data: &[u8]) -> Result<Vec<String>, ParseError> {
    let mut outer = Reader::new(data, "alpn");
    outer.sub_u16("protocol_name_list", |r| {
        let mut out = Vec::new();
        while !r.is_empty() {
            let proto = r.vec_u8_len("protocol_name")?;
            if proto.is_empty() {
                return Err(ParseError::InvalidValue {
                    field: "alpn",
                    reason: "empty protocol name",
                });
            }
            if !proto.iter().all(|&b| b.is_ascii_graphic() || b == b'-') {
                return Err(ParseError::InvalidValue {
                    field: "alpn",
                    reason: "protocol name contains non-printable ASCII",
                });
            }
            out.push(String::from_utf8(proto.to_vec()).expect("validated ASCII"));
        }
        if out.is_empty() {
            return Err(ParseError::InvalidValue {
                field: "alpn",
                reason: "empty ALPN list",
            });
        }
        Ok(out)
    })
}

/// 解析 supported_versions 扩展，返回 (非 GREASE 版本, GREASE 版本)。
fn parse_supported_versions(data: &[u8]) -> Result<(Vec<u16>, Vec<u16>), ParseError> {
    let mut outer = Reader::new(data, "supported_versions");
    outer.sub_u8("supported_versions_list", |r| -> Result<_, ParseError> {
        let mut versions = Vec::new();
        let mut grease = Vec::new();
        while !r.is_empty() {
            let v = r.u16()?;
            if is_grease(v) {
                grease.push(v);
            } else {
                versions.push(v);
            }
        }
        if versions.is_empty() && grease.is_empty() {
            return Err(ParseError::InvalidValue {
                field: "supported_versions",
                reason: "empty version list",
            });
        }
        Ok((versions, grease))
    })
}
