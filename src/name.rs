//! 域名（RFC 1035 §3.1 / §4.1.4）：标签序列、压缩指针安全解码与编码。
//!
//! 压缩指针的规则（线格式首字节高两位）：
//!
//! | 高两位 | 含义                       |
//! |--------|----------------------------|
//! | `00`   | 普通标签，低 6 位为标签长度 |
//! | `11`   | 压缩指针，与下一字节拼成 14 位绝对偏移 |
//! | `01`   | RFC 1035 保留（拒绝）        |
//! | `10`   | EDNS 扩展标签（本实现不支持，一律拒绝） |
//!
//! 指针必须回指报文中更早出现的位置吗？RFC 文字上只定义“偏移”，实践中压缩
//! 指针几乎总是向后；但本实现**允许前向指针**作为验收特性：只要目标在报文内，
//! 就跟随之。真正的安全边界是：(1) 目标偏移不越界；(2) 跳转次数有上限；
//! (3) 重复访问同一偏移即判定为环。前向指针因此同样被严格约束。

use crate::error::DnsError;
use crate::parser::{Limits, Reader};

/// RFC 1035 §2.3.4：名字在线格式（含根标签）最大 255 字节。
const MAX_NAME_WIRE_LEN: usize = 255;

/// 一个域名：标签按出现顺序保存（不含长度字节，不含根零字节），根域名为空序列。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Name {
    labels: Vec<Vec<u8>>,
}

impl Name {
    /// 从标签序列构造（用于构造报文/应答）。标签内容按字节保留，比较时大小写不敏感。
    pub fn from_labels<I, L>(labels: I) -> Result<Name, DnsError>
    where
        I: IntoIterator<Item = L>,
        L: AsRef<[u8]>,
    {
        let limits = Limits::default();
        let mut out: Vec<Vec<u8>> = Vec::new();
        let mut wire_len = 1usize; // 终止根字节
        for l in labels {
            let b = l.as_ref();
            if b.is_empty() {
                return Err(DnsError::EmptyLabel);
            }
            if b.len() > limits.max_label_len {
                return Err(DnsError::LabelTooLong);
            }
            wire_len = wire_len
                .checked_add(1 + b.len())
                .ok_or(DnsError::NameTooLong)?;
            if wire_len > MAX_NAME_WIRE_LEN {
                return Err(DnsError::NameTooLong);
            }
            if out.len() >= limits.max_labels {
                return Err(DnsError::TooManyLabels);
            }
            out.push(b.to_vec());
        }
        Ok(Name { labels: out })
    }

    /// 解析点分文本（`example.com` 或 `example.com.` 均可；大小写按字节保留）。
    pub fn from_dotted(text: &str) -> Result<Name, DnsError> {
        if text.is_empty() || text == "." {
            return Ok(Name { labels: Vec::new() });
        }
        let trimmed = text.strip_suffix('.').unwrap_or(text);
        if trimmed.is_empty() {
            // "." 已在上方处理；走到这里说明输入异常。
            return Err(DnsError::EmptyLabel);
        }
        Name::from_labels(trimmed.split('.').map(|s| s.as_bytes().to_vec()))
    }

    /// 根域名（`.`）。
    pub fn root() -> Name {
        Name { labels: Vec::new() }
    }

    /// 是否为根域名。
    pub fn is_root(&self) -> bool {
        self.labels.is_empty()
    }

    /// 标签数（根域名为 0）。
    pub fn label_count(&self) -> usize {
        self.labels.len()
    }

    /// 第 i 个标签（不含长度字节）。
    pub fn label(&self, i: usize) -> Option<&[u8]> {
        self.labels.get(i).map(|v| v.as_slice())
    }

    /// 在线长度（每个标签 1 字节长度 + 内容，末尾 1 字节根标签）。
    pub fn wire_len(&self) -> usize {
        self.labels.iter().fold(1, |acc, l| acc + 1 + l.len())
    }

