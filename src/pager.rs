//! 页式磁盘存储层。
//!
//! 文件布局：
//! - 0 号页固定为元数据页（[`Meta`]）
//! - 其余页为节点页；被释放的页通过每页前 4 字节串成单链空闲链表
//!
//! 本层只负责按页号读写，不理解节点内容（除空闲页头 4 字节外）。
//! 所有写操作都会 [`std::fs::File::sync_all`]（除非以 `sync = false` 打开），
//! 保证一次 upsert/delete 返回成功后数据已落盘。

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::node::PageId;

/// 元数据页页号。
pub const META_PAGE: PageId = 0;

/// 页大小下限。设到 64 字节可以让极小容量（叶 3 项 / 内部 2 键）
/// 在测试中频繁触发分裂、借位与合并。
pub const MIN_PAGE_SIZE: u16 = 64;

const MAGIC: u32 = 0x4250_5452; // "BPTR"
const META_VERSION: u16 = 1;

/// 元数据页内容（仅使用页头 32 字节）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Meta {
    /// 根节点页号；空树时为 [`crate::node::NULL_PAGE`]。
    pub root: PageId,
    /// 最左叶节点页号；空树时为 [`crate::node::NULL_PAGE`]。
    pub leftmost: PageId,
    /// 固定页大小（字节）。
    pub page_size: u16,
    /// 空闲链表头页号。
    pub free_head: PageId,
    /// 当前键值对数量（删除到 0 时清零校验用）。
    pub key_count: u64,
}

/// 磁盘分页器：持有一个可读写文件与固定页大小。
pub struct Pager {
    file: File,
    page_size: u16,
    free_head: PageId,
    /// 是否每次写后 fsync（测试中可关）。
    sync: bool,
}

impl Pager {
    /// 创建新数据库文件并写入初始元数据。
    /// `path` 已存在且非空时返回错误，避免误覆盖。
    pub fn create<P: AsRef<Path>>(path: P, page_size: u16) -> io::Result<Self> {
        if page_size < MIN_PAGE_SIZE {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("page_size 不能小于 {MIN_PAGE_SIZE}"),
            ));
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        let mut pager = Pager {
            file,
            page_size,
            free_head: crate::node::NULL_PAGE,
            sync: true,
        };
        // 将文件扩展到整页：0 号页固定为元数据页，后续页号从 1 开始。
        pager.file.set_len(page_size as u64)?;
        pager.write_meta(&Meta {
            root: crate::node::NULL_PAGE,
            leftmost: crate::node::NULL_PAGE,
            page_size,
            free_head: crate::node::NULL_PAGE,
            key_count: 0,
        })?;
        Ok(pager)
    }

    /// 打开已有数据库文件，读取并校验元数据。
    pub fn open<P: AsRef<Path>>(path: P, sync: bool) -> io::Result<Self> {
        let mut file = OpenOptions::new().read(true).write(true).open(path)?;
        let mut header = [0u8; 32];
        file.read_exact(&mut header)?;
        let meta = decode_meta(&header).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        Ok(Pager {
            file,
            page_size: meta.page_size,
            free_head: meta.free_head,
            sync,
        })
    }

    pub fn page_size(&self) -> u16 {
        self.page_size
    }

    /// 设置写页后是否 fsync（测试加速用）。
    pub fn set_sync(&mut self, sync: bool) {
        self.sync = sync;
    }

    pub fn free_head(&self) -> PageId {
        self.free_head
    }

    /// 读取整页到 `buf`（长度必须等于页大小）。
    pub fn read_page(&mut self, id: PageId, buf: &mut [u8]) -> io::Result<()> {
        debug_assert_eq!(buf.len(), self.page_size as usize);
        self.file.seek(SeekFrom::Start(page_offset(id, self.page_size)))?;
        self.file.read_exact(buf)
    }

    /// 写入整页。
    pub fn write_page(&mut self, id: PageId, buf: &[u8]) -> io::Result<()> {
        debug_assert_eq!(buf.len(), self.page_size as usize);
        self.file.seek(SeekFrom::Start(page_offset(id, self.page_size)))?;
        self.file.write_all(buf)?;
        if self.sync {
            self.file.sync_data()?;
        }
        Ok(())
    }

    /// 分配一页：优先取空闲链表，否则在文件尾部追加。
    /// 返回的页内容被清零。
    pub fn alloc_page(&mut self) -> io::Result<PageId> {
        let id = if self.free_head != crate::node::NULL_PAGE {
            let id = self.free_head;
            // 空闲页前 4 字节存链表后继。
            let mut hdr = [0u8; 4];
            self.file.seek(SeekFrom::Start(page_offset(id, self.page_size)))?;
            self.file.read_exact(&mut hdr)?;
            self.free_head = u32::from_le_bytes(hdr);
            id
        } else {
            let len = self.file.seek(SeekFrom::End(0))?;
            let ps = self.page_size as u64;
            let id = (len / ps) as PageId;
            self.file.set_len(len + ps)?; // 追加扩展一页
            id
        };
        let zeros = vec![0u8; self.page_size as usize];
        self.write_page(id, &zeros)?;
        Ok(id)
    }

    /// 释放一页：挂到空闲链表头部。
    pub fn free_page(&mut self, id: PageId) -> io::Result<()> {
        let mut buf = vec![0u8; self.page_size as usize];
        buf[..4].copy_from_slice(&self.free_head.to_le_bytes());
        self.write_page(id, &buf)?;
        self.free_head = id;
        Ok(())
    }

    /// 写回元数据（空闲链表头由 pager 内部状态决定）。
    pub fn write_meta(&mut self, meta: &Meta) -> io::Result<()> {
        self.free_head = meta.free_head;
        let mut buf = [0u8; 32];
        encode_meta(meta, &mut buf);
        self.file.seek(SeekFrom::Start(0))?;
        self.file.write_all(&buf)?;
        if self.sync {
            self.file.sync_all()?;
        }
        Ok(())
    }

    /// 读取元数据。
    pub fn read_meta(&mut self) -> io::Result<Meta> {
        let mut buf = [0u8; 32];
        self.file.seek(SeekFrom::Start(0))?;
        self.file.read_exact(&mut buf)?;
        decode_meta(&buf).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }
}

