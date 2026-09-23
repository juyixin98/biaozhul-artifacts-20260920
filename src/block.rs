//! 数据块：块内前缀压缩 + 重启点 + CRC32 校验。
//!
//! 块内字节布局（全部小端）：
//!
//! ```text
//! +--------------------------------------------------+
//! | entry 0 | entry 1 | ... | entry N-1              |  <- 条目区
//! | restart[0] (u32) ... restart[R-1] (u32)          |  <- 重启点偏移数组（相对块起始）
//! | num_restarts (u32)                               |
//! | crc32 (u32)  -- 覆盖此前全部字节                 |
//! +--------------------------------------------------+
//!
//! 单个 entry：
//!   shared_len   varint  与前一条键的公共前缀长度（重启点处必须为 0）
//!   unshared_len varint  键剩余部分长度
//!   value_len    varint
//!   key_delta    [u8; unshared_len]
//!   value        [u8; value_len]
//! ```

use crate::crc32::crc32;
use crate::error::{Error, Result};
use crate::varint::{get_uvarint, put_uvarint};

/// 块尾 CRC 字节数。
pub const BLOCK_CRC_SIZE: usize = 4;
/// 块尾固定部分（重启数组之外）：num_restarts(4) + crc(4)。
const BLOCK_FIXED_TRAILER: usize = 8;

fn common_prefix(a: &[u8], b: &[u8]) -> usize {
    let n = a.len().min(b.len());
    let mut i = 0;
    while i < n && a[i] == b[i] {
        i += 1;
    }
    i
}

/// 块构建器。调用方必须保证按键严格递增顺序添加。
pub struct BlockBuilder {
    restart_interval: usize,
    buf: Vec<u8>,
    restarts: Vec<u32>,
    counter: usize,
    last_key: Vec<u8>,
    num_entries: usize,
}

impl BlockBuilder {
    pub fn new(restart_interval: usize) -> Self {
        BlockBuilder {
            restart_interval: restart_interval.max(1),
            buf: Vec::new(),
            restarts: vec![0],
            counter: 0,
            last_key: Vec::new(),
            num_entries: 0,
        }
    }

    pub fn add(&mut self, key: &[u8], value: &[u8]) {
        let shared = if self.counter < self.restart_interval {
            common_prefix(&self.last_key, key)
        } else {
            // 到达重启点：记录偏移，不再压缩前缀
            self.restarts.push(self.buf.len() as u32);
            self.counter = 0;
            0
        };
        put_uvarint(&mut self.buf, shared as u64);
        put_uvarint(&mut self.buf, (key.len() - shared) as u64);
        put_uvarint(&mut self.buf, value.len() as u64);
        self.buf.extend_from_slice(&key[shared..]);
        self.buf.extend_from_slice(value);
        self.last_key.clear();
        self.last_key.extend_from_slice(key);
        self.counter += 1;
        self.num_entries += 1;
    }

    pub fn is_empty(&self) -> bool {
        self.num_entries == 0
    }

    pub fn num_entries(&self) -> usize {
        self.num_entries
    }

    /// 当前块内最后添加的键（调用方保证非空时使用）。
    pub fn last_key(&self) -> &[u8] {
        &self.last_key
    }

    /// 估算落盘后的块大小（含重启数组与 CRC）。
    pub fn estimated_size(&self) -> usize {
        self.buf.len() + self.restarts.len() * 4 + BLOCK_FIXED_TRAILER
    }

    /// 产出最终块字节（条目 + 重启数组 + 数量 + CRC）。
    pub fn finish(&self) -> Vec<u8> {
        let mut out = self.buf.clone();
        for r in &self.restarts {
            out.extend_from_slice(&r.to_le_bytes());
        }
        out.extend_from_slice(&(self.restarts.len() as u32).to_le_bytes());
        let crc = crc32(&out);
        out.extend_from_slice(&crc.to_le_bytes());
        out
    }

    pub fn reset(&mut self) {
        self.buf.clear();
        self.restarts.clear();
        self.restarts.push(0);
        self.counter = 0;
        self.last_key.clear();
        self.num_entries = 0;
    }
}

/// 只读块视图。构造时校验 CRC 与重启数组边界。
#[derive(Clone, Debug)]
pub struct BlockReader {
    data: Vec<u8>,       // 完整块字节（含 CRC）
    entries_end: usize,  // 条目区结束偏移（即重启数组起始）
    num_restarts: usize,
}