    /// 不使用压缩编码到 `out`。返回写入字节数。
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.wire_len());
        self.encode_into(&mut out);
        out
    }

    /// 不使用压缩编码追加到 `out`。
    pub fn encode_into(&self, out: &mut Vec<u8>) {
        for l in &self.labels {
            out.push(l.len() as u8);
            out.extend_from_slice(l);
        }
        out.push(0);
    }

    /// 在游标处解析名字（支持压缩指针）。
    ///
    /// `start` 为名字在报文中的起始绝对偏移。返回 `(名字, 该名字在线表示占用的
    /// 字节数)`；占用字节数只计物理上位于 `start` 之后的序列（若以指针结束则为
    /// “标签序列 + 2 字节指针”），这正是外层 RR 推进所需要的量。
    pub(crate) fn parse(
        reader: &mut Reader<'_>,
        limits: &Limits,
    ) -> Result<(Name, usize), DnsError> {
        let mut labels: Vec<Vec<u8>> = Vec::new();
        let mut cur = Reader {
            msg: reader.msg_ref(),
            pos: reader.pos(),
        };
        let start = reader.pos();
        let mut end_of_physical: Option<usize> = None;
        let mut jumps = 0usize;
        let mut visited: Vec<usize> = Vec::new();
        // 展开后名字的在线长度（只计“长度字节+标签”，末尾根字节在结束时补 1）；
        // 指针自身的两个字节不属于展开后的名字，不计入 255 上限。
        let mut wire_len = 0usize;

        loop {
            let offset = cur.pos();
            // 环检测：同一绝对偏移重复出现即环（含自指）。
            if visited.contains(&offset) {
                return Err(DnsError::PointerLoop);
            }
            visited.push(offset);

            let first = cur.u8()?;
            match first >> 6 {
                0b00 => {
                    let len = (first & 0x3F) as usize;
                    if len == 0 {
                        // 根标签，名字结束。物理序列到此为止。
                        if end_of_physical.is_none() {
                            end_of_physical = Some(cur.pos());
                        }
                        // 展开后的在线长度 = 各标签(1+len) + 终止根 1 字节。
                        if wire_len + 1 > limits.max_name_wire_len {
                            return Err(DnsError::NameTooLong);
                        }
                        break;
                    }
                    if len > limits.max_label_len {
                        return Err(DnsError::LabelTooLong);
                    }
                    let raw = cur.take(len)?;
                    wire_len = wire_len.checked_add(1 + len).ok_or(DnsError::NameTooLong)?;
                    if wire_len > limits.max_name_wire_len {
                        return Err(DnsError::NameTooLong);
                    }
                    if labels.len() >= limits.max_labels {
                        return Err(DnsError::TooManyLabels);
                    }
                    labels.push(raw.to_vec());
                }
                0b11 => {
                    // 压缩指针：高 6 位已在 first 中，再取一字节拼 14 位偏移。
                    let second = cur.u8().map_err(|_| DnsError::TruncatedPointer)?;
                    let target = ((first as usize & 0x3F) << 8) | second as usize;
                    if target >= cur.len() {
                        return Err(DnsError::PointerOutOfBounds);
                    }
                    if end_of_physical.is_none() {
                        // 指针占两字节，物理序列在指针之后结束。
                        end_of_physical = Some(cur.pos());
                    }
                    jumps += 1;
                    if jumps > limits.max_pointer_jumps {
                        return Err(DnsError::PointerChainTooLong);
                    }
                    // 跳到目标绝对偏移，仍以整份报文为边界。
                    cur = Reader::at_offset(cur.msg_ref(), target)?;
                }
                0b01 => {
                    // 01xxxxxx：RFC 1035 保留，报文不得出现。
                    return Err(DnsError::ReservedLabelKind);
                }
                _ => {
                    // 10xxxxxx：EDNS 扩展标签（RFC 2671）。本实现不支持扩展标签，
                    // 一律以明确的错误拒绝。
                    return Err(DnsError::UnsupportedExtendedLabel);
                }
            }
        }

        let consumed = end_of_physical.unwrap_or(cur.pos()).saturating_sub(start);
        // 外层游标跳过物理序列（标签 + 根或指针）。
        reader.skip_to(start + consumed)?;
        Ok((Name { labels }, consumed))
    }

    /// 不区分大小写地按标签比较（RFC 4343：DNS 名字大小写不敏感）。
    pub fn eq_ignore_ascii_case(&self, other: &Name) -> bool {
        if self.labels.len() != other.labels.len() {
            return false;
        }
        self.labels
            .iter()
            .zip(&other.labels)
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
    }
}

impl std::fmt::Display for Name {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.is_root() {
            return write!(f, ".");
        }
        for l in &self.labels {
            // 标签语义上是 ASCII（LDH）；非可打印字节按转义 \nnn 输出。
            for &b in l {
                if b.is_ascii_graphic() && b != b'\\' && b != b'.' {
                    write!(f, "{}", b as char)?;
                } else {
                    write!(f, "\\{b:03}")?;
                }
            }
            write!(f, ".")?;
        }
        Ok(())
    }
}

// 给 name 解析用的内部辅助：在同一报文上以指定绝对偏移重建游标。
impl<'a> Reader<'a> {
    pub(crate) fn msg_ref(&self) -> &'a [u8] {
        self.msg
    }

    pub(crate) fn at_offset(msg: &'a [u8], pos: usize) -> Result<Reader<'a>, DnsError> {
        if pos > msg.len() {
            return Err(DnsError::PointerOutOfBounds);
        }
        Ok(Reader { msg, pos })
    }

    pub(crate) fn skip_to(&mut self, pos: usize) -> Result<(), DnsError> {
        if pos > self.msg.len() {
            return Err(DnsError::UnexpectedEof);
        }
        self.pos = pos;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_name_roundtrip() {
        let n = Name::from_dotted(".").unwrap();
        assert!(n.is_root());
        assert_eq!(n.encode(), vec![0]);
        assert_eq!(n.wire_len(), 1);
    }

    #[test]
    fn dotted_text_basic() {
        let n = Name::from_dotted("Example.COM.").unwrap();
        assert_eq!(n.label_count(), 2);
        assert_eq!(n.label(0), Some(b"Example".as_ref()));
        // 编码保持原文大小写。
        assert_eq!(
            n.encode(),
            vec![7, b'E', b'x', b'a', b'm', b'p', b'l', b'e', 3, b'C', b'O', b'M', 0]
        );
    }

    #[test]
    fn rejects_empty_label() {
        assert_eq!(Name::from_dotted("a..b").unwrap_err(), DnsError::EmptyLabel);
    }

    #[test]
    fn rejects_oversize_label() {
        let big = "a".repeat(64);
        assert_eq!(Name::from_dotted(&big).unwrap_err(), DnsError::LabelTooLong);
    }
}
