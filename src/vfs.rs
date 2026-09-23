//! 可注入的文件 I/O 层。
//!
//! 存储库逻辑只依赖 [`Vfs`] trait，因此测试中可以：
//! - 用 [`RealVfs`] 跑真实磁盘（含 fsync 语义）；
//! - 用带钩子的 [`MemVfs`]（[`MemVfs::with_hook`]）配合 [`FaultPolicy`] 模拟 I/O 错误与
//!   “断电崩溃”（崩溃 = journal 回到最后 fsync 的长度，其它未 sync 的新块句柄失效），
//!   验证 WAL 提交边界与崩溃恢复。

use std::collections::BTreeMap;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// 文件句柄抽象（读写、定位、同步）。
pub trait VfsFile: Read + Write + Seek {
    fn sync_all(&mut self) -> io::Result<()>;
    /// 截断/扩展文件到指定长度（journal 截断、真实 FS 上对应 `set_len`）。
    fn set_len(&mut self, size: u64) -> io::Result<()>;
    fn len(&mut self) -> io::Result<u64> {
        let pos = self.stream_position()?;
        let end = self.seek(SeekFrom::End(0))?;
        self.seek(SeekFrom::Start(pos))?;
        Ok(end)
    }
}

/// 存储库需要的最小文件系统操作集。路径均为相对仓库根的相对路径。
pub trait Vfs: Send + Sync {
    /// 创建缺失的目录（含父目录）。
    fn mkdirs(&self, rel: &str) -> io::Result<()>;
    /// 路径是否存在（文件或目录）。
    fn exists(&self, rel: &str) -> bool;
    /// 以可读可写方式打开已存在文件（不创建）。
    fn open(&self, rel: &str) -> io::Result<Box<dyn VfsFile>>;
    /// 创建新文件（截断），父目录需已存在。
    fn create(&self, rel: &str) -> io::Result<Box<dyn VfsFile>>;
    /// 删除文件。
    fn remove(&self, rel: &str) -> io::Result<()>;
    /// 原子改名（用于 manifest 提交）。
    fn rename(&self, from: &str, to: &str) -> io::Result<()>;
    /// 列出目录下文件名（不递归）。
    fn list(&self, rel: &str) -> io::Result<Vec<String>>;
}

// ---------------- 真实文件系统 ----------------

/// 基于 `std::fs` 的实现，仓库根目录 [`RealVfs::root`]。
pub struct RealVfs {
    root: PathBuf,
}

impl RealVfs {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    fn path(&self, rel: &str) -> PathBuf {
        let p = Path::new(rel);
        assert!(
            !p.is_absolute() && p.components().all(|c| c != std::path::Component::ParentDir),
            "vfs path escapes root: {rel}"
        );
        self.root.join(p)
    }
}

impl Vfs for RealVfs {
    fn mkdirs(&self, rel: &str) -> io::Result<()> {
        std::fs::create_dir_all(self.path(rel))
    }
    fn exists(&self, rel: &str) -> bool {
        self.path(rel).exists()
    }
    fn open(&self, rel: &str) -> io::Result<Box<dyn VfsFile>> {
        Ok(Box::new(RealFile(
            std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(self.path(rel))?,
        )))
    }
    fn create(&self, rel: &str) -> io::Result<Box<dyn VfsFile>> {
        Ok(Box::new(RealFile(
            std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(true)
                .open(self.path(rel))?,
        )))
    }
    fn remove(&self, rel: &str) -> io::Result<()> {
        std::fs::remove_file(self.path(rel))
    }
    fn rename(&self, from: &str, to: &str) -> io::Result<()> {
        std::fs::rename(self.path(from), self.path(to))
    }
    fn list(&self, rel: &str) -> io::Result<Vec<String>> {
        let mut out = Vec::new();
        for e in std::fs::read_dir(self.path(rel))? {
            if let Some(name) = e?.file_name().to_str() {
                out.push(name.to_string());
            }
        }
        Ok(out)
    }
}

struct RealFile(std::fs::File);

