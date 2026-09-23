//! 定长页存储与页分配器。
//!
//! 磁盘文件按固定 `page_size` 切分为页：
//! - 0 号页为文件头（[`HEADER_SIZE`] 以内）。
//! - 其余页由分配器管理，回收的页串成单链表（空闲页首 8 字节存下一个空闲页 id）。
//!
//! 逻辑层（B+ 树）只通过 [`Pager`] trait 访问页，因此测试可以用 [`MemoryPager`]
//! 做无磁盘的高速差分测试。

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

pub type PageId = u64;

/// 空闲链表的哨兵值（0 号页是文件头，永远不会空闲）。
pub const NIL: PageId = 0;

pub const MAGIC: [u8; 8] = *b"BPTREE01";
pub const HEADER_SIZE: usize = 64;

// 文件头字段偏移
const OFF_MAGIC: usize = 0;
const OFF_VERSION: usize = 8;
const OFF_PAGE_SIZE: usize = 12;
const OFF_ROOT: usize = 16;
const OFF_NUM_PAGES: usize = 24;
const OFF_FREE_HEAD: usize = 32;

pub const VERSION: u32 = 1;

/// 页存储抽象：定长页的随机读写 + 页分配/回收。
pub trait Pager {
    fn page_size(&self) -> usize;
    fn read_page(&self, id: PageId, buf: &mut [u8]);
    fn write_page(&mut self, id: PageId, buf: &[u8]);
    fn alloc_page(&mut self) -> PageId;
    fn free_page(&mut self, id: PageId);
    fn num_pages(&self) -> u64;
    fn root_page(&self) -> PageId;
    fn set_root_page(&mut self, id: PageId);
    /// 把文件头与脏数据落盘。
    fn sync(&mut self) -> std::io::Result<()>;
}

/// 分配器的公共状态，具体读写由内部小 trait 提供。
struct AllocState {
    page_size: usize,
    root: PageId,
    num_pages: u64,
    free_head: PageId,
}

/// 底层页介质：按页号读写，必要时扩容。
trait Backing {
    fn read_raw(&self, id: PageId, buf: &mut [u8]);
    fn write_raw(&mut self, id: PageId, buf: &[u8]);
    fn grow_one(&mut self, id: PageId, page_size: usize);
    fn sync_backing(&mut self) -> std::io::Result<()>;
}

fn load_header(backing: &dyn Backing) -> (u32, AllocState) {
    let mut hdr = vec![0u8; HEADER_SIZE];
    backing.read_raw(0, &mut hdr);
    if hdr[OFF_MAGIC..OFF_MAGIC + 8] != MAGIC {
        panic!("不是合法的 bptree 数据文件（magic 不匹配）");
    }
    let version = u32::from_le_bytes(hdr[OFF_VERSION..OFF_VERSION + 4].try_into().unwrap());
    let page_size =
        u32::from_le_bytes(hdr[OFF_PAGE_SIZE..OFF_PAGE_SIZE + 4].try_into().unwrap()) as usize;
    let root = u64::from_le_bytes(hdr[OFF_ROOT..OFF_ROOT + 8].try_into().unwrap());
    let num_pages = u64::from_le_bytes(hdr[OFF_NUM_PAGES..OFF_NUM_PAGES + 8].try_into().unwrap());
    let free_head = u64::from_le_bytes(hdr[OFF_FREE_HEAD..OFF_FREE_HEAD + 8].try_into().unwrap());
    (
        version,
        AllocState {
            page_size,
            root,
            num_pages,
            free_head,
        },
    )
}

impl AllocState {
    fn header_bytes(&self) -> Vec<u8> {
        let mut hdr = vec![0u8; HEADER_SIZE];
        hdr[OFF_MAGIC..OFF_MAGIC + 8].copy_from_slice(&MAGIC);
        hdr[OFF_VERSION..OFF_VERSION + 4].copy_from_slice(&VERSION.to_le_bytes());
        hdr[OFF_PAGE_SIZE..OFF_PAGE_SIZE + 4]
            .copy_from_slice(&(self.page_size as u32).to_le_bytes());
        hdr[OFF_ROOT..OFF_ROOT + 8].copy_from_slice(&self.root.to_le_bytes());
        hdr[OFF_NUM_PAGES..OFF_NUM_PAGES + 8].copy_from_slice(&self.num_pages.to_le_bytes());
        hdr[OFF_FREE_HEAD..OFF_FREE_HEAD + 8].copy_from_slice(&self.free_head.to_le_bytes());
        hdr
    }
}

