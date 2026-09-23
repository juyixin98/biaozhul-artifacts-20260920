//! 可注入故障的 I/O 层。
//!
//! 两个后端实现同一组原语，存储引擎不感知底层是真实文件还是模拟磁盘：
//!
//! * [`RealStorage`]：真实文件系统（`File::write_all` + `sync_all` = fsync）；
//! * [`SimStorage`]：进程内“模拟磁盘”，显式区分**易失页缓存**与**稳定存储**，
//!   可按 `(目标文件, 操作, 第几次)` 精确注入断电 / 半页写。
//!
//! # 模拟磁盘的崩溃语义
//!
//! 每次 write 只修改该挂载实例的易失缓存；`sync` 才把缓存整体下发到稳定存储。
//! “断电”（[`FaultKind::CrashBefore`]）丢弃全部未同步缓存，稳定存储不变；
//! “半页写”（[`FaultKind::TearWrite`]）只让本次写入的前 N 个字节落到稳定存储，
//! 随后同样丢弃全部缓存——这正是掉电瞬间 4 KiB 页只持久化一部分的效果。
//! 重新 [`SimDisk::storage`] 即相当于重新挂载：缓存清空、计数归零。

use std::cell::RefCell;
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;

use crate::format::{PAGE_SIZE, SUPER_FILE_SIZE};

/// I/O 操作目标文件。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Target {
    /// 仅追加的数据文件 data.log。
    Data,
    /// 双页超级块文件 super.db。
    Super,
}

/// I/O 操作种类。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Op {
    /// 数据文件追加 / 超级块槽位整页写入。
    Write,
    /// fsync。
    Sync,
}

/// 注入的故障类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultKind {
    /// 操作执行前断电：所有未落盘缓存丢失，稳定存储保持原状。
    CrashBefore,
    /// 仅本次写入的前 `keep` 字节持久化，其余丢失，随后断电（半页写）。
    TearWrite(usize),
}

/// 一条故障规则：对 `target` 的第 `nth` 个 `op` 操作（每次挂载从 1 开始计数）
/// 注入 `kind`。规则一次性生效。
#[derive(Debug, Clone, Copy)]
pub struct FaultRule {
    pub target: Target,
    pub op: Op,
    pub nth: u64,
    pub kind: FaultKind,
}

impl FaultRule {
    pub fn crash(target: Target, op: Op, nth: u64) -> Self {
        FaultRule {
            target,
            op,
            nth,
            kind: FaultKind::CrashBefore,
        }
    }
    pub fn tear(target: Target, nth: u64, keep: usize) -> Self {
        FaultRule {
            target,
            op: Op::Write,
            nth,
            kind: FaultKind::TearWrite(keep),
        }
    }
}

/// 存储引擎依赖的 I/O 原语。一次提交中的调用顺序见模块级文档。
pub trait Storage {
    /// 读取数据文件全部字节（挂载时使用）。
    fn read_data(&self) -> io::Result<Vec<u8>>;
    /// 读取超级块文件全部字节（挂载时使用）。
    fn read_super(&self) -> io::Result<Vec<u8>>;
    /// 向数据文件末尾追加一次逻辑写入。
    fn append_data(&self, bytes: &[u8]) -> io::Result<()>;
    /// fsync 数据文件。
    fn sync_data(&self) -> io::Result<()>;
    /// 将整页（PAGE_SIZE）写入超级块指定槽位。
    fn write_super_slot(&self, slot: usize, page: &[u8]) -> io::Result<()>;
    /// fsync 超级块文件。
    fn sync_super(&self) -> io::Result<()>;
}

/// 模拟磁盘的稳定状态（可被多个“挂载实例”共享，模拟重挂载）。
struct SimDiskState {
    data: Vec<u8>,
    superfile: Vec<u8>,
}

/// 进程内模拟磁盘句柄。克隆廉价（内部引用计数）。
#[derive(Clone)]
pub struct SimDisk {
    inner: Rc<RefCell<SimDiskState>>,
}

