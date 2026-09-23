//! 表级格式：数据块序列 + 稀疏索引块 + 定长页脚。
//!
//! 页脚（32 字节，定长，位于文件末尾）：
//!
//! ```text
//! | index_offset u64 | index_size u64 | num_entries u64 | magic u64 |
//! ```
//!
//! 索引块本身也是一个块（restart_interval = 1，即每条目都是重启点），
//! 键为对应数据块的最后一个键（分隔键），值为 `varint(offset) ++ varint(size)`。
//!
//! 同步边界：`TableWriter::finish` 依次写入最后一个数据块、索引块、页脚，
//! 最后调用 `WriteSink::sync`；只有 sync 成功，表才被视为完整持久。
//! 读取侧打开时校验魔数与索引边界，每个块读取时校验 CRC，
//! `verify` 再做全量严格校验。

use crate::block::{BlockBuilder, BlockIter, BlockReader};
use crate::error::{Error, Result};
use crate::storage::{ReadAt, WriteSink};
use crate::varint::{get_uvarint, put_uvarint};

/// 文件魔数："SSTBLE01"。
pub const MAGIC: u64 = 0x5353_5442_4C45_3031;
/// 页脚定长字节数。
pub const FOOTER_SIZE: usize = 32;

#[derive(Clone, Copy, Debug)]
pub struct TableOptions {
    /// 数据块目标大小（字节），达到后切新块。
    pub block_size: usize,
    /// 每多少条目设置一个重启点。
    pub restart_interval: usize,
}