impl Read for RealFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl Write for RealFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl Seek for RealFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.0.seek(pos)
    }
}
impl VfsFile for RealFile {
    fn sync_all(&mut self) -> io::Result<()> {
        self.0.sync_all()
    }
    fn set_len(&mut self, size: u64) -> io::Result<()> {
        self.0.set_len(size)
    }
}

// ---------------- 内存文件系统（可挂故障钩子） ----------------

type Hook = Arc<dyn Fn(VfsOp) -> io::Result<()> + Send + Sync>;

/// [`MemVfs`] 操作类别，钩子可据此在特定文件/动作上注入错误。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VfsOp<'a> {
    Create { path: &'a str },
    Open { path: &'a str },
    Write { path: &'a str },
    Sync { path: &'a str },
    Rename { from: &'a str, to: &'a str },
    Remove { path: &'a str },
}

#[derive(Clone)]
enum HookEvent {
    Create(String),
    Open(String),
    Rename(String, String),
    Remove(String),
}

impl HookEvent {
    fn op(&self) -> VfsOp<'_> {
        match self {
            HookEvent::Create(p) => VfsOp::Create { path: p },
            HookEvent::Open(p) => VfsOp::Open { path: p },
            HookEvent::Rename(f, t) => VfsOp::Rename { from: f, to: t },
            HookEvent::Remove(p) => VfsOp::Remove { path: p },
        }
    }
}

type SharedFs = Arc<Mutex<BTreeMap<String, Vec<u8>>>>;
type SharedDirs = Arc<Mutex<BTreeMap<String, ()>>>;
type SharedHook = Arc<Mutex<Option<Hook>>>;

/// 纯内存文件系统。写入在 `write()` 时即对共享存储生效（无页缓存建模）；
/// `sync_all` 是持久化 barrier，崩溃模拟时通过钩子把文件截断到“最后 sync 的长度”。
#[derive(Clone)]
pub struct MemVfs {
    files: SharedFs,
    dirs: SharedDirs,
    hook: SharedHook,
    /// 每个文件最后一次 sync 时的长度；崩溃后恢复到这些长度。
    sync_len: Arc<Mutex<BTreeMap<String, u64>>>,
}

impl MemVfs {
    pub fn new() -> Self {
        let mut dirs = BTreeMap::new();
        dirs.insert(".".to_string(), ());
        Self {
            files: Arc::new(Mutex::new(BTreeMap::new())),
            dirs: Arc::new(Mutex::new(dirs)),
            hook: Arc::new(Mutex::new(None)),
            sync_len: Arc::new(Mutex::new(BTreeMap::new())),
        }
    }

    /// 挂载操作钩子。钩子返回 Err 时该操作失败（但不影响已写入的共享状态）。
    pub fn with_hook<F>(self, hook: F) -> Self
    where
        F: Fn(VfsOp) -> io::Result<()> + Send + Sync + 'static,
    {
        *self.hook.lock().unwrap() = Some(Arc::new(hook));
        self
    }

    fn fire(&self, ev: &HookEvent) -> io::Result<()> {
        let h = self.hook.lock().unwrap();
        if let Some(f) = h.as_ref() {
            f(ev.op())
        } else {
            Ok(())
        }
    }

    /// 模拟断电：把所有文件截断回最后 `sync_all` 时的长度（未 sync 的尾部丢失），
    /// 并删除从未 sync 过的文件。
    pub fn simulate_crash(&self) {
        let mut fs = self.files.lock().unwrap();
        let lens = self.sync_len.lock().unwrap();
        let mut to_remove = Vec::new();
        for (path, data) in fs.iter_mut() {
            match lens.get(path) {
                Some(&len) => data.truncate(len as usize),
                None => to_remove.push(path.clone()),
            }
        }
        for p in to_remove {
            fs.remove(&p);
        }
    }

    /// 测试辅助：直接读取文件内容（拷贝）。
    pub fn snapshot(&self, rel: &str) -> Option<Vec<u8>> {
        self.files.lock().unwrap().get(rel).cloned()
    }
}

impl Default for MemVfs {
    fn default() -> Self {
        Self::new()
    }
}

struct MemFile {
    fs: SharedFs,
    name: String,
    pos: u64,
    on_sync: Arc<dyn Fn(&str) + Send + Sync>,
    on_write: Arc<dyn Fn(&str) -> io::Result<()> + Send + Sync>,
}

impl Read for MemFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let fs = self.fs.lock().unwrap();
        let data = fs.get(&self.name).map(|v| v.as_slice()).unwrap_or(&[][..]);
        if self.pos as usize >= data.len() {
            return Ok(0);
        }
        let n = buf.len().min(data.len() - self.pos as usize);
        buf[..n].copy_from_slice(&data[self.pos as usize..self.pos as usize + n]);
        self.pos += n as u64;
        Ok(n)
    }
}

impl Write for MemFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        (self.on_write)(&self.name)?;
        let mut fs = self.fs.lock().unwrap();
        let data = fs.entry(self.name.clone()).or_default();
        let start = self.pos as usize;
        if start > data.len() {
            data.resize(start, 0);
        }
        let end = start + buf.len();
        if end > data.len() {
            data.resize(end, 0);
        }
        data[start..end].copy_from_slice(buf);
        self.pos += buf.len() as u64;
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
impl Seek for MemFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        let fs = self.fs.lock().unwrap();
        let len = fs.get(&self.name).map(|v| v.len() as u64).unwrap_or(0);
        let new = match pos {
            SeekFrom::Start(n) => n as i64,
            SeekFrom::Current(n) => self.pos as i64 + n,
            SeekFrom::End(n) => len as i64 + n,
        };
        if new < 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "negative seek"));
        }
        self.pos = new as u64;
        Ok(self.pos)
    }
}
impl VfsFile for MemFile {
    fn sync_all(&mut self) -> io::Result<()> {
        (self.on_sync)(&self.name);
        Ok(())
    }
    fn set_len(&mut self, size: u64) -> io::Result<()> {
        self.fs
            .lock()
            .unwrap()
            .entry(self.name.clone())
            .or_default()
            .resize(size as usize, 0);
        if self.pos > size {
            self.pos = size;
        }
        Ok(())
    }
    fn len(&mut self) -> io::Result<u64> {
        Ok(self
            .fs
            .lock()
            .unwrap()
            .get(&self.name)
            .map(|v| v.len() as u64)
            .unwrap_or(0))
    }
}

