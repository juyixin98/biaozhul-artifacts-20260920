//! 持久化单调序号分配器 (`next.idx`)。
//!
//! 为了严格满足"恢复后序号不得复用"——即使某条记录在字节落盘前就崩溃、
//! 连半截痕迹都没留下——序号在**写数据之前**就被持久化"认领":
//!
//! 1. `commit(seq + 1)` 把新的高水位写入 marker 并 `fsync`;
//! 2. 随后才写记录帧并 `fsync` 数据;
//! 3. 两步都成功才向客户端确认。
//!
//! 因此每条 append 需要两次 fsync (这是"连写前崩溃也不复用序号"的必然代价;
//! 吞吐优先的实现可用组提交摊销, 见 README)。
//!
//! # 双槽原子更新
//!
//! marker 文件固定两个 24 字节槽, 交替覆写:
//!
//! ```text
//! slot (24B) = version:u64 | next_seq:u64 | crc32:u32 | reserved:u32
//! ```
//!
//! 每次认领写"非活动槽"(version+1), 旧活动槽在新槽 fsync 完成前保持不变,
//! 所以覆写中途断电至少还有一个 CRC 合法的旧槽。恢复时取 CRC 合法且
//! version 最大的槽。CRC 覆盖 version||next_seq。

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::crc32;

pub const SLOT_LEN: usize = 24;
const FILE_LEN: usize = SLOT_LEN * 2;

/// 一个槽: version 单调递增; `next_seq` 是下一个待分配序号。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slot {
    pub version: u64,
    pub next_seq: u64,
}

fn encode_slot(slot: Slot) -> [u8; SLOT_LEN] {
    let mut buf = [0u8; SLOT_LEN];
    buf[0..8].copy_from_slice(&slot.version.to_be_bytes());
    buf[8..16].copy_from_slice(&slot.next_seq.to_be_bytes());
    let crc = crc32::crc32(&buf[0..16]);
    buf[16..20].copy_from_slice(&crc.to_be_bytes());
    // 20..24 保留为 0
    buf
}

fn decode_slot(buf: &[u8]) -> Option<Slot> {
    if buf.len() < SLOT_LEN {
        return None;
    }
    let version = u64::from_be_bytes(buf[0..8].try_into().unwrap());
    let next_seq = u64::from_be_bytes(buf[8..16].try_into().unwrap());
    let stored = u32::from_be_bytes(buf[16..20].try_into().unwrap());
    if crc32::crc32(&buf[0..16]) != stored {
        return None;
    }
    Some(Slot { version, next_seq })
}

/// 序号 marker。
pub struct Marker {
    file: File,
    /// 当前已持久化的活动槽 (version, next_seq)。
    live: Slot,
}

impl Marker {
    /// 打开目录 `dir` 下的 `next.idx`。不存在时不创建 (首次 commit 时创建)。
    ///
    /// 返回:
    /// * `Ok(None)`                —— 文件不存在, 日志必然为空, 起始序号为 1;
    /// * `Ok(Some((marker, next)))` —— 恢复出的下一序号;
    /// * `Err`                     —— marker 存在但没有任何合法槽 (损坏), 拒绝打开。
    pub fn open(dir: &Path) -> io::Result<Option<(Marker, u64)>> {
        let path = path(dir);
        let mut file = match OpenOptions::new().read(true).write(true).open(&path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };

        let mut buf = [0u8; FILE_LEN];
        let n = file.read(&mut buf)?;

        let slot0 = if n >= SLOT_LEN {
            decode_slot(&buf[0..SLOT_LEN])
        } else {
            None
        };
        let slot1 = if n >= FILE_LEN {
            decode_slot(&buf[SLOT_LEN..FILE_LEN])
        } else {
            None
        };

        let live = match (slot0, slot1) {
            (Some(a), Some(b)) => {
                // version 大的为新; 异常同号视为损坏。
                if a.version == b.version {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "next.idx corrupt: equal slot versions",
                    ));
                }
                if a.version > b.version { a } else { b }
            }
            (Some(a), None) => a,
            (None, Some(b)) => b,
            // 文件存在却连一个完整合法槽都没有。
            (None, None) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "next.idx corrupt: no valid slot",
                ));
            }
        };

        if live.next_seq == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "next.idx corrupt: next_seq is zero",
            ));
        }

        Ok(Some((Marker { file, live }, live.next_seq)))
    }

    /// 持久化认领: 声明下一个可用序号为 `next_seq`, fsync 成功才返回。
    ///
    /// `is_first` 为真时文件刚创建, 额外 fsync 目录以持久化目录项。
    pub fn commit(&mut self, next_seq: u64, dir: &Path, is_first: bool) -> io::Result<()> {
        let new_version = self.live.version.wrapping_add(1);
        // 写"另一个"槽: v1->槽0, v2->槽1, v3->槽0 ... 旧活动槽在 fsync
        // 完成前不动。
        let slot_index = new_version.wrapping_sub(1) % 2;
        let offset = slot_index * SLOT_LEN as u64;
        let encoded = encode_slot(Slot {
            version: new_version,
            next_seq,
        });

        self.file.seek(SeekFrom::Start(offset))?;
        self.file.write_all(&encoded)?;
        self.file.flush()?;
        self.file.sync_data()?;
        if is_first {
            let d = File::open(dir)?;
            d.sync_all()?;
        }
        self.live = Slot {
            version: new_version,
            next_seq,
        };
        Ok(())
    }
}