impl SimDisk {
    /// 新建一块“已分区但未格式化”的空盘：数据文件为空，超级块文件为
    /// SUPER_FILE_SIZE 个零字节（零页对恢复而言是无效页）。
    pub fn fresh() -> Self {
        SimDisk {
            inner: Rc::new(RefCell::new(SimDiskState {
                data: Vec::new(),
                superfile: vec![0u8; SUPER_FILE_SIZE],
            })),
        }
    }

    /// 用给定故障规则重新挂载（旧挂载的缓存天然不可见）。
    pub fn storage(&self, rules: Vec<FaultRule>) -> SimStorage {
        for r in &rules {
            if matches!(r.kind, FaultKind::TearWrite(_)) && r.op != Op::Write {
                panic!("TearWrite 只能注入写操作，规则: {r:?}");
            }
        }
        SimStorage {
            disk: self.inner.clone(),
            rules: RefCell::new(rules.into_iter().map(|r| (r, false)).collect()),
            counts: RefCell::new(HashMap::new()),
            data_cache: RefCell::new(Vec::new()),
            super_cache: RefCell::new(None),
        }
    }

    // —— 供测试直接篡改稳定存储（模拟盘外介质损坏 / 人工造数）——

    pub fn stable_data(&self) -> Vec<u8> {
        self.inner.borrow().data.clone()
    }
    pub fn stable_super(&self) -> Vec<u8> {
        self.inner.borrow().superfile.clone()
    }
    pub fn set_stable_data(&self, data: Vec<u8>) {
        self.inner.borrow_mut().data = data;
    }
    pub fn set_stable_super(&self, superfile: Vec<u8>) {
        assert_eq!(superfile.len(), SUPER_FILE_SIZE);
        self.inner.borrow_mut().superfile = superfile;
    }
    /// 就地修改稳定存储字节。
    pub fn with_data<F: FnOnce(&mut Vec<u8>)>(&self, f: F) {
        f(&mut self.inner.borrow_mut().data);
    }
    pub fn with_super<F: FnOnce(&mut Vec<u8>)>(&self, f: F) {
        f(&mut self.inner.borrow_mut().superfile);
    }
}

/// 一次“挂载”：持有独立的易失缓存、操作计数与故障规则。
pub struct SimStorage {
    disk: Rc<RefCell<SimDiskState>>,
    rules: RefCell<Vec<(FaultRule, bool)>>,
    counts: RefCell<HashMap<(Target, Op), u64>>,
    data_cache: RefCell<Vec<u8>>,
    super_cache: RefCell<Option<(usize, Vec<u8>)>>,
}

/// 故障被触发时返回的 io::Error 内部错误，存储引擎据此与真实 I/O 错误区分。
#[derive(Debug)]
pub struct SimFaultError {
    pub target: Target,
    pub op: Op,
    pub nth: u64,
    pub kind: FaultKind,
}

impl std::fmt::Display for SimFaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "注入故障: {:?} 第 {} 个 {:?} -> {:?}",
            self.target, self.nth, self.op, self.kind
        )
    }
}
impl std::error::Error for SimFaultError {}

impl SimStorage {
    /// 计数并检查本操作是否命中故障规则；命中则返回故障类型。
    fn fire(&self, target: Target, op: Op) -> Option<FaultKind> {
        let nth = {
            let mut counts = self.counts.borrow_mut();
            let e = counts.entry((target, op)).or_insert(0);
            *e += 1;
            *e
        };
        let mut rules = self.rules.borrow_mut();
        for (rule, consumed) in rules.iter_mut() {
            if !*consumed && rule.target == target && rule.op == op && rule.nth == nth {
                *consumed = true;
                return Some(rule.kind);
            }
        }
        None
    }

    fn fault_err(&self, target: Target, op: Op, nth: u64, kind: FaultKind) -> io::Error {
        io::Error::other(SimFaultError {
            target,
            op,
            nth,
            kind,
        })
    }

    /// 断电：丢弃该挂载的全部易失缓存。
    fn power_loss(&self) {
        self.data_cache.borrow_mut().clear();
        *self.super_cache.borrow_mut() = None;
    }
}

impl Storage for SimStorage {
    fn read_data(&self) -> io::Result<Vec<u8>> {
        Ok(self.disk.borrow().data.clone())
    }