impl Vfs for MemVfs {
    fn mkdirs(&self, rel: &str) -> io::Result<()> {
        self.dirs.lock().unwrap().insert(rel.to_string(), ());
        Ok(())
    }
    fn exists(&self, rel: &str) -> bool {
        self.files.lock().unwrap().contains_key(rel) || self.dirs.lock().unwrap().contains_key(rel)
    }
    fn open(&self, rel: &str) -> io::Result<Box<dyn VfsFile>> {
        self.fire(&HookEvent::Open(rel.to_string()))?;
        if !self.files.lock().unwrap().contains_key(rel) {
            return Err(io::Error::new(io::ErrorKind::NotFound, rel.to_string()));
        }
        let fs = self.files.clone();
        let name = rel.to_string();
        let on_sync = {
            let sync_len = self.sync_len.clone();
            Arc::new(move |p: &str| {
                let len = fs
                    .lock()
                    .unwrap()
                    .get(p)
                    .map(|v| v.len() as u64)
                    .unwrap_or(0);
                sync_len.lock().unwrap().insert(p.to_string(), len);
            })
        };
        let hook = self.hook.clone();
        let on_write = Arc::new(move |p: &str| -> io::Result<()> {
            let h = hook.lock().unwrap();
            if let Some(f) = h.as_ref() {
                f(VfsOp::Write { path: p })?;
            }
            Ok(())
        });
        Ok(Box::new(MemFile {
            fs: self.files.clone(),
            name,
            pos: 0,
            on_sync,
            on_write,
        }))
    }
    fn create(&self, rel: &str) -> io::Result<Box<dyn VfsFile>> {
        self.fire(&HookEvent::Create(rel.to_string()))?;
        self.files
            .lock()
            .unwrap()
            .insert(rel.to_string(), Vec::new());
        self.sync_len.lock().unwrap().remove(rel);
        let fs = self.files.clone();
        let name = rel.to_string();
        let on_sync = {
            let sync_len = self.sync_len.clone();
            Arc::new(move |p: &str| {
                let len = fs
                    .lock()
                    .unwrap()
                    .get(p)
                    .map(|v| v.len() as u64)
                    .unwrap_or(0);
                sync_len.lock().unwrap().insert(p.to_string(), len);
            })
        };
        let hook = self.hook.clone();
        let on_write = Arc::new(move |p: &str| -> io::Result<()> {
            let h = hook.lock().unwrap();
            if let Some(f) = h.as_ref() {
                f(VfsOp::Write { path: p })?;
            }
            Ok(())
        });
        Ok(Box::new(MemFile {
            fs: self.files.clone(),
            name,
            pos: 0,
            on_sync,
            on_write,
        }))
    }
    fn remove(&self, rel: &str) -> io::Result<()> {
        self.fire(&HookEvent::Remove(rel.to_string()))?;
        self.files.lock().unwrap().remove(rel);
        self.sync_len.lock().unwrap().remove(rel);
        Ok(())
    }
    fn rename(&self, from: &str, to: &str) -> io::Result<()> {
        self.fire(&HookEvent::Rename(from.to_string(), to.to_string()))?;
        let mut fs = self.files.lock().unwrap();
        let data = fs
            .remove(from)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, from.to_string()))?;
        fs.insert(to.to_string(), data);
        // rename 是元数据原子操作：目标继承源的“已持久化长度”，
        // 否则崩溃模拟会把刚 fsync 并提升的块当成“从未 sync 的新文件”删掉。
        let mut lens = self.sync_len.lock().unwrap();
        if let Some(len) = lens.remove(from) {
            lens.insert(to.to_string(), len);
        } else {
            lens.remove(to);
        }
        Ok(())
    }
    fn list(&self, rel: &str) -> io::Result<Vec<String>> {
        let prefix = if rel == "." {
            String::new()
        } else {
            format!("{rel}/")
        };
        let fs = self.files.lock().unwrap();
        let mut names: Vec<String> = fs
            .keys()
            .filter_map(|k| {
                if k.starts_with(&prefix) {
                    let rest = &k[prefix.len()..];
                    if !rest.is_empty() && !rest.contains('/') {
                        Some(rest.to_string())
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .collect();
        names.sort();
        Ok(names)
    }
}

// ---------------- 故障点与策略 ----------------

/// 故障点：与仓库提交流程中的关键阶段一一对应（语义见 store.rs 提交流程注释）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FaultPoint {
    /// 写 chunk 暂存块文件
    ChunkWrite,
    /// 暂存块 fsync
    ChunkSync,
    /// 追加 journal 帧体
    JournalAppend,
    /// journal fsync（提交记录落盘点）
    JournalSync,
    /// 暂存块提升为正式块
    PromoteChunk,
    /// 写 manifest.tmp
    ManifestWrite,
    /// manifest.tmp fsync
    ManifestSync,
    /// manifest rename
    ManifestRename,
    /// 删除/截断 journal
    JournalTruncate,
    /// journal 截断后 fsync
    JournalTruncateSync,
}

impl FaultPoint {
    pub fn all() -> [FaultPoint; 10] {
        use FaultPoint::*;
        [
            ChunkWrite,
            ChunkSync,
            JournalAppend,
            JournalSync,
            PromoteChunk,
            ManifestWrite,
            ManifestSync,
            ManifestRename,
            JournalTruncate,
            JournalTruncateSync,
        ]
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FaultKind {
    /// 返回 I/O 错误但不丢数据（进程存活，调用方收到错误后重开恢复）。
    IoError,
    /// 模拟断电：返回错误，并把文件系统截断到最后 sync 状态。
    Crash,
}

#[derive(Debug, Clone)]
struct Rule {
    remaining: u32,
    kind: FaultKind,
}

/// 故障注入策略（线程安全共享）。每个故障点可配置“第 N 次命中时故障”。
#[derive(Default)]
pub struct FaultPolicy {
    rules: std::collections::HashMap<FaultPoint, Rule>,
    /// 每个故障点累计命中次数。
    pub fired: std::collections::HashMap<FaultPoint, u32>,
}

impl std::fmt::Debug for FaultPolicy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FaultPolicy")
            .field("rules", &self.rules)
            .field("fired", &self.fired)
            .finish()
    }
}

impl FaultPolicy {
    pub fn new() -> Self {
        Self::default()
    }
    /// 在故障点 `at` 第 `times` 次到达时注入普通 I/O 错误（默认第 1 次）。
    pub fn io_error(mut self, at: FaultPoint, times: u32) -> Self {
        self.rules.insert(
            at,
            Rule {
                remaining: times,
                kind: FaultKind::IoError,
            },
        );
        self
    }
    /// 在故障点 `at` 第 `times` 次到达时模拟崩溃。
    pub fn crash(mut self, at: FaultPoint, times: u32) -> Self {
        self.rules.insert(
            at,
            Rule {
                remaining: times,
                kind: FaultKind::Crash,
            },
        );
        self
    }

    /// 命中故障点。返回 Ok 表示放行；Err 表示注入的故障。
    /// 对 `Crash` 类型，调用 `on_crash` 回调（由测试装配为 [`MemVfs::simulate_crash`]）。
    pub(crate) fn hit(&mut self, at: FaultPoint, on_crash: &dyn Fn()) -> io::Result<()> {
        let count = self.fired.entry(at).or_insert(0);
        *count += 1;
        if let Some(r) = self.rules.get_mut(&at) {
            if r.remaining > 0 {
                r.remaining -= 1;
                return match r.kind {
                    FaultKind::IoError => Err(io::Error::new(
                        io::ErrorKind::Other,
                        format!("injected I/O error at {at:?}"),
                    )),
                    FaultKind::Crash => {
                        on_crash();
                        Err(io::Error::new(
                            io::ErrorKind::Other,
                            format!("injected crash at {at:?}"),
                        ))
                    }
                };
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mem_vfs_basic_io() {
        let fs: Arc<dyn Vfs> = Arc::new(MemVfs::new());
        fs.mkdirs("chunks").unwrap();
        let mut f = fs.create("chunks/0").unwrap();
        f.write_all(b"hello").unwrap();
        f.sync_all().unwrap();
        drop(f);
        assert!(fs.exists("chunks/0"));
        let mut f = fs.open("chunks/0").unwrap();
        f.seek(SeekFrom::End(0)).unwrap();
        assert_eq!(f.stream_position().unwrap(), 5);
        assert_eq!(fs.list("chunks").unwrap(), vec!["0".to_string()]);
    }

    #[test]
    fn crash_truncates_unsynced_tail() {
        let mem = MemVfs::new();
        let fs: Arc<dyn Vfs> = Arc::new(mem.clone());
        let mut f = fs.create("journal").unwrap();
        f.write_all(b"committed-part").unwrap();
        f.sync_all().unwrap();
        f.write_all(b"torn-tail").unwrap(); // 未 sync
        drop(f);
        let mut g = fs.create("brand-new").unwrap();
        g.write_all(b"unsynced new file").unwrap(); // 整个文件未 sync
        drop(g);

        mem.simulate_crash();

        assert_eq!(mem.snapshot("journal").unwrap(), b"committed-part");
        assert!(mem.snapshot("brand-new").is_none());
    }

    #[test]
    fn hook_injects_write_error() {
        let mem = MemVfs::new().with_hook(|op| match op {
            VfsOp::Write { path } if path == "x" => {
                Err(io::Error::new(io::ErrorKind::Other, "disk full"))
            }
            _ => Ok(()),
        });
        let fs: Arc<dyn Vfs> = Arc::new(mem);
        let mut f = fs.create("x").unwrap();
        assert!(f.write_all(b"a").is_err());
    }

    #[test]
    fn real_vfs_roundtrip() {
        let tmp = std::env::temp_dir().join(format!("imerkle-vfs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let fs = RealVfs::new(&tmp);
        fs.mkdirs("chunks").unwrap();
        let mut f = fs.create("chunks/1").unwrap();
        f.write_all(b"abc").unwrap();
        f.sync_all().unwrap();
        drop(f);
        assert!(fs.exists("chunks/1"));
        fs.rename("chunks/1", "chunks/2").unwrap();
        assert!(!fs.exists("chunks/1"));
        assert!(fs.exists("chunks/2"));
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
