//! 确定性 ustar 写入器。
//!
//! 确定性规则（显式规范）：
//! - 所有条目 mtime 固定为 0（Unix epoch），uid=gid=0，uname/gname 为空；
//! - 权限由规则映射（见 pack 模块），不读取宿主文件权限的完整位；
//! - 条目顺序由调用方按规范化路径字节序排好后写入；
//! - 仅使用 ustar 格式（无 GNU/PAX 扩展头），超长路径用 prefix 字段拆分，
//!   无法拆分（任一段超限）时返回错误；
//! - 文件内容原样写入并按 512 字节块补零；归档以两个全零块结束。

use std::io::{self, Write};

const BLOCK: usize = 512;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryType {
    File,
    Dir,
    Symlink,
}

impl EntryType {
    fn typeflag(self) -> u8 {
        match self {
            EntryType::File => b'0',
            EntryType::Dir => b'5',
            EntryType::Symlink => b'2',
        }
    }
}

#[derive(Debug)]
pub struct TarEntryMeta<'a> {
    pub path: &'a str,
    pub entry_type: EntryType,
    pub mode: u32,
    pub size: u64,
    pub link_target: Option<&'a str>,
}

#[derive(Debug)]
pub enum TarError {
    NameTooLong(String),
    LinkNameTooLong(String),
    Io(io::Error),
}

impl std::fmt::Display for TarError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TarError::NameTooLong(p) => write!(f, "path too long for ustar: {p}"),
            TarError::LinkNameTooLong(p) => write!(f, "symlink target too long for ustar: {p}"),
            TarError::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for TarError {}

impl From<io::Error> for TarError {
    fn from(e: io::Error) -> Self {
        TarError::Io(e)
    }
}

/// 将 ustar 的 name/prefix 拆分出来。name ≤ 100，prefix ≤ 155。
fn split_name(path: &str) -> Result<(&str, &str), TarError> {
    let bytes = path.as_bytes();
    if bytes.len() <= 100 {
        return Ok((path, ""));
    }
    if bytes.len() > 255 {
        return Err(TarError::NameTooLong(path.to_string()));
    }
    // 从右往左找 '/'，使 name 段 ≤ 100 且 prefix 段 ≤ 155。
    for i in (0..bytes.len()).rev() {
        if bytes[i] == b'/' {
            let (prefix, name) = (&path[..i], &path[i + 1..]);
            if !name.is_empty() && name.len() <= 100 && prefix.len() <= 155 {
                return Ok((name, prefix));
            }
        }
    }
    Err(TarError::NameTooLong(path.to_string()))
}

fn write_octal(field: &mut [u8], value: u64) {
    // 末字节为 NUL，前面填八进制数字，左侧补 '0'。
    let width = field.len() - 1;
    let s = format!("{:0width$o}", value, width = width);
    let b = s.as_bytes();
    let start = width.saturating_sub(b.len());
    field[..start].fill(b'0');
    field[start..width].copy_from_slice(&b[b.len().saturating_sub(width)..]);
    field[width] = 0;
}

fn write_str(field: &mut [u8], s: &str) {
    let b = s.as_bytes();
    let n = b.len().min(field.len());
    field[..n].copy_from_slice(&b[..n]);
    // 其余保持 0
}

fn build_header(meta: &TarEntryMeta) -> Result<[u8; BLOCK], TarError> {
    let mut h = [0u8; BLOCK];
    let (name, prefix) = split_name(meta.path)?;
    write_str(&mut h[0..100], name);
    write_octal(&mut h[100..108], meta.mode as u64);
    write_octal(&mut h[108..116], 0); // uid
    write_octal(&mut h[116..124], 0); // gid
    write_octal(&mut h[124..136], meta.size);
    write_octal(&mut h[136..148], 0); // mtime = 0（固定时间戳）
    // 校验和字段先填空格
    h[148..156].fill(b' ');
    h[156] = meta.entry_type.typeflag();
    if let Some(target) = meta.link_target {
        if target.len() > 100 {
            return Err(TarError::LinkNameTooLong(target.to_string()));
        }
        write_str(&mut h[157..257], target);
    }
    write_str(&mut h[257..263], "ustar\0");
    h[263] = b'0';
    h[264] = b'0';
    // uname/gname 留空（全零），devmajor/devminor 写八进制 0
    write_octal(&mut h[329..337], 0);
    write_octal(&mut h[337..345], 0);
    write_str(&mut h[345..500], prefix);
    // 计算校验和
    let sum: u64 = h.iter().map(|&b| b as u64).sum();
    let s = format!("{:06o}\0 ", sum);
    h[148..156].copy_from_slice(s.as_bytes());
    Ok(h)
}

/// 确定性 tar 写入器。调用方负责按固定顺序写入条目。
pub struct TarWriter<W: Write> {
    w: W,
    finished: bool,
}

impl<W: Write> TarWriter<W> {
    pub fn new(w: W) -> Self {
        TarWriter { w, finished: false }
    }

    /// 写入一个条目（目录/符号链接的 data 传 &[]）。
    pub fn append(&mut self, meta: &TarEntryMeta, data: &[u8]) -> Result<(), TarError> {
        debug_assert_eq!(meta.size, data.len() as u64);
        let header = build_header(meta)?;
        self.w.write_all(&header)?;
        self.w.write_all(data)?;
        let rem = (BLOCK - (data.len() % BLOCK)) % BLOCK;
        if rem > 0 {
            self.w.write_all(&vec![0u8; rem])?;
        }
        Ok(())
    }

    /// 写入结束块并返回内部 writer。
    pub fn finish(mut self) -> Result<W, TarError> {
        if !self.finished {
            self.w.write_all(&[0u8; BLOCK])?;
            self.w.write_all(&[0u8; BLOCK])?;
            self.finished = true;
        }
        self.w.flush()?;
        Ok(self.w)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_deterministic_bytes() {
        let build = || {
            let mut tw = TarWriter::new(Vec::new());
            tw.append(
                &TarEntryMeta {
                    path: "a/b.txt",
                    entry_type: EntryType::File,
                    mode: 0o644,
                    size: 3,
                    link_target: None,
                },
                b"abc",
            )
            .unwrap();
            tw.finish().unwrap()
        };
        assert_eq!(build(), build());
    }

    #[test]
    fn splits_long_names() {
        let prefix = "p".repeat(120);
        let name = "n".repeat(90);
        let path = format!("{prefix}/{name}");
        let (n, p) = split_name(&path).unwrap();
        assert_eq!(n, name);
        assert_eq!(p, prefix);
        assert!(split_name(&"x".repeat(300)).is_err());
    }

    #[test]
    fn header_checksum_valid() {
        let meta = TarEntryMeta {
            path: "hello.txt",
            entry_type: EntryType::File,
            mode: 0o644,
            size: 5,
            link_target: None,
        };
        let h = build_header(&meta).unwrap();
        // 重新计算：校验和字段按空格计
        let mut tmp = h;
        tmp[148..156].fill(b' ');
        let sum: u64 = tmp.iter().map(|&b| b as u64).sum();
        let stored = std::str::from_utf8(&h[148..154]).unwrap();
        assert_eq!(stored, format!("{:06o}", sum));
    }
}