impl Default for TableOptions {
    fn default() -> Self {
        TableOptions {
            block_size: 4096,
            restart_interval: 16,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct TableStats {
    pub num_entries: u64,
    pub num_blocks: u64,
    pub file_size: u64,
}

// ---------------------------------------------------------------------------
// 写入器
// ---------------------------------------------------------------------------

/// 单次构建、构建后只读。键必须严格递增添加（空键合法且小于任何非空键）。
pub struct TableWriter<W: WriteSink> {
    sink: W,
    opts: TableOptions,
    block: BlockBuilder,
    index: BlockBuilder,
    offset: u64,
    last_key: Option<Vec<u8>>,
    num_entries: u64,
    num_blocks: u64,
    finished: bool,
}

impl<W: WriteSink> TableWriter<W> {
    pub fn new(sink: W, opts: TableOptions) -> Self {
        TableWriter {
            sink,
            opts,
            block: BlockBuilder::new(opts.restart_interval),
            index: BlockBuilder::new(1), // 索引块每条目一个重启点
            offset: 0,
            last_key: None,
            num_entries: 0,
            num_blocks: 0,
            finished: false,
        }
    }

    pub fn add(&mut self, key: &[u8], value: &[u8]) -> Result<()> {
        if self.finished {
            return Err(Error::bad_request("writer already finished"));
        }
        if let Some(last) = &self.last_key {
            if key <= last.as_slice() {
                return Err(Error::bad_request(
                    "keys must be added in strictly increasing order",
                ));
            }
        }
        self.block.add(key, value);
        self.last_key = Some(key.to_vec());
        self.num_entries += 1;
        if self.block.estimated_size() >= self.opts.block_size {
            self.flush_block()?;
        }
        Ok(())
    }

    fn flush_block(&mut self) -> Result<()> {
        if self.block.is_empty() {
            return Ok(());
        }
        let bytes = self.block.finish();
        // 分隔键 = 本块最后一个键
        let mut idx_val = Vec::new();
        put_uvarint(&mut idx_val, self.offset);
        put_uvarint(&mut idx_val, bytes.len() as u64);
        let sep = self.block.last_key().to_vec();
        self.index.add(&sep, &idx_val);
        self.sink.write_all(&bytes)?;
        self.offset += bytes.len() as u64;
        self.num_blocks += 1;
        self.block.reset();
        Ok(())
    }

    /// 写完索引与页脚并 sync。此后表才完整可见。
    pub fn finish(mut self) -> Result<TableStats> {
        if self.finished {
            return Err(Error::bad_request("writer already finished"));
        }
        self.flush_block()?;
        let index_bytes = self.index.finish();
        let index_offset = self.offset;
        self.sink.write_all(&index_bytes)?;
        self.offset += index_bytes.len() as u64;

        let mut footer = Vec::with_capacity(FOOTER_SIZE);
        footer.extend_from_slice(&index_offset.to_le_bytes());
        footer.extend_from_slice(&(index_bytes.len() as u64).to_le_bytes());
        footer.extend_from_slice(&self.num_entries.to_le_bytes());
        footer.extend_from_slice(&MAGIC.to_le_bytes());
        self.sink.write_all(&footer)?;
        self.offset += FOOTER_SIZE as u64;

        self.sink.sync()?;
        self.finished = true;
        Ok(TableStats {
            num_entries: self.num_entries,
            num_blocks: self.num_blocks,
            file_size: self.offset,
        })
    }
}

// ---------------------------------------------------------------------------
// 读取器
// ---------------------------------------------------------------------------

/// 严格校验报告。
#[derive(Debug)]
pub struct VerifyReport {
    pub num_blocks: usize,
    pub num_entries: u64,
    pub first_key: Option<Vec<u8>>,
    pub last_key: Option<Vec<u8>>,
    pub file_size: u64,
}

struct Footer {
    index_offset: u64,
    index_size: u64,
    num_entries: u64,
}

fn parse_footer(buf: &[u8]) -> Result<Footer> {
    debug_assert_eq!(buf.len(), FOOTER_SIZE);
    let index_offset = u64::from_le_bytes(buf[0..8].try_into().unwrap());
    let index_size = u64::from_le_bytes(buf[8..16].try_into().unwrap());
    let num_entries = u64::from_le_bytes(buf[16..24].try_into().unwrap());
    let magic = u64::from_le_bytes(buf[24..32].try_into().unwrap());
    if magic != MAGIC {
        return Err(Error::corruption(format!(
            "bad magic {magic:#018x}, expected {MAGIC:#018x} (not an sst file or truncated)"
        )));
    }
    Ok(Footer {
        index_offset,
        index_size,
        num_entries,
    })
}

pub struct TableReader<R: ReadAt> {
    r: R,    /// (分隔键, 块偏移, 块大小)
    index: Vec<(Vec<u8>, u64, u64)>,
    num_entries: u64,
    file_size: u64,
    index_offset: u64,
}

impl<R: ReadAt> std::fmt::Debug for TableReader<R> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TableReader")
            .field("num_entries", &self.num_entries)
            .field("num_blocks", &self.index.len())
            .field("file_size", &self.file_size)
            .finish()
    }
}

impl<R: ReadAt> TableReader<R> {
    /// 打开并做基本校验：长度、魔数、索引边界、索引块 CRC、索引条目可解码。
    pub fn open(r: R) -> Result<TableReader<R>> {
        let len = r.len()?;
        if len < FOOTER_SIZE as u64 {
            return Err(Error::corruption(format!(
                "file too small ({len} bytes) to contain a {}-byte footer",
                FOOTER_SIZE
            )));
        }
        let mut footer_buf = [0u8; FOOTER_SIZE];
        r.read_at(&mut footer_buf, len - FOOTER_SIZE as u64)?;
        let footer = parse_footer(&footer_buf)?;

        let data_end = len - FOOTER_SIZE as u64;
        let index_end = footer
            .index_offset
            .checked_add(footer.index_size)
            .ok_or_else(|| Error::corruption("index range overflows"))?;
        if index_end > data_end {
            return Err(Error::corruption(format!(
                "index [{}, {index_end}) extends past footer start {data_end}",
                footer.index_offset
            )));
        }

        let mut raw_index = vec![0u8; footer.index_size as usize];
        r.read_at(&mut raw_index, footer.index_offset)?;
        let index_block = BlockReader::parse(&raw_index)?;
        let mut index = Vec::new();
        for e in index_block.iter() {
            let (k, v) = e?;
            let (off, n1) = get_uvarint(&v)
                .ok_or_else(|| Error::corruption("index value: bad offset varint"))?;
            let (size, n2) = get_uvarint(&v[n1..])
                .ok_or_else(|| Error::corruption("index value: bad size varint"))?;
            if n1 + n2 != v.len() {
                return Err(Error::corruption("index value has trailing bytes"));
            }
            index.push((k, off, size));
        }

        Ok(TableReader {
            r,
            index,
            num_entries: footer.num_entries,
            file_size: len,
            index_offset: footer.index_offset,
        })
    }