impl BlockReader {
    /// 解析并校验一个块（`raw` 含 CRC 尾部）。
    pub fn parse(raw: &[u8]) -> Result<BlockReader> {
        if raw.len() < BLOCK_FIXED_TRAILER + 4 {
            return Err(Error::corruption(format!(
                "block too small: {} bytes",
                raw.len()
            )));
        }
        let (body, crc_bytes) = raw.split_at(raw.len() - BLOCK_CRC_SIZE);
        let stored = u32::from_le_bytes(crc_bytes.try_into().unwrap());
        let actual = crc32(body);
        if actual != stored {
            return Err(Error::corruption(format!(
                "block crc mismatch: stored {stored:#010x}, computed {actual:#010x}"
            )));
        }
        let num = u32::from_le_bytes(body[body.len() - 4..].try_into().unwrap()) as usize;
        if num == 0 {
            return Err(Error::corruption("block has zero restart points"));
        }
        let restarts_bytes = num
            .checked_mul(4)
            .ok_or_else(|| Error::corruption("restart count overflows"))?;
        if body.len() < 4 + restarts_bytes {
            return Err(Error::corruption(format!(
                "restart array out of bounds: num_restarts={num}, block body={} bytes",
                body.len()
            )));
        }
        let entries_end = body.len() - 4 - restarts_bytes;
        Ok(BlockReader {
            data: raw.to_vec(),
            entries_end,
            num_restarts: num,
        })
    }

    pub fn num_restarts(&self) -> usize {
        self.num_restarts
    }

    fn restart_offset(&self, i: usize) -> usize {
        let off = self.entries_end + i * 4;
        u32::from_le_bytes(self.data[off..off + 4].try_into().unwrap()) as usize
    }

    /// 解码 `off` 处的条目标头，返回 (shared, unshared, value_len, header_len)。
    fn decode_header(&self, off: usize) -> Result<(usize, usize, usize, usize)> {
        let region = &self.data[..self.entries_end];
        if off >= self.entries_end {
            return Err(Error::corruption(format!(
                "entry offset {off} out of bounds (entries end at {})",
                self.entries_end
            )));
        }
        let (shared, n1) = get_uvarint(&region[off..])
            .ok_or_else(|| Error::corruption("bad varint: shared_len"))?;
        let (unshared, n2) = get_uvarint(&region[off + n1..])
            .ok_or_else(|| Error::corruption("bad varint: unshared_len"))?;
        let (vlen, n3) = get_uvarint(&region[off + n1 + n2..])
            .ok_or_else(|| Error::corruption("bad varint: value_len"))?;
        let hdr = n1 + n2 + n3;
        let (shared, unshared, vlen) = (shared as usize, unshared as usize, vlen as usize);
        // 边界检查（防溢出用 checked）
        let need = unshared
            .checked_add(vlen)
            .ok_or_else(|| Error::corruption("entry length overflow"))?;
        if off + hdr + need > self.entries_end {
            return Err(Error::corruption(format!(
                "entry at {off} overruns entries region (end {})",
                self.entries_end
            )));
        }
        Ok((shared, unshared, vlen, hdr))
    }

    /// 解码 `off` 处的完整条目。`prev_key` 为前一条目的完整键。
    /// 返回 (key, value, next_offset)。
    fn decode_entry_at(&self, off: usize, prev_key: &[u8]) -> Result<(Vec<u8>, Vec<u8>, usize)> {
        let (shared, unshared, vlen, hdr) = self.decode_header(off)?;
        if shared > prev_key.len() {
            return Err(Error::corruption(format!(
                "entry at {off}: shared prefix {shared} exceeds previous key length {}",
                prev_key.len()
            )));
        }
        let key_start = off + hdr;
        let val_start = key_start + unshared;
        let next = val_start + vlen;
        let mut key = Vec::with_capacity(shared + unshared);
        key.extend_from_slice(&prev_key[..shared]);
        key.extend_from_slice(&self.data[key_start..val_start]);
        let value = self.data[val_start..next].to_vec();
        Ok((key, value, next))
    }

    /// 解码重启点处的完整键（该处 shared 必须为 0）。
    fn restart_key(&self, restart_idx: usize) -> Result<Vec<u8>> {
        let off = self.restart_offset(restart_idx);
        let (shared, _, _, _) = self.decode_header(off)?;
        if shared != 0 {
            return Err(Error::corruption(format!(
                "restart point {restart_idx} at offset {off} has shared_len={shared}, expected 0"
            )));
        }
        let (key, _, _) = self.decode_entry_at(off, &[])?;
        Ok(key)
    }

    /// 从头开始的迭代器。
    pub fn iter(&self) -> BlockIter {
        BlockIter {
            block: self.clone(),
            next_offset: 0,
            prev_key: Vec::new(),
            done: false,
        }
    }