/// 首次创建 marker: 新建文件并写第一个槽。
pub fn create(dir: &Path, first_next: u64) -> io::Result<Marker> {
    let path = path(dir);
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&path)?;
    let encoded = encode_slot(Slot {
        version: 1,
        next_seq: first_next,
    });
    file.seek(SeekFrom::Start(0))?;
    file.write_all(&encoded)?;
    file.flush()?;
    file.sync_data()?;
    // 持久化 next.idx 的目录项。
    let d = File::open(dir)?;
    d.sync_all()?;
    Ok(Marker {
        file,
        live: Slot {
            version: 1,
            next_seq: first_next,
        },
    })
}

fn path(dir: &Path) -> PathBuf {
    dir.join("next.idx")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "seglog-marker-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn absent_means_fresh() {
        let dir = tempdir();
        assert!(Marker::open(&dir).unwrap().is_none());
    }

    #[test]
    fn create_and_reopen() {
        let dir = tempdir();
        {
            let _m = create(&dir, 2).unwrap();
        }
        let (_m, next) = Marker::open(&dir).unwrap().unwrap();
        assert_eq!(next, 2);
    }

    #[test]
    fn alternating_slots_advance() {
        let dir = tempdir();
        {
            let mut m = create(&dir, 2).unwrap();
            m.commit(3, &dir, false).unwrap();
            m.commit(4, &dir, false).unwrap();
            m.commit(5, &dir, false).unwrap();
        }
        let (_m, next) = Marker::open(&dir).unwrap().unwrap();
        assert_eq!(next, 5);
        // 文件保持双槽定长。
        let len = std::fs::metadata(dir.join("next.idx")).unwrap().len();
        assert_eq!(len, FILE_LEN as u64);
    }

    #[test]
    fn torn_slot_falls_back_to_older() {
        let dir = tempdir();
        {
            // create: slot0 = v1 next=1; 一次 commit: slot1 = v2 next=2 (活槽)。
            let mut m = create(&dir, 1).unwrap();
            m.commit(2, &dir, false).unwrap();
        }
        // 覆写"活槽"slot1 中途断电: 破坏 slot1 (偏移 24 起), 旧槽 slot0(v1,next=1)
        // 必须兜底, 而不是报错或采用损坏值。
        let p = dir.join("next.idx");
        let mut data = std::fs::read(&p).unwrap();
        assert!(data.len() >= SLOT_LEN * 2);
        data[SLOT_LEN] ^= 0xFF;
        std::fs::write(&p, &data).unwrap();

        let (_m, next) = Marker::open(&dir).unwrap().unwrap();
        assert_eq!(next, 1);
    }

    #[test]
    fn fully_garbage_is_rejected() {
        let dir = tempdir();
        std::fs::write(dir.join("next.idx"), vec![0xABu8; 30]).unwrap();
        assert!(Marker::open(&dir).is_err());
    }
}