/// 通用分配逻辑（文件 / 内存共用）。
struct PagerInner<B: Backing> {
    backing: B,
    st: AllocState,
    header_dirty: bool,
}

impl<B: Backing> PagerInner<B> {
    fn flush_header(&mut self) {
        let hdr = self.st.header_bytes();
        self.backing.write_raw(0, &hdr);
        self.header_dirty = false;
    }
}

impl<B: Backing> Pager for PagerInner<B> {
    fn page_size(&self) -> usize {
        self.st.page_size
    }

    fn read_page(&self, id: PageId, buf: &mut [u8]) {
        assert!(id != 0 && id < self.st.num_pages, "读取越界页 {id}");
        assert_eq!(buf.len(), self.st.page_size);
        self.backing.read_raw(id, buf);
    }

    fn write_page(&mut self, id: PageId, buf: &[u8]) {
        assert!(id != 0 && id < self.st.num_pages, "写入越界页 {id}");
        assert_eq!(buf.len(), self.st.page_size);
        self.backing.write_raw(id, buf);
    }

    fn alloc_page(&mut self) -> PageId {
        if self.st.free_head != NIL {
            let id = self.st.free_head;
            let mut buf = vec![0u8; self.st.page_size];
            self.backing.read_raw(id, &mut buf);
            self.st.free_head = u64::from_le_bytes(buf[0..8].try_into().unwrap());
            // 新交付的页清零，避免残留数据干扰。
            buf.fill(0);
            self.backing.write_raw(id, &buf);
            self.header_dirty = true;
            id
        } else {
            let id = self.st.num_pages;
            self.backing.grow_one(id, self.st.page_size);
            self.st.num_pages += 1;
            self.header_dirty = true;
            id
        }
    }

    fn free_page(&mut self, id: PageId) {
        assert!(id != 0 && id < self.st.num_pages, "释放越界页 {id}");
        assert!(id != self.st.root, "不能释放根页");
        let mut buf = vec![0u8; self.st.page_size];
        buf[0..8].copy_from_slice(&self.st.free_head.to_le_bytes());
        self.backing.write_raw(id, &buf);
        self.st.free_head = id;
        self.header_dirty = true;
    }

    fn num_pages(&self) -> u64 {
        self.st.num_pages
    }

    fn root_page(&self) -> PageId {
        self.st.root
    }

    fn set_root_page(&mut self, id: PageId) {
        self.st.root = id;
        self.header_dirty = true;
    }

    fn sync(&mut self) -> std::io::Result<()> {
        if self.header_dirty {
            self.flush_header();
        }
        self.backing.sync_backing()
    }
}

// ---------------- 磁盘文件介质 ----------------

struct FileBacking {
    file: File,
}

impl Backing for FileBacking {
    fn read_raw(&self, id: PageId, buf: &mut [u8]) {
        let ps = buf.len() as u64;
        let mut file = &self.file;
        file.seek(SeekFrom::Start(id * ps)).expect("seek");
        file.read_exact(buf).expect("read page");
    }

    fn write_raw(&mut self, id: PageId, buf: &[u8]) {
        let ps = buf.len() as u64;
        self.file.seek(SeekFrom::Start(id * ps)).expect("seek");
        self.file.write_all(buf).expect("write page");
    }

    fn grow_one(&mut self, id: PageId, page_size: usize) {
        let zeros = vec![0u8; page_size];
        self.file
            .seek(SeekFrom::Start(id * page_size as u64))
            .expect("seek");
        self.file.write_all(&zeros).expect("extend");
    }

    fn sync_backing(&mut self) -> std::io::Result<()> {
        self.file.flush()?;
        self.file.sync_data()
    }
}

/// 磁盘文件支持的 B+ 树页管理器。
pub struct FilePager(PagerInner<FileBacking>);

impl FilePager {
    /// 创建新数据库文件（要求路径不存在），并初始化根叶页。
    pub fn create(path: impl AsRef<Path>, page_size: usize) -> std::io::Result<Self> {
        assert!(page_size >= 64, "页大小至少 64 字节");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        let backing = FileBacking { file };
        let mut inner = PagerInner {
            backing,
            st: AllocState {
                page_size,
                root: NIL,
                num_pages: 1, // 0 号页即文件头
                free_head: NIL,
            },
            header_dirty: false,
        };
        inner.flush_header();
        let root = inner.alloc_page();
        // 交付页已清零，按叶节点写一次头。
        let mut buf = vec![0u8; page_size];
        buf[0] = super::node::KIND_LEAF;
        inner.write_page(root, &buf);
        inner.set_root_page(root);
        inner.sync()?;
        Ok(FilePager(inner))
    }

