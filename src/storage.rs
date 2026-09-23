//! 可注入 I/O 层。
//!
//! 表读写只依赖 `ReadAt` / `WriteSink` 两个 trait，因此测试可以：
//! - 用 `MemStorage` 完全在内存中往返；
//! - 用 `FaultyWriter` 注入写失败、同步失败、撕裂写（部分字节落盘）等故障。

use crate::error::{Error, Result};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::{Arc, Mutex};

/// 随机读抽象（表读取侧）。
pub trait ReadAt: Send + Sync {
    /// 从 `offset` 处精确读满 `buf`，不足则报错。
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()>;
    /// 文件当前总长度。
    fn len(&self) -> Result<u64>;
    /// 文件是否为空。
    fn is_empty(&self) -> Result<bool> {
        Ok(self.len()? == 0)
    }
}

/// 顺序写抽象（表构建侧）。
///
/// 同步边界约定：`write_all` 只保证字节进入内核/内存缓冲；
/// 只有 `sync` 成功返回后，此前写入的全部字节才被视为持久。
pub trait WriteSink: Send {
    fn write_all(&mut self, buf: &[u8]) -> Result<()>;
    fn sync(&mut self) -> Result<()>;
}

// ---------------------------------------------------------------------------
// 文件实现
// ---------------------------------------------------------------------------

pub struct FileReader {
    file: File,
    len: u64,
}

impl FileReader {
    pub fn open(path: impl AsRef<Path>) -> Result<FileReader> {
        let file = File::open(path)?;
        let len = file.metadata()?.len();
        Ok(FileReader { file, len })
    }
}

impl ReadAt for FileReader {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()> {
        use std::os::unix::fs::FileExt;
        self.file.read_exact_at(buf, offset)?;
        Ok(())
    }
    fn len(&self) -> Result<u64> {
        Ok(self.len)
    }
}

pub struct FileWriter {
    file: File,
}

impl FileWriter {
    /// 创建（或截断）目标文件。
    pub fn create(path: impl AsRef<Path>) -> Result<FileWriter> {
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(path)?;
        Ok(FileWriter { file })
    }
}

impl WriteSink for FileWriter {
    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        self.file.write_all(buf)?;
        Ok(())
    }
    fn sync(&mut self) -> Result<()> {
        self.file.sync_data()?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 内存实现（测试与演示用）
// ---------------------------------------------------------------------------

/// 线程安全的内存“文件”。克隆句柄指向同一份数据。
#[derive(Clone, Default)]
pub struct MemStorage {
    data: Arc<Mutex<Vec<u8>>>,
}

impl MemStorage {
    pub fn new() -> MemStorage {
        MemStorage::default()
    }

    /// 当前全部字节快照。
    pub fn bytes(&self) -> Vec<u8> {
        self.data.lock().unwrap().clone()
    }

    /// 直接修改底层字节（测试用来注入损坏）。
    pub fn mutate(&self, f: impl FnOnce(&mut Vec<u8>)) {
        let mut g = self.data.lock().unwrap();
        f(&mut g);
    }
}

impl ReadAt for MemStorage {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()> {
        let g = self.data.lock().unwrap();
        let start = offset as usize;
        let end = start
            .checked_add(buf.len())
            .ok_or_else(|| Error::corruption("read range overflows"))?;
        if end > g.len() {
            return Err(Error::corruption(format!(
                "read out of bounds: [{offset}, {end}) but file is {} bytes",
                g.len()
            )));
        }
        buf.copy_from_slice(&g[start..end]);
        Ok(())
    }
    fn len(&self) -> Result<u64> {
        Ok(self.data.lock().unwrap().len() as u64)
    }
}

impl WriteSink for MemStorage {
    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        self.data.lock().unwrap().extend_from_slice(buf);
        Ok(())
    }
    fn sync(&mut self) -> Result<()> {
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// 故障注入写包装器
// ---------------------------------------------------------------------------

/// 包装任意 `WriteSink`，按配置注入故障：
/// - `fail_on_call(n)`：第 n 次（0 起）`write_all` 返回 I/O 错误；
/// - `partial_on_call(n, k)`：第 n 次 `write_all` 只写入前 k 字节却报告成功（撕裂写）；
/// - `fail_sync(true)`：`sync` 返回 I/O 错误（模拟 fsync 失败）。
pub struct FaultyWriter<W: WriteSink> {
    inner: W,
    call: usize,
    pub fail_on_call: Option<usize>,
    pub partial_on_call: Option<(usize, usize)>,
    pub fail_sync: bool,
}

impl<W: WriteSink> FaultyWriter<W> {
    pub fn new(inner: W) -> Self {
        FaultyWriter {
            inner,
            call: 0,
            fail_on_call: None,
            partial_on_call: None,
            fail_sync: false,
        }
    }

    pub fn into_inner(self) -> W {
        self.inner
    }
}

impl<W: WriteSink> WriteSink for FaultyWriter<W> {
    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        let call = self.call;
        self.call += 1;
        if self.fail_on_call == Some(call) {
            return Err(Error::Io(std::io::Error::other(
                format!("injected write failure on call {call}"),
            )));
        }
        if let Some((n, k)) = self.partial_on_call {
            if n == call {
                let k = k.min(buf.len());
                // 撕裂写：只落前 k 字节，但对上层报告成功
                return self.inner.write_all(&buf[..k]);
            }
        }
        self.inner.write_all(buf)
    }

    fn sync(&mut self) -> Result<()> {
        if self.fail_sync {
            return Err(Error::Io(std::io::Error::other(
                "injected sync failure",
            )));
        }
        self.inner.sync()
    }
}