    /// 定位到第一个 `>= target` 的条目。
    /// 先对重启点二分，再在重启区间内线性前进。
    pub fn seek(&self, target: &[u8]) -> Result<BlockIter> {
        // 二分：找最大的满足 restart_key(i) <= target 的 i
        let mut lo = 0usize;
        let mut hi = self.num_restarts - 1;
        let mut ans = 0usize;
        while lo <= hi {
            let mid = (lo + hi) / 2;
            let k = self.restart_key(mid)?;
            if k.as_slice() <= target {
                ans = mid;
                lo = mid + 1;
            } else if mid == 0 {
                break;
            } else {
                hi = mid - 1;
            }
        }
        // 从重启点线性前进，直到键 >= target
        let mut off = self.restart_offset(ans);
        let mut prev_key: Vec<u8> = Vec::new();
        while off < self.entries_end {
            let (key, _, next) = self.decode_entry_at(off, &prev_key)?;
            if key.as_slice() >= target {
                return Ok(BlockIter {
                    block: self.clone(),
                    next_offset: off,
                    prev_key,
                    done: false,
                });
            }
            prev_key = key;
            off = next;
        }
        Ok(BlockIter {
            block: self.clone(),
            next_offset: self.entries_end,
            prev_key,
            done: true,
        })
    }

    /// 严格校验块内全部不变量，返回条目数。
    ///
    /// 检查项：
    /// - 每个条目可完整解码，shared_len 不超过前一键长度
    /// - 键严格递增
    /// - 条目区被条目恰好占满（无空洞、无越界）
    /// - 重启数组：首个为 0、严格递增、每个都落在条目边界上
    /// - 重启点处条目的 shared_len 必须为 0
    pub fn verify_strict(&self) -> Result<usize> {
        let mut entry_offsets = Vec::new();
        let mut off = 0usize;
        let mut prev_key: Vec<u8> = Vec::new();
        let mut prev: Option<Vec<u8>> = None;
        while off < self.entries_end {
            entry_offsets.push(off);
            let (key, _, next) = self.decode_entry_at(off, &prev_key)?;
            if let Some(p) = &prev {
                if key.as_slice() <= p.as_slice() {
                    return Err(Error::corruption(format!(
                        "keys not strictly increasing at entry offset {off}"
                    )));
                }
            }
            prev_key = key.clone();
            prev = Some(key);
            off = next;
        }
        // decode_entry_at 的边界检查保证 next <= entries_end；循环退出时必然相等
        debug_assert_eq!(off, self.entries_end);

        // 空块：恰好一个指向 0 的重启点
        if entry_offsets.is_empty() {
            if self.num_restarts != 1 || self.restart_offset(0) != 0 {
                return Err(Error::corruption(
                    "empty block must have exactly one restart point at offset 0",
                ));
            }
            return Ok(0);
        }

        if self.restart_offset(0) != 0 {
            return Err(Error::corruption(format!(
                "first restart offset is {}, expected 0",
                self.restart_offset(0)
            )));
        }
        for i in 1..self.num_restarts {
            let cur = self.restart_offset(i);
            let prev_r = self.restart_offset(i - 1);
            if cur <= prev_r {
                return Err(Error::corruption(format!(
                    "restart offsets not strictly increasing: restart[{i}]={cur} <= restart[{}]={prev_r}",
                    i - 1
                )));
            }
        }
        for i in 0..self.num_restarts {
            let r = self.restart_offset(i);
            match entry_offsets.binary_search(&r) {
                Ok(_) => {}
                Err(_) => {
                    return Err(Error::corruption(format!(
                        "restart offset {r} (restart[{i}]) does not point to an entry start"
                    )))
                }
            }
            // 重启点处 shared_len 必须为 0
            let (shared, _, _, _) = self.decode_header(r)?;
            if shared != 0 {
                return Err(Error::corruption(format!(
                    "restart[{i}] at offset {r} has shared_len={shared}, expected 0"
                )));
            }
        }
        Ok(entry_offsets.len())
    }
}

/// 拥有块数据的迭代器，按序产出 (key, value)。
/// 解码错误以 `Err` 产出一次后迭代结束，不会 panic。
pub struct BlockIter {
    block: BlockReader,
    next_offset: usize,
    prev_key: Vec<u8>,
    done: bool,
}

