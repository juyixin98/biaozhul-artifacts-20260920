//! 游标式增量字节解析器。
//!
//! 解析以整份 TCP 帧（UDP 报文）为边界：RFC 1035 压缩指针只能回指同一份报文，
//! 因此解引用时校验“绝对偏移 < 报文总长”。这正是指针越界与指针环的防护点。

use crate::error::DnsError;

/// 可配置的解析上限。默认值见各字段文档与 [`Limits::default`]。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 压缩指针最大跳转次数（默认 64，RFC 未规定；超过即拒绝，环同时被检出）。
    pub max_pointer_jumps: usize,
    /// 单个标签最大字节数（协议固定 63，暴露为参数便于测试）。
    pub max_label_len: usize,
    /// 一个域名的标签数上限（默认 127，协议在线长度 255 字节下的保守值）。
    pub max_labels: usize,
    /// 单个区段（问题/回答/授权/附加）记录数上限（默认 4096）。
    pub max_records_per_section: usize,
    /// 域名在线格式最大长度（协议固定 255）。
    pub max_name_wire_len: usize,
}

impl Limits {
    /// 协议默认上限。
    pub const fn const_default() -> Self {
        Limits {
            max_pointer_jumps: 64,
            max_label_len: 63,
            max_labels: 127,
            max_records_per_section: 4096,
            max_name_wire_len: 255,
        }
    }
}

impl Default for Limits {
    fn default() -> Self {
        Limits::const_default()
    }
}

/// 在一份报文字节切片上移动的只读游标。
pub(crate) struct Reader<'a> {
    pub(crate) msg: &'a [u8],
    pub(crate) pos: usize,
}

impl<'a> Reader<'a> {
    /// 从偏移 0 开始。
    pub(crate) fn new(msg: &'a [u8]) -> Self {
        Reader { msg, pos: 0 }
    }

    /// 报文总长度。
    pub(crate) fn len(&self) -> usize {
        self.msg.len()
    }

    /// 当前偏移。
    pub(crate) fn pos(&self) -> usize {
        self.pos
    }

    /// 读取一个字节。
    pub(crate) fn u8(&mut self) -> Result<u8, DnsError> {
        let b = *self.msg.get(self.pos).ok_or(DnsError::UnexpectedEof)?;
        self.pos += 1;
        Ok(b)
    }

    /// 读取网络字节序 u16。
    pub(crate) fn u16(&mut self) -> Result<u16, DnsError> {
        let hi = self.u8()? as u16;
        let lo = self.u8()? as u16;
        Ok((hi << 8) | lo)
    }

    /// 读取网络字节序 u32。
    pub(crate) fn u32(&mut self) -> Result<u32, DnsError> {
        let mut v = 0u32;
        for _ in 0..4 {
            v = (v << 8) | self.u8()? as u32;
        }
        Ok(v)
    }

    /// 读取定长切片（用于 A/AAAA 的 RDATA）。
    pub(crate) fn take(&mut self, n: usize) -> Result<&'a [u8], DnsError> {
        let end = self.pos.checked_add(n).ok_or(DnsError::UnexpectedEof)?;
        let s = self.msg.get(self.pos..end).ok_or(DnsError::UnexpectedEof)?;
        self.pos = end;
        Ok(s)
    }
}
