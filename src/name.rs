//! DNS 名字表示、压缩指针解压（带跳转上限与环检测）以及压缩编码。

use std::collections::HashMap;

use crate::error::{DnsError, Result};
use crate::reader::Reader;

/// 单个标签最大长度（RFC 1035：标签长度字段低 6 位最大 63）。
pub const MAX_LABEL_LEN: usize = 63;
/// 一个域名在线格式上的最大总长度（含长度字节与结尾 0）。
pub const MAX_NAME_WIRE_LEN: usize = 255;
/// 解压时允许跟随的最大压缩指针跳转次数（防御性上限，远低于任何合法报文需要）。
pub const MAX_POINTER_JUMPS: usize = 64;

/// 域名：标签序列，每个标签是不透明字节（合法线格式允许任意字节，
/// 因此不强制 ASCII）。根名为空序列。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct Name {
    labels: Vec<Vec<u8>>,
}

impl Name {
    /// 从标签序列构造；返回错误当且仅当标签违反长度约束。
    pub fn from_labels<I, L>(labels: I) -> Result<Self>
    where
        I: IntoIterator<Item = L>,
        L: AsRef<[u8]>,
    {
        let mut out = Vec::new();
        let mut wire = 0usize;
        for l in labels {
            let bytes = l.as_ref();
            if bytes.is_empty() {
                return Err(DnsError::InvalidName("出现空标签（非根结尾）".into()));
            }
            if bytes.len() > MAX_LABEL_LEN {
                return Err(DnsError::LabelTooLong {
                    len: bytes.len(),
                    max: MAX_LABEL_LEN,
                });
            }
            wire += 1 + bytes.len();
            if wire + 1 > MAX_NAME_WIRE_LEN {
                return Err(DnsError::LabelTooLong {
                    len: wire + 1,
                    max: MAX_NAME_WIRE_LEN,
                });
            }
            out.push(bytes.to_vec());
        }
        Ok(Self { labels: out })
    }

    /// 解析点分文本（如 `www.example.com`）；`.` 表示根。
    /// 文本中的字节按 UTF-8 逐字取，不做 IDN/punycode 转换。
    pub fn from_text(text: &str) -> Result<Self> {
        if text == "." || text.is_empty() {
            return Ok(Self::root());
        }
        let iter = text.split('.').filter(|s| !s.is_empty());
        Self::from_labels(iter.map(|s| s.as_bytes().to_vec()))
    }

    /// 根名。
    pub fn root() -> Self {
        Self { labels: Vec::new() }
    }

    /// 是否为根名。
    pub fn is_root(&self) -> bool {
        self.labels.is_empty()
    }

    /// 标签数。
    pub fn label_count(&self) -> usize {
        self.labels.len()
    }

    /// 标签迭代器。
    pub fn labels(&self) -> impl Iterator<Item = &[u8]> {
        self.labels.iter().map(|v| v.as_slice())
    }

    /// 点分文本（非法 UTF-8 标签使用替换字符），非根名不带结尾点。
    pub fn to_text_lossy(&self) -> String {
        if self.labels.is_empty() {
            return ".".to_string();
        }
        let parts: Vec<String> = self
            .labels
            .iter()
            .map(|l| String::from_utf8_lossy(l).into_owned())
            .collect();
        parts.join(".")
    }

    /// 未压缩线格式长度（每个标签 1 字节长度前缀 + 内容 + 结尾 0）。
    pub fn wire_len(&self) -> usize {
        self.labels.iter().map(|l| 1 + l.len()).sum::<usize>() + 1
    }

    /// 以 `reader` 当前位置为起点解析域名（可能含压缩指针），
    /// 返回解析出的名字以及名字在线格式中占用的字节数
    /// （压缩情况下为“起始偏移 → 指针两字节”的长度）。
    pub fn parse(reader: &mut Reader<'_>) -> Result<(Name, usize)> {
        let start = reader.position();

        let mut labels: Vec<Vec<u8>> = Vec::new();
        // 当前读取的绝对偏移；first_end 记录第一次遇到指针前的结束位置。
        let mut cur = start;
        let mut first_end: Option<usize> = None;
        let mut jumps = 0usize;
        // 环检测：记录已经“作为指针目标”访问过的绝对偏移。
        let mut visited_ptrs: Vec<u16> = Vec::new();
        let mut wire_len_total = 0usize;

        loop {
            let len_byte = read_byte_at(cur, reader_buf(reader))?;
            cur += 1;

            if len_byte == 0 {
                // 名字结束。
                if first_end.is_none() {
                    first_end = Some(cur);
                }
                break;
            }

            let prefix = len_byte & 0xC0;
            match prefix {
                0x00 => {
                    // 普通标签。
                    let llen = (len_byte & 0x3F) as usize;
                    if llen > MAX_LABEL_LEN {
                        // 0x00 前缀下低 6 位不可能 > 63，此处仅为防御。
                        return Err(DnsError::LabelTooLong {
                            len: llen,
                            max: MAX_LABEL_LEN,
                        });
                    }
                    let data = read_slice_at(cur, llen, reader_buf(reader))?;
                    labels.push(data.to_vec());
                    wire_len_total += 1 + llen;
                    cur += llen;
                    if wire_len_total + 1 > MAX_NAME_WIRE_LEN {
                        return Err(DnsError::LabelTooLong {
                            len: wire_len_total + 1,
                            max: MAX_NAME_WIRE_LEN,
                        });
                    }
                }
                0xC0 => {
                    // 压缩指针（2 字节），目标为 14 位偏移。
                    let second = read_byte_at(cur, reader_buf(reader))?;
                    let target = u16::from_be_bytes([len_byte & 0x3F, second]);
                    let target_us = target as usize;
                    // 进入本分支时 cur 已指向指针第二字节；
                    // 指针在名字中占用 2 字节，故“名字结束”的下一位置是 cur+1。

                    if first_end.is_none() {
                        first_end = Some(cur + 1);
                    }
                    jumps += 1;
                    if jumps > MAX_POINTER_JUMPS {
                        return Err(DnsError::TooManyPointers { jumps });
                    }
                    if visited_ptrs.contains(&target) {
                        return Err(DnsError::PointerLoop { offset: target });
                    }
                    visited_ptrs.push(target);
                    // 指针必须指向报文内部；允许前向指针（目标在当前位置之后），
                    // 因为合法报文里 RDATA/后续段可被前面的名字引用，
                    // 环与跳转上限仍由上面的 visited/jumps 保证终止。
                    if target_us >= reader_buf(reader).len() {
                        return Err(DnsError::PointerOutOfBounds {
                            target: target_us,
                            msg_len: reader_buf(reader).len(),
                        });
                    }
                    cur = target_us;
                }
                _ => {
                    // 0b01 / 0b10：保留扩展标签类型，本实现明确不支持。
                    // 只保留高 2 位（右移 6 位），值为 1 或 2。
                    return Err(DnsError::UnsupportedLabelType {
                        prefix: prefix >> 6,
                    });
                }
            }
        }

        let consumed = first_end.unwrap_or(cur) - start;
        // 推进父读取器到名字占用结束处（压缩时只跳过指针 2 字节）。
        advance_reader(reader, start + consumed)?;
        Ok((Name { labels }, consumed))
    }
}