    pub fn num_entries(&self) -> u64 {
        self.num_entries
    }
    pub fn num_blocks(&self) -> usize {
        self.index.len()
    }
    pub fn file_size(&self) -> u64 {
        self.file_size
    }

    fn read_block(&self, i: usize) -> Result<BlockReader> {
        let (_, off, size) = &self.index[i];
        let end = off
            .checked_add(*size)
            .ok_or_else(|| Error::corruption("block range overflows"))?;
        if end > self.index_offset {
            return Err(Error::corruption(format!(
                "block {i} [{off}, {end}) extends into index region (starts at {})",
                self.index_offset
            )));
        }
        let mut raw = vec![0u8; *size as usize];
        self.r.read_at(&mut raw, *off)?;
        BlockReader::parse(&raw)
    }

    /// 点查：返回 `Ok(None)` 表示键不存在。
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let i = self.index.partition_point(|(sep, _, _)| sep.as_slice() < key);
        if i == self.index.len() {
            return Ok(None);
        }
        let block = self.read_block(i)?;
        let it = block.seek(key)?;
        for item in it {
            let (k, v) = item?;
            match k.as_slice().cmp(key) {
                std::cmp::Ordering::Equal => return Ok(Some(v)),
                std::cmp::Ordering::Greater => return Ok(None),
                std::cmp::Ordering::Less => {}
            }
        }
        Ok(None)
    }