fn page_offset(id: PageId, page_size: u16) -> u64 {
    id as u64 * page_size as u64
}

fn encode_meta(m: &Meta, out: &mut [u8]) {
    out[0..4].copy_from_slice(&MAGIC.to_le_bytes());
    out[4..6].copy_from_slice(&META_VERSION.to_le_bytes());
    out[6..8].copy_from_slice(&m.page_size.to_le_bytes());
    out[8..12].copy_from_slice(&m.root.to_le_bytes());
    out[12..16].copy_from_slice(&m.leftmost.to_le_bytes());
    out[16..20].copy_from_slice(&m.free_head.to_le_bytes());
    out[20..28].copy_from_slice(&m.key_count.to_le_bytes());
}

fn decode_meta(buf: &[u8]) -> Result<Meta, String> {
    let magic = u32::from_le_bytes(buf[0..4].try_into().unwrap());
    if magic != MAGIC {
        return Err("不是有效的 B+ 树文件（magic 不匹配）".to_string());
    }
    let version = u16::from_le_bytes(buf[4..6].try_into().unwrap());
    if version != META_VERSION {
        return Err(format!("不支持的元数据版本 {version}"));
    }
    let page_size = u16::from_le_bytes(buf[6..8].try_into().unwrap());
    if page_size < MIN_PAGE_SIZE {
        return Err(format!("页大小 {page_size} 小于下限 {MIN_PAGE_SIZE}"));
    }
    Ok(Meta {
        page_size,
        root: u32::from_le_bytes(buf[8..12].try_into().unwrap()),
        leftmost: u32::from_le_bytes(buf[12..16].try_into().unwrap()),
        free_head: u32::from_le_bytes(buf[16..20].try_into().unwrap()),
        key_count: u64::from_le_bytes(buf[20..28].try_into().unwrap()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alloc_free_reuse() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.db");
        let mut p = Pager::create(&path, 64).unwrap();
        let a = p.alloc_page().unwrap();
        let b = p.alloc_page().unwrap();
        assert_eq!(a, 1);
        assert_eq!(b, 2);

        // 释放 a、b：链为 b -> a -> NULL；元数据记录链头 b。
        p.free_page(a).unwrap();
        p.free_page(b).unwrap();
        p.write_meta(&Meta {
            root: 0,
            leftmost: 0,
            page_size: 64,
            free_head: b,
            key_count: 0,
        })
        .unwrap();

        // LIFO 复用：先 b 后 a，再分配才是新页 3。
        assert_eq!(p.alloc_page().unwrap(), b);
        assert_eq!(p.alloc_page().unwrap(), a);
        let e = p.alloc_page().unwrap();
        assert_eq!(e, 3);
        let mut buf = [0u8; 64];
        p.read_page(e, &mut buf).unwrap();
        assert!(buf.iter().all(|&x| x == 0));
        drop(p);

        // 重开：页大小与已写数据可读。
        let mut p2 = Pager::open(&path, true).unwrap();
        assert_eq!(p2.page_size(), 64);
        let mut buf = [0u8; 64];
        p2.read_page(e, &mut buf).unwrap();
        assert!(buf.iter().all(|&x| x == 0));
    }

    #[test]
    fn reject_bad_page_size() {
        let dir = tempfile::tempdir().unwrap();
        match Pager::create(dir.path().join("x.db"), 32) {
            Err(e) => assert_eq!(e.kind(), io::ErrorKind::InvalidInput),
            Ok(_) => panic!("页大小 32 应被拒绝"),
        }
    }
}