impl std::fmt::Display for Name {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.to_text_lossy())
    }
}

// ---- 读取器上的“绝对偏移”辅助函数（绕开只顺序推进的游标） ----

fn reader_buf<'a>(r: &'a Reader<'_>) -> &'a [u8] {
    r.full_buffer()
}

fn read_byte_at(offset: usize, buf: &[u8]) -> Result<u8> {
    buf.get(offset).copied().ok_or(DnsError::UnexpectedEof {
        offset,
        len: buf.len(),
    })
}

fn read_slice_at(offset: usize, n: usize, buf: &[u8]) -> Result<&[u8]> {
    let end = offset.checked_add(n).ok_or(DnsError::UnexpectedEof {
        offset,
        len: buf.len(),
    })?;
    buf.get(offset..end).ok_or(DnsError::UnexpectedEof {
        offset,
        len: buf.len(),
    })
}

fn advance_reader(r: &mut Reader<'_>, absolute: usize) -> Result<()> {
    r.seek_to(absolute)
}

/// 支持压缩的域名编码器。
///
/// 编码策略（RFC 1035 §4.1.4）：
/// 对每个后缀（由长到短）查 `table`；若该后缀此前已出现在报文绝对偏移
/// `p <= 0x3FFF` 处，则输出两字节指针并结束；否则输出该标签并把
/// “该后缀起点偏移”登记进表。
pub fn write_name(
    out: &mut Vec<u8>,
    name: &Name,
    table: &mut NameTable,
) -> Result<()> {
    if name.is_root() {
        table.register_root(out.len());
        out.push(0);
        return Ok(());
    }

    // 名字自身长度已在构造时保证 <=255；这里再做一次防御性校验。
    if name.wire_len() > MAX_NAME_WIRE_LEN {
        return Err(DnsError::LabelTooLong {
            len: name.wire_len(),
            max: MAX_NAME_WIRE_LEN,
        });
    }

    let n = name.label_count();
    for i in 0..n {
        let suffix = Name {
            labels: name.labels[i..].to_vec(),
        };
        if let Some(&ptr) = table.lookup(&suffix) {
            // 后缀此前已出现：写压缩指针（指针必须能放进 14 位）。
            if ptr > 0x3FFF {
                return Err(DnsError::MessageTooLong {
                    len: ptr,
                    max: 0x3FFF,
                });
            }
            let p = ptr as u16;
            out.extend_from_slice(&[(0xC0 | (p >> 8) as u8), (p & 0xFF) as u8]);
            return Ok(());
        }
        // 登记此后缀在当前输出中的起点，再输出标签。
        let pos = out.len();
        table.register(suffix, pos);
        let label = &name.labels[i];
        out.push(label.len() as u8);
        out.extend_from_slice(label);
    }
    // 根结尾。
    table.register_root(out.len());
    out.push(0);
    Ok(())
}

/// 名字后缀 -> 首次出现的绝对偏移 压缩表。
#[derive(Debug, Default)]
pub struct NameTable {
    map: HashMap<Name, usize>,
}

impl NameTable {
    pub fn new() -> Self {
        Self::default()
    }

    fn lookup(&self, suffix: &Name) -> Option<&usize> {
        self.map.get(suffix)
    }

    fn register(&mut self, suffix: Name, offset: usize) {
        // 只登记能被 14 位指针表达的偏移。
        if offset <= 0x3FFF {
            self.map.entry(suffix).or_insert(offset);
        }
    }

    fn register_root(&mut self, offset: usize) {
        if offset <= 0x3FFF {
            self.map.entry(Name::root()).or_insert(offset);
        }
    }
}