    /// 打开已存在的数据库文件。
    pub fn open(path: impl AsRef<Path>) -> std::io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let backing = FileBacking { file };
        let (version, st) = load_header(&backing);
        assert_eq!(version, VERSION, "不支持的文件版本 {version}");
        let mut inner = PagerInner {
            backing,
            st,
            header_dirty: false,
        };
        // 打开即回写一次头，保证文件完整。
        inner.flush_header();
        Ok(FilePager(inner))
    }
}

impl Pager for FilePager {
    fn page_size(&self) -> usize {
        self.0.page_size()
    }
    fn read_page(&self, id: PageId, buf: &mut [u8]) {
        self.0.read_page(id, buf)
    }
    fn write_page(&mut self, id: PageId, buf: &[u8]) {
        self.0.write_page(id, buf)
    }
    fn alloc_page(&mut self) -> PageId {
        self.0.alloc_page()
    }
    fn free_page(&mut self, id: PageId) {
        self.0.free_page(id)
    }
    fn num_pages(&self) -> u64 {
        self.0.num_pages()
    }
    fn root_page(&self) -> PageId {
        self.0.root_page()
    }
    fn set_root_page(&mut self, id: PageId) {
        self.0.set_root_page(id)
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.0.sync()
    }
}

// ---------------- 内存介质（测试用） ----------------

struct MemBacking {
    pages: Vec<Vec<u8>>,
    page_size: usize,
}

impl Backing for MemBacking {
    fn read_raw(&self, id: PageId, buf: &mut [u8]) {
        buf.copy_from_slice(&self.pages[id as usize]);
    }
    fn write_raw(&mut self, id: PageId, buf: &[u8]) {
        self.pages[id as usize].copy_from_slice(buf);
    }
    fn grow_one(&mut self, _id: PageId, _page_size: usize) {
        self.pages.push(vec![0u8; self.page_size]);
    }
    fn sync_backing(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// 纯内存页管理器，供单元/差分测试使用。
pub struct MemoryPager(PagerInner<MemBacking>);

impl MemoryPager {
    pub fn new(page_size: usize) -> Self {
        assert!(page_size >= 64, "页大小至少 64 字节");
        let backing = MemBacking {
            pages: vec![vec![0u8; HEADER_SIZE.min(page_size)]],
            page_size,
        };
        let mut inner = PagerInner {
            backing,
            st: AllocState {
                page_size,
                root: NIL,
                num_pages: 1,
                free_head: NIL,
            },
            header_dirty: false,
        };
        inner.flush_header();
        let root = inner.alloc_page();
        let mut buf = vec![0u8; page_size];
        buf[0] = super::node::KIND_LEAF;
        inner.write_page(root, &buf);
        inner.set_root_page(root);
        let _ = inner.sync();
        MemoryPager(inner)
    }
}

impl Pager for MemoryPager {
    fn page_size(&self) -> usize {
        self.0.page_size()
    }
    fn read_page(&self, id: PageId, buf: &mut [u8]) {
        self.0.read_page(id, buf)
    }
    fn write_page(&mut self, id: PageId, buf: &[u8]) {
        self.0.write_page(id, buf)
    }
    fn alloc_page(&mut self) -> PageId {
        self.0.alloc_page()
    }
    fn free_page(&mut self, id: PageId) {
        self.0.free_page(id)
    }
    fn num_pages(&self) -> u64 {
        self.0.num_pages()
    }
    fn root_page(&self) -> PageId {
        self.0.root_page()
    }
    fn set_root_page(&mut self, id: PageId) {
        self.0.set_root_page(id)
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.0.sync()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alloc_reuses_freed_pages() {
        let mut p = MemoryPager::new(64);
        let a = p.alloc_page();
        let b = p.alloc_page();
        p.free_page(a);
        p.free_page(b);
        assert_eq!(p.alloc_page(), b, "应从自由表头取最近释放的页");
        assert_eq!(p.alloc_page(), a);
        let c = p.alloc_page();
        assert!(c > b, "自由链取空后继续扩容");
        let _ = c;
    }

    #[test]
    fn written_page_survives_reread() {
        let mut p = MemoryPager::new(128);
        let id = p.alloc_page();
        let mut buf = vec![7u8; 128];
        buf[0] = 9;
        p.write_page(id, &buf);
        let mut got = vec![0u8; 128];
        p.read_page(id, &mut got);
        assert_eq!(got, buf);
    }
}