    fn read_super(&self) -> io::Result<Vec<u8>> {
        Ok(self.disk.borrow().superfile.clone())
    }

    fn append_data(&self, bytes: &[u8]) -> io::Result<()> {
        let nth = *self
            .counts
            .borrow()
            .get(&(Target::Data, Op::Write))
            .unwrap_or(&0)
            + 1;
        if let Some(kind) = self.fire(Target::Data, Op::Write) {
            match kind {
                FaultKind::CrashBefore => {
                    self.power_loss();
                    return Err(self.fault_err(Target::Data, Op::Write, nth, kind));
                }
                FaultKind::TearWrite(keep) => {
                    // 掉电瞬间：本次追加只有前 keep 字节落盘，缓存全丢。
                    let keep = keep.min(bytes.len());
                    self.power_loss();
                    let mut d = self.disk.borrow_mut();
                    d.data.extend_from_slice(&bytes[..keep]);
                    return Err(self.fault_err(Target::Data, Op::Write, nth, kind));
                }
            }
        }
        self.data_cache.borrow_mut().extend_from_slice(bytes);
        Ok(())
    }

    fn sync_data(&self) -> io::Result<()> {
        let nth = *self
            .counts
            .borrow()
            .get(&(Target::Data, Op::Sync))
            .unwrap_or(&0)
            + 1;
        if let Some(kind) = self.fire(Target::Data, Op::Sync) {
            self.power_loss();
            return Err(self.fault_err(Target::Data, Op::Sync, nth, kind));
        }
        let pending = std::mem::take(&mut *self.data_cache.borrow_mut());
        self.disk.borrow_mut().data.extend_from_slice(&pending);
        Ok(())
    }

    fn write_super_slot(&self, slot: usize, page: &[u8]) -> io::Result<()> {
        assert_eq!(page.len(), PAGE_SIZE, "超级块写入必须恰好一页");
        assert!(slot < 2);
        let nth = *self
            .counts
            .borrow()
            .get(&(Target::Super, Op::Write))
            .unwrap_or(&0)
            + 1;
        if let Some(kind) = self.fire(Target::Super, Op::Write) {
            match kind {
                FaultKind::CrashBefore => {
                    self.power_loss();
                    return Err(self.fault_err(Target::Super, Op::Write, nth, kind));
                }
                FaultKind::TearWrite(keep) => {
                    // 新页只落盘前 keep 字节（落在交替槽位，旧槽位不受影响）。
                    let keep = keep.min(page.len());
                    self.power_loss();
                    let mut d = self.disk.borrow_mut();
                    let pos = slot * PAGE_SIZE;
                    d.superfile[pos..pos + keep].copy_from_slice(&page[..keep]);
                    return Err(self.fault_err(Target::Super, Op::Write, nth, kind));
                }
            }
        }
        self.super_cache.replace(Some((slot, page.to_vec())));
        Ok(())
    }

    fn sync_super(&self) -> io::Result<()> {
        let nth = *self
            .counts
            .borrow()
            .get(&(Target::Super, Op::Sync))
            .unwrap_or(&0)
            + 1;
        if let Some(kind) = self.fire(Target::Super, Op::Sync) {
            self.power_loss();
            return Err(self.fault_err(Target::Super, Op::Sync, nth, kind));
        }
        if let Some((slot, page)) = self.super_cache.borrow_mut().take() {
            let pos = slot * PAGE_SIZE;
            let mut d = self.disk.borrow_mut();
            d.superfile[pos..pos + PAGE_SIZE].copy_from_slice(&page);
        }
        Ok(())
    }
}

// ============================ 真实文件后端 ============================

/// 真实文件系统后端：数据文件以 O_APPEND 打开，超级块文件随机定位整页写。
pub struct RealStorage {
    dir: PathBuf,
    data: RefCell<File>,
    superfile: RefCell<File>,
}

