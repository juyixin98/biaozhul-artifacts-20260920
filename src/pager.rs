//! 磁盘分页布局与底层 `Pager`。
//!
//! 单个索引文件布局（每页固定 4096 字节）：
//!
//! ```text
//! 页 0                  索引头（global depth、桶容量、哈希模式、空闲链表头……）
//! 页 1                  事务日志元数据
//! 页 2                  事务日志描述符表（分裂链中每个最终桶一项）
//! 页 3 .. 26            事务日志镜像区（最多 24 个最终桶页快照）
//! 页 27 .. 1050         目录区（最多 2^20 个 u32 槽，4 MiB）
//! 页 1051 ..            桶页（固定大小，按桶 id 线性寻址）
//! ```
//!
//! 所有可变页尾部 4 字节存放 CRC-32，覆盖页内其余字节，用于检测撕裂写/损坏。

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::errors::{IndexError, Result};

pub const PAGE_SIZE: usize = 4096;
pub const CRC_SIZE: usize = 4;

/// 全局深度上限：目录最多 2^20 个槽（4 MiB）。
pub const MAX_GLOBAL_DEPTH: u8 = 20;

pub const HEADER_PAGE: u64 = 0;
pub const JOURNAL_META_PAGE: u64 = 1;
pub const JOURNAL_DESC_PAGE: u64 = 2;
/// 第一个镜像页；镜像共 [`JOURNAL_IMAGE_PAGES`] 页。
pub const JOURNAL_IMAGE_FIRST: u64 = 3;
/// 镜像页数（单次分裂链最多产生的最终桶数，即链长上界+1）。
pub const JOURNAL_IMAGE_PAGES: u64 = 24;
pub const DIR_FIRST_PAGE: u64 = JOURNAL_IMAGE_FIRST + JOURNAL_IMAGE_PAGES; // 27
/// 目录页数（1024 槽/页 × 1024 页 = 2^20 槽）。
pub const DIR_PAGES: u64 = 1 << (MAX_GLOBAL_DEPTH - 10);
pub const FIRST_BUCKET_PAGE: u64 = DIR_FIRST_PAGE + DIR_PAGES;

pub(crate) const HEADER_MAGIC: u32 = 0x4548_4944; // "EHID"
pub(crate) const BUCKET_MAGIC: u32 = 0x4255_434B; // "BUCK"
pub(crate) const FREE_MAGIC: u32 = 0x4652_4545; // "FREE"
pub(crate) const JOURNAL_MAGIC: u32 = 0x4A4F_5552; // "JOUR"
pub(crate) const FORMAT_VERSION: u8 = 1;

/// 桶页内容区固定起点（前 48 字节为桶头）。
pub const BUCKET_HEADER_LEN: usize = 48;

/// 底层分页器：拥有索引文件，按页读写。所有写都由上层在合适时机 `sync`。
pub struct Pager {
    file: File,
}

impl Pager {
    /// 创建新索引文件（文件必须不存在），写入零页使稀疏文件可寻址。
    pub fn create(path: &Path) -> Result<Pager> {
        if path.exists() {
            return Err(IndexError::Corrupt(format!(
                "索引文件已存在：{}（如需打开请用 open）",
                path.display()
            )));
        }
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        // 预锚定文件起点；桶页/目录页按需写（稀疏文件）。
        file.set_len(0)?;
        Ok(Pager { file })
    }

    pub fn open(path: &Path) -> Result<Pager> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        Ok(Pager { file })
    }

    /// 读取整页到 `buf`（长度必须为 [`PAGE_SIZE`]）。未写过的洞读为零。
    pub fn read_page(&mut self, page: u64, buf: &mut [u8]) -> Result<()> {
        assert_eq!(buf.len(), PAGE_SIZE);
        self.file.seek(SeekFrom::Start(page * PAGE_SIZE as u64))?;
        // 稀疏文件尾部可能短读，用 read_exact 会报错；逐次读并补零。
        let mut filled = 0;
        while filled < PAGE_SIZE {
            match self.file.read(&mut buf[filled..]) {
                Ok(0) => break,
                Ok(n) => filled += n,
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e.into()),
            }
        }
        if filled < PAGE_SIZE {
            buf[filled..].fill(0);
        }
        Ok(())
    }

    /// 写整页（不含 fsync，由上层决定持久化边界）。
    pub fn write_page(&mut self, page: u64, buf: &[u8]) -> Result<()> {
        assert_eq!(buf.len(), PAGE_SIZE);
        self.file.seek(SeekFrom::Start(page * PAGE_SIZE as u64))?;
        self.file.write_all(buf)?;
        Ok(())
    }

    pub fn sync(&mut self) -> Result<()> {
        self.file.sync_all()?;
        Ok(())
    }

    /// 桶 id -> 桶页号。
    pub fn bucket_page(bucket_id: u32) -> u64 {
        FIRST_BUCKET_PAGE + bucket_id as u64
    }

    /// 目录槽号 -> (目录页号, 页内字节偏移)。
    pub fn dir_slot_loc(slot: usize) -> (u64, usize) {
        let byte = slot * 4;
        (DIR_FIRST_PAGE + (byte / PAGE_SIZE) as u64, byte % PAGE_SIZE)
    }
}