impl Iterator for BlockIter {
    type Item = Result<(Vec<u8>, Vec<u8>)>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done || self.next_offset >= self.block.entries_end {
            return None;
        }
        match self
            .block
            .decode_entry_at(self.next_offset, &self.prev_key)
        {
            Ok((key, value, next)) => {
                self.prev_key = key.clone();
                self.next_offset = next;
                Some(Ok((key, value)))
            }
            Err(e) => {
                self.done = true;
                Some(Err(e))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build(entries: &[(&[u8], &[u8])], restart_interval: usize) -> Vec<u8> {
        let mut b = BlockBuilder::new(restart_interval);
        for (k, v) in entries {
            b.add(k, v);
        }
        b.finish()
    }

    #[test]
    fn roundtrip_and_seek() {
        let entries: Vec<(Vec<u8>, Vec<u8>)> = (0..50)
            .map(|i| {
                (
                    format!("user/{:04}", i).into_bytes(),
                    format!("val-{}", i).into_bytes(),
                )
            })
            .collect();
        let mut b = BlockBuilder::new(4);
        for (k, v) in &entries {
            b.add(k, v);
        }
        let raw = b.finish();
        let r = BlockReader::parse(&raw).unwrap();
        assert_eq!(r.verify_strict().unwrap(), 50);

        // 全量迭代
        let got: Vec<_> = r.iter().collect::<Result<Vec<_>>>().unwrap();
        assert_eq!(got, entries);

        // seek 命中与未命中
        let it = r.seek(b"user/0010").unwrap();
        let (k, _) = it.take(1).next().unwrap().unwrap();
        assert_eq!(k, b"user/0010");

        let it = r.seek(b"user/0010x").unwrap();
        let (k, _) = it.take(1).next().unwrap().unwrap();
        assert_eq!(k, b"user/0011"); // 第一个 >= target

        let it = r.seek(b"zzz").unwrap();
        assert_eq!(it.count(), 0); // 超过最大键 → 空
    }

    #[test]
    fn empty_block() {
        let raw = build(&[], 4);
        let r = BlockReader::parse(&raw).unwrap();
        assert_eq!(r.verify_strict().unwrap(), 0);
        assert_eq!(r.iter().count(), 0);
    }

    #[test]
    fn empty_keys_and_values() {
        let entries: Vec<(&[u8], &[u8])> =
            vec![(b"", b""), (b"a", b""), (b"ab", b"v"), (b"b", b"")];
        let raw = build(&entries, 2);
        let r = BlockReader::parse(&raw).unwrap();
        assert_eq!(r.verify_strict().unwrap(), 4);
        let got: Vec<_> = r.iter().collect::<Result<Vec<_>>>().unwrap();
        let want: Vec<(Vec<u8>, Vec<u8>)> = entries
            .iter()
            .map(|(k, v)| (k.to_vec(), v.to_vec()))
            .collect();
        assert_eq!(got, want);
    }

    #[test]
    fn crc_mismatch_detected() {
        let raw = build(&[(b"k1", b"v1"), (b"k2", b"v2")], 1);
        let mut bad = raw.clone();
        bad[1] ^= 0xFF; // 翻转条目区一个字节
        let err = BlockReader::parse(&bad).unwrap_err();
        assert!(err.to_string().contains("crc"), "unexpected: {err}");
    }

    #[test]
    fn corrupt_restart_offset_detected_by_strict_verify() {
        // 构造含多个重启点的块，篡改 restart[1] 后重算 CRC，
        // 使 CRC 校验通过、但严格校验必须报重启点非法。
        let entries: Vec<(Vec<u8>, Vec<u8>)> = (0..10)
            .map(|i| (format!("k{:02}", i).into_bytes(), b"v".to_vec()))
            .collect();
        let mut b = BlockBuilder::new(2);
        for (k, v) in &entries {
            b.add(k, v);
        }
        let raw = b.finish();
        let len = raw.len();
        let num_restarts = u32::from_le_bytes(raw[len - 8..len - 4].try_into().unwrap()) as usize;
        assert!(num_restarts >= 2);
        let restarts_begin = len - 8 - num_restarts * 4;
        // restart[1] 改成指向条目区之外
        let mut bad = raw.clone();
        bad[restarts_begin + 4..restarts_begin + 8].copy_from_slice(&0xFFFFu32.to_le_bytes());
        let crc = crc32(&bad[..len - 4]);
        bad[len - 4..].copy_from_slice(&crc.to_le_bytes());

        let r = BlockReader::parse(&bad).unwrap(); // CRC 通过
        let err = r.verify_strict().unwrap_err();
        assert!(err.to_string().contains("restart"), "unexpected: {err}");
    }

    #[test]
    fn shared_len_exceeding_prev_key_detected() {
        // 手工编码一个非法块：首条目 shared_len=3（前一键为空，不可能共享 3 字节）
        let mut buf = Vec::new();
        put_uvarint(&mut buf, 3); // shared
        put_uvarint(&mut buf, 1); // unshared
        put_uvarint(&mut buf, 1); // vlen
        buf.push(b'x');
        buf.push(b'v');
        buf.extend_from_slice(&0u32.to_le_bytes()); // restarts[0] = 0
        buf.extend_from_slice(&1u32.to_le_bytes()); // num_restarts
        let crc = crc32(&buf);
        buf.extend_from_slice(&crc.to_le_bytes());

        let r = BlockReader::parse(&buf).unwrap();
        let err = r.verify_strict().unwrap_err();
        assert!(err.to_string().contains("shared prefix"), "unexpected: {err}");
    }
}