    /// 范围扫描 `[start, end)`；`end` 为 `None` 表示扫描到表尾。
    /// 惰性迭代，跨块自动前进；块损坏时产出一次 `Err` 后结束。
    pub fn scan(&self, start: &[u8], end: Option<&[u8]>) -> ScanIter<'_, R> {
        let first = self.index.partition_point(|(sep, _, _)| sep.as_slice() < start);
        ScanIter {
            reader: self,
            end: end.map(|e| e.to_vec()),
            first_block: first,
            next_block: first,
            current: None,
            start: start.to_vec(),
            finished: false,
        }
    }

    /// 全量严格校验。逐块检查 CRC、重启点、键序、索引分隔键与块边界，
    /// 并核对条目总数与页脚一致。
    pub fn verify(&self) -> Result<VerifyReport> {
        // 重新从底层读页脚与索引，确保校验不依赖 open 时的缓存结论
        let len = self.r.len()?;
        if len != self.file_size {
            return Err(Error::corruption(format!(
                "file size changed: open saw {}, now {len}",
                self.file_size
            )));
        }
        let mut footer_buf = [0u8; FOOTER_SIZE];
        self.r.read_at(&mut footer_buf, len - FOOTER_SIZE as u64)?;
        let footer = parse_footer(&footer_buf)?;
        if footer.index_offset + footer.index_size != len - FOOTER_SIZE as u64 {
            return Err(Error::corruption(
                "index block does not end exactly at footer start",
            ));
        }
        // 索引块严格校验（含键序）
        let mut raw_index = vec![0u8; footer.index_size as usize];
        self.r.read_at(&mut raw_index, footer.index_offset)?;
        let index_block = BlockReader::parse(&raw_index)?;
        index_block.verify_strict()?;

        let mut expected_off = 0u64;
        let mut total: u64 = 0;
        let mut prev_last: Option<Vec<u8>> = None;
        let mut first_key: Option<Vec<u8>> = None;
        let mut last_key: Option<Vec<u8>> = None;

        for (i, (sep, off, size)) in self.index.iter().enumerate() {
            if *off != expected_off {
                return Err(Error::corruption(format!(
                    "block {i} starts at {off}, expected {expected_off} (blocks must be contiguous)"
                )));
            }
            let block = self.read_block(i)?;
            let n = block.verify_strict()? as u64;

            let mut block_first: Option<Vec<u8>> = None;
            let mut block_last: Option<Vec<u8>> = None;
            let mut count = 0u64;
            for item in block.iter() {
                let (k, _) = item?;
                if block_first.is_none() {
                    block_first = Some(k.clone());
                }
                block_last = Some(k);
                count += 1;
            }
            if count != n {
                return Err(Error::corruption(format!(
                    "block {i}: strict verify counted {n} entries but iteration saw {count}"
                )));
            }
            if let (Some(bf), Some(bl)) = (&block_first, &block_last) {
                if let Some(p) = &prev_last {
                    if bf.as_slice() <= p.as_slice() {
                        return Err(Error::corruption(format!(
                            "block {i} first key not greater than previous block last key"
                        )));
                    }
                }
                if bl != sep {
                    return Err(Error::corruption(format!(
                        "block {i}: index separator does not match block last key"
                    )));
                }
                if first_key.is_none() {
                    first_key = Some(bf.clone());
                }
                last_key = Some(bl.clone());
                prev_last = Some(bl.clone());
            }
            expected_off += size;
            total += n;
        }

        if expected_off != self.index_offset {
            return Err(Error::corruption(format!(
                "data blocks end at {expected_off}, but index starts at {}",
                self.index_offset
            )));
        }
        if total != self.num_entries {
            return Err(Error::corruption(format!(
                "entry count mismatch: footer says {}, blocks contain {total}",
                self.num_entries
            )));
        }

        Ok(VerifyReport {
            num_blocks: self.index.len(),
            num_entries: total,
            first_key,
            last_key,
            file_size: len,
        })
    }
}

/// 跨块惰性范围迭代器。
pub struct ScanIter<'a, R: ReadAt> {
    reader: &'a TableReader<R>,
    end: Option<Vec<u8>>,
    first_block: usize,
    next_block: usize,
    current: Option<BlockIter>,
    start: Vec<u8>,
    finished: bool,
}

impl<'a, R: ReadAt> Iterator for ScanIter<'a, R> {
    type Item = Result<(Vec<u8>, Vec<u8>)>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if self.finished {
                return None;
            }
            if self.current.is_none() {
                if self.next_block >= self.reader.index.len() {
                    self.finished = true;
                    return None;
                }
                let block = match self.reader.read_block(self.next_block) {
                    Ok(b) => b,
                    Err(e) => {
                        self.finished = true;
                        return Some(Err(e));
                    }
                };
                let it = if self.next_block == self.first_block {
                    match block.seek(&self.start) {
                        Ok(it) => it,
                        Err(e) => {
                            self.finished = true;
                            return Some(Err(e));
                        }
                    }
                } else {
                    block.iter()
                };
                self.next_block += 1;
                self.current = Some(it);
            }
            let it = self.current.as_mut().unwrap();
            match it.next() {
                Some(Ok((k, v))) => {
                    if let Some(end) = &self.end {
                        if k.as_slice() >= end.as_slice() {
                            self.finished = true;
                            return None;
                        }
                    }
                    return Some(Ok((k, v)));
                }
                Some(Err(e)) => {
                    self.finished = true;
                    return Some(Err(e));
                }
                None => {
                    self.current = None; // 前进到下一块
                }
            }
        }
    }
}