impl RealStorage {
    /// 在目录上挂载。要求目录与两个文件都已存在（先用 `format` 格式化）。
    pub fn open(dir: &Path) -> io::Result<Self> {
        let data_path = dir.join(crate::format::DATA_FILE);
        let super_path = dir.join(crate::format::SUPER_FILE);
        if !data_path.exists() || !super_path.exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "存储尚未格式化（缺少 data.log / super.db）",
            ));
        }
        // 数据文件必须以 append 打开（append 已隐含写权限）：挂载等价于新
        // 句柄，其初始偏移为 0，普通 write 会从文件头覆盖既有记录。O_APPEND
        // 保证每次 write 都原子地定位到当前末尾。超级块是定长随机写，不能 append。
        let data = OpenOptions::new()
            .read(true)
            .append(true)
            .open(&data_path)?;
        let superfile = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&super_path)?;
        Ok(RealStorage {
            dir: dir.to_path_buf(),
            data: RefCell::new(data),
            superfile: RefCell::new(superfile),
        })
    }

    /// 格式化：（重）建两个文件。数据文件截断为空；超级块文件先零填两页，
    /// 随后由存储引擎把代次 0 的空超级块写入槽位 0。
    pub fn create_files(dir: &Path) -> io::Result<()> {
        std::fs::create_dir_all(dir)?;
        let data = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(dir.join(crate::format::DATA_FILE))?;
        data.sync_all()?;
        let sf = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(dir.join(crate::format::SUPER_FILE))?;
        sf.set_len(SUPER_FILE_SIZE as u64)?;
        sf.sync_all()?;
        Ok(())
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

impl Storage for RealStorage {
    fn read_data(&self) -> io::Result<Vec<u8>> {
        let mut f = File::open(self.dir.join(crate::format::DATA_FILE))?;
        let mut v = Vec::new();
        f.read_to_end(&mut v)?;
        Ok(v)
    }

    fn read_super(&self) -> io::Result<Vec<u8>> {
        let mut f = File::open(self.dir.join(crate::format::SUPER_FILE))?;
        let mut v = Vec::new();
        f.read_to_end(&mut v)?;
        Ok(v)
    }

    fn append_data(&self, bytes: &[u8]) -> io::Result<()> {
        self.data.borrow_mut().write_all(bytes)
    }

    fn sync_data(&self) -> io::Result<()> {
        self.data.borrow().sync_all()
    }

    fn write_super_slot(&self, slot: usize, page: &[u8]) -> io::Result<()> {
        assert_eq!(page.len(), PAGE_SIZE);
        let mut f = self.superfile.borrow_mut();
        f.seek(SeekFrom::Start((slot * PAGE_SIZE) as u64))?;
        f.write_all(page)?;
        // 复位到末尾不是必须的：超级块从不追加。
        Ok(())
    }

    fn sync_super(&self) -> io::Result<()> {
        self.superfile.borrow().sync_all()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 未同步的写在“断电”后必须全部丢失；sync 后的必须保留。
    #[test]
    fn crash_loses_only_unsynced_bytes() {
        let disk = SimDisk::fresh();
        {
            let s = disk.storage(vec![]);
            s.append_data(b"aaa").unwrap();
            s.sync_data().unwrap();
            s.append_data(b"bbb").unwrap();
            // 不 sync 直接丢弃挂载实例。
        }
        let s2 = disk.storage(vec![]);
        assert_eq!(s2.read_data().unwrap(), b"aaa");
    }

    /// 半页写：只有前 keep 字节落盘。
    #[test]
    fn tear_write_persists_prefix_only() {
        let disk = SimDisk::fresh();
        let rule = FaultRule::tear(Target::Data, 1, 2);
        let s = disk.storage(vec![rule]);
        let err = s.append_data(b"abcdef").unwrap_err();
        assert!(err.get_ref().unwrap().is::<SimFaultError>());
        assert_eq!(disk.stable_data(), b"ab");
    }

    /// 断电于 fsync 之前：整次提交的数据都不落盘。
    #[test]
    fn crash_before_sync_loses_whole_batch() {
        let disk = SimDisk::fresh();
        let s = disk.storage(vec![FaultRule::crash(Target::Data, Op::Sync, 1)]);
        s.append_data(b"xyz").unwrap();
        assert!(s.sync_data().is_err());
        assert!(disk.stable_data().is_empty());
    }
}
