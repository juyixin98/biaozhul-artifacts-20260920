//! 明确枚举的错误类型：输入报文的每一种畸形都有对应变体。

use std::fmt;

/// DNS 区段，用于长度上限相关错误中指明越界发生在哪个区段。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Section {
    /// 问题段（Question）
    Question,
    /// 回答段（Answer）
    Answer,
    /// 授权段（Authority）
    Authority,
    /// 附加段（Additional）
    Additional,
}

impl fmt::Display for Section {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Section::Question => write!(f, "question"),
            Section::Answer => write!(f, "answer"),
            Section::Authority => write!(f, "authority"),
            Section::Additional => write!(f, "additional"),
        }
    }
}

/// DNS 解析 / 编码错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DnsError {
    /// 读取时越过报文末尾。
    UnexpectedEof,
    /// 报文短于 12 字节定长头部。
    MessageTooShort,
    /// 编码后报文长度超过 65535 字节，无法加 TCP 长度前缀。
    MessageTooLong,
    /// 标签以保留的二进制前缀 01 开头（RFC 1035 保留）。
    ReservedLabelKind,
    /// 标签以 EDNS 扩展标签前缀 01 之外的保留扩展位开头（低 6 位非 0）。
    UnsupportedExtendedLabel,
    /// 压缩指针指向自身（环检测）。
    PointerLoop,
    /// 压缩指针跳转次数超过配置上限（防止伪造长链耗尽资源）。
    PointerChainTooLong,
    /// 压缩指针目标偏移越出报文范围。
    PointerOutOfBounds,
    /// 压缩指针只有一个字节，报文在此截断。
    TruncatedPointer,
    /// 单个标签超过 63 字节。
    LabelTooLong,
    /// 域名在线格式超过 255 字节（RFC 1035 上限）。
    NameTooLong,
    /// 域名标签数超过配置上限。
    TooManyLabels,
    /// 由文本构造域名时出现空标签（连续点、前导点或末尾多余点）。
    EmptyLabel,
    /// RDLENGTH 声明的长度超出报文剩余范围（即截断的资源记录）。
    RdataLengthMismatch {
        /// 声明的 RDLENGTH
        declared: usize,
        /// 实际剩余字节数（解析 CNAME 名字时为名字实际占用的物理字节数）
        available: usize,
    },
    /// 固定长度 RDATA 类型的 RDLENGTH 不符合协议规定。
    InvalidRdataLen {
        /// 类型码
        rtype: u16,
        /// 报文中声明的 RDLENGTH
        declared: usize,
        /// 该类型要求的 RDLENGTH（A=4，AAAA=16）
        expected: usize,
    },
    /// RDATA 中的压缩名字解析失败（内层错误）。
    InvalidRdataName(Box<DnsError>),
    /// 区段记录数超过配置上限。
    TooManyRecords(Section),
}

impl fmt::Display for DnsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DnsError::UnexpectedEof => write!(f, "读取越过报文末尾（截断）"),
            DnsError::MessageTooShort => write!(f, "报文短于 12 字节定长头部"),
            DnsError::MessageTooLong => write!(f, "编码后报文超过 65535 字节"),
            DnsError::ReservedLabelKind => write!(f, "标签使用了保留的二进制前缀 01"),
            DnsError::UnsupportedExtendedLabel => {
                write!(f, "标签使用了不支持的 EDNS 扩展标签类型")
            }
            DnsError::PointerLoop => write!(f, "压缩指针形成环"),
            DnsError::PointerChainTooLong => write!(f, "压缩指针链超过跳转次数上限"),
            DnsError::PointerOutOfBounds => write!(f, "压缩指针目标偏移越界"),
            DnsError::TruncatedPointer => write!(f, "压缩指针第二个字节缺失（截断）"),
            DnsError::LabelTooLong => write!(f, "标签长度超过 63 字节"),
            DnsError::NameTooLong => write!(f, "域名在线长度超过 255 字节"),
            DnsError::TooManyLabels => write!(f, "标签数超过上限"),
            DnsError::EmptyLabel => write!(f, "文本域名中出现空标签"),
            DnsError::RdataLengthMismatch {
                declared,
                available,
            } => write!(
                f,
                "RDLENGTH={declared} 但 RDATA 剩余仅 {available} 字节（资源记录截断或名字越界）"
            ),
            DnsError::InvalidRdataLen {
                rtype,
                declared,
                expected,
            } => write!(
                f,
                "类型 {rtype} 的 RDLENGTH={declared} 非法，应为 {expected}"
            ),
            DnsError::InvalidRdataName(e) => write!(f, "RDATA 中的名字无效：{e}"),
            DnsError::TooManyRecords(s) => write!(f, "{s} 段记录数超过上限"),
        }
    }
}

impl std::error::Error for DnsError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            DnsError::InvalidRdataName(e) => Some(e.as_ref()),
            _ => None,
        }
    }
}
