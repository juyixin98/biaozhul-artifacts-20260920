//! The sorted table: write path (`TableBuilder`), read path (`Table`),
//! two-level sparse index lookup, range scans and whole-file validation.
//!
//! ## Synchronization boundaries
//!
//! Data blocks are appended as they fill, but bytes on disk are not a
//! committed table until [`TableBuilder::finish`] returns: it writes the
//! metaindex, index and footer, then issues exactly one `sync_all`. The
//! file-based helper [`build_table_sorted`] additionally writes to a temp
//! name, renames over the final name and syncs the parent directory, so a
//! table file appears atomically and a failed build leaves no final file.

use std::fs::{self, File};
use std::path::Path;

use crate::block::{self, Block, BlockBuilder};
use crate::coding;
use crate::error::{Error, Result};
use crate::format::{
    self, BlockHandle, Footer, BLOCK_TRAILER_LEN, BLOCK_TYPE_DATA, FOOTER_LEN, MAX_BLOCK_SIZE,
};
use crate::io::{append_all, read_fill, PosixReader, PosixWriter, RandomAccessFile, WritableFile};

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

/// Builder options.
#[derive(Debug, Clone)]
pub struct Options {
    /// Flush a data block once its estimated encoded size reaches this many
    /// bytes. (A single larger-than-target entry still gets its own block.)
    pub block_size: usize,
    /// Entries between restart points inside a data block.
    pub restart_interval: u32,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            block_size: 4096,
            restart_interval: 16,
        }
    }
}

impl Options {
    pub fn validate(&self) -> Result<()> {
        if self.block_size < 32 {
            return Err(Error::invalid_argument("block_size must be >= 32"));
        }
        if self.block_size > MAX_BLOCK_SIZE {
            return Err(Error::invalid_argument("block_size exceeds maximum"));
        }
        if self.restart_interval == 0 {
            return Err(Error::invalid_argument("restart_interval must be >= 1"));
        }
        Ok(())
    }
}

/// Summary returned by a successful build.
#[derive(Debug, Clone)]
pub struct BuildStats {
    pub bytes: u64,
    pub entries: u64,
    pub data_blocks: u64,
}

// ---------------------------------------------------------------------------
// Write path
// ---------------------------------------------------------------------------

/// Builds one table over a generic [`WritableFile`].
pub struct TableBuilder<W: WritableFile> {
    writer: W,
    options: Options,
    data: BlockBuilder,
    index: BlockBuilder,
    last_key: Vec<u8>,
    offset: u64,
    num_entries: u64,
    num_data_blocks: u64,
    pending_index_entry: bool,
    pending_handle: Option<BlockHandle>,
    pending_last_key: Option<Vec<u8>>,
    finished: bool,
}

impl<W: WritableFile> TableBuilder<W> {
    pub fn new(writer: W, options: Options) -> Result<TableBuilder<W>> {
        options.validate()?;
        Ok(TableBuilder {
            writer,
            data: BlockBuilder::new(options.restart_interval)?,
            index: BlockBuilder::new(1)?,
            last_key: Vec::new(),
            offset: 0,
            num_entries: 0,
            num_data_blocks: 0,
            pending_index_entry: false,
            pending_handle: None,
            pending_last_key: None,
            finished: false,
            options,
        })
    }

    pub fn entries(&self) -> u64 {
        self.num_entries
    }

    /// Append a key/value pair. Keys MUST be strictly increasing.
    pub fn add(&mut self, key: &[u8], value: &[u8]) -> Result<()> {
        if self.finished {
            return Err(Error::invalid_argument("builder already finished"));
        }
        if self.num_entries > 0 && key <= self.last_key.as_slice() {
            return Err(Error::invalid_argument(format!(
                "keys must be strictly increasing: new key {} <= last key {}",
                coding::to_hex(key),
                coding::to_hex(&self.last_key)
            )));
        }

        // The most recent flush needs its index separator, which can only be
        // chosen now that we know the first key of the *next* block.
        if self.pending_index_entry {
            let mut separator = self
                .pending_last_key
                .take()
                .expect("pending separator key must exist");
            find_shortest_separator(&mut separator, key);
            let handle = self
                .pending_handle
                .take()
                .expect("pending handle must exist");
            self.index.add(&separator, &encode_handle(handle));
            self.pending_index_entry = false;
        }

        self.data.add(key, value);
        self.last_key.clear();
        self.last_key.extend_from_slice(key);
        self.num_entries += 1;

        if self.data.estimated_size() >= self.options.block_size {
            self.flush_data_block()?;
        }
        Ok(())
    }

    fn flush_data_block(&mut self) -> Result<()> {
        if self.data.is_empty() {
            return Ok(());
        }
        // Resolve a still-pending separator from the previous flush. Normally
        // this happens at the top of `add` (where the next user key is known);
        // the only time it is still pending here is when a single entry grew
        // the buffer past the target, i.e. that entry is itself the first key
        // of the block about to start — so it is also the correct upper bound.
        if self.pending_index_entry {
            let upper = self.last_key.clone();
            let mut separator = self
                .pending_last_key
                .take()
                .expect("pending separator key must exist");
            find_shortest_separator(&mut separator, &upper);
            let handle = self
                .pending_handle
                .take()
                .expect("pending handle must exist");
            self.index.add(&separator, &encode_handle(handle));
            self.pending_index_entry = false;
        }
        let payload = self.data.finish().to_vec();
        let handle = append_block(&mut self.writer, self.offset, &payload)?;
        self.offset = handle.end_exclusive()?;
        self.data.reset();
        self.num_data_blocks += 1;
        self.pending_index_entry = true;
        self.pending_handle = Some(handle);
        self.pending_last_key = Some(self.last_key.clone());
        Ok(())
    }

    /// Flush trailing data, write metaindex/index/footer and sync. Consumes the
    /// builder; the writer itself is dropped afterwards.
    pub fn finish(mut self) -> Result<BuildStats> {
        self.flush_data_block()?;

        if self.pending_index_entry {
            let mut successor = self
                .pending_last_key
                .take()
                .expect("pending successor key must exist");
            find_short_successor(&mut successor);
            let handle = self
                .pending_handle
                .take()
                .expect("pending handle must exist");
            self.index.add(&successor, &encode_handle(handle));
            self.pending_index_entry = false;
        }

        // Empty metaindex block (no optional sections in this version).
        let mut meta = BlockBuilder::new(1)?;
        let meta_payload = meta.finish().to_vec();
        let meta_handle = append_block(&mut self.writer, self.offset, &meta_payload)?;
        self.offset = meta_handle.end_exclusive()?;

        let index_payload = self.index.finish().to_vec();
        let index_handle = append_block(&mut self.writer, self.offset, &index_payload)?;
        self.offset = index_handle.end_exclusive()?;

        let mut footer_bytes = Vec::with_capacity(FOOTER_LEN);
        Footer::new(meta_handle, index_handle).encode_to(&mut footer_bytes)?;
        append_all(&mut self.writer, &footer_bytes)?;
        self.offset += footer_bytes.len() as u64;

        // The single durability boundary for the whole table.
        self.writer.sync()?;
        self.finished = true;

        Ok(BuildStats {
            bytes: self.offset,
            entries: self.num_entries,
            data_blocks: self.num_data_blocks,
        })
    }
}

fn append_block<W: WritableFile + ?Sized>(
    w: &mut W,
    offset: u64,
    payload: &[u8],
) -> Result<BlockHandle> {
    if payload.len() > MAX_BLOCK_SIZE {
        return Err(Error::invalid_argument("block payload exceeds maximum"));
    }
    let mut buf = Vec::with_capacity(payload.len() + BLOCK_TRAILER_LEN);
    let probe = format::write_block(&mut buf, BLOCK_TYPE_DATA, payload);
    debug_assert_eq!(probe.offset, 0);
    append_all(w, &buf)?;
    Ok(BlockHandle::new(offset, payload.len() as u64))
}

fn encode_handle(h: BlockHandle) -> Vec<u8> {
    let mut v = Vec::new();
    h.encode_to(&mut v);
    v
}

fn decode_handle_value(value: &[u8]) -> Result<BlockHandle> {
    let (handle, n) = BlockHandle::decode(value)?;
    if n != value.len() {
        return Err(Error::corruption("index value has trailing bytes"));
    }
    Ok(handle)
}

/// Mutate `start` into a short key such that `start <= result < limit`
/// (LevelDB's `FindShortestSeparator`).
fn find_shortest_separator(start: &mut Vec<u8>, limit: &[u8]) {
    let min_len = start.len().min(limit.len());
    let mut diff = 0;
    while diff < min_len && start[diff] == limit[diff] {
        diff += 1;
    }
    if diff >= min_len {
        // One is a prefix of the other: cannot shorten.
        return;
    }
    let diff_byte = start[diff];
    if diff_byte < 0xff && diff_byte + 1 < limit[diff] {
        start[diff] += 1;
        start.truncate(diff + 1);
    }
}

/// Shortest key strictly greater than `key` (used as the final index bound).
fn find_short_successor(key: &mut Vec<u8>) {
    for (i, b) in key.iter_mut().enumerate() {
        if *b != 0xff {
            *b += 1;
            key.truncate(i + 1);
            return;
        }
    }
    // All 0xff: no shorter successor exists; leave the key unchanged.
}

// ---------------------------------------------------------------------------
// File-based atomic build
// ---------------------------------------------------------------------------

/// Sort and strictly validate `entries`, then build `dir/name` atomically.
///
/// The temp file is removed on any error. `unique_suffix` must make the temp
/// name unique among concurrent builds in the same directory.
pub fn build_table_sorted(
    dir: &Path,
    name: &str,
    entries: &[(Vec<u8>, Vec<u8>)],
    options: Options,
    unique_suffix: u64,
) -> Result<BuildStats> {
    validate_table_name(name)?;
    let mut sorted: Vec<&(Vec<u8>, Vec<u8>)> = entries.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    for pair in sorted.windows(2) {
        if pair[0].0 == pair[1].0 {
            return Err(Error::invalid_argument(format!(
                "duplicate key: {}",
                coding::to_hex(&pair[0].0)
            )));
        }
    }

    let final_path = dir.join(name);
    // `rename(2)` silently replaces an existing destination on Linux; check
    // explicitly so rebuilding an existing table is a conflict rather than an
    // overwrite of a potentially open file.
    if final_path.try_exists()? {
        return Err(Error::Io(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!("table {name} already exists"),
        )));
    }
    let tmp_name = format!(".{name}.tmp.{unique_suffix}");
    let tmp_path = dir.join(&tmp_name);

    let result = (|| -> Result<BuildStats> {
        let writer = PosixWriter::create(&tmp_path)?;
        let mut builder = TableBuilder::new(writer, options)?;
        for (k, v) in &sorted {
            builder.add(k, v)?;
        }
        let stats = builder.finish()?;
        fs::rename(&tmp_path, &final_path)?;
        // Sync the directory entry so the rename itself is durable.
        sync_directory(dir)?;
        Ok(stats)
    })();

    if result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    result
}

pub fn validate_table_name(name: &str) -> Result<()> {
    if name.is_empty()
        || name == "."
        || name == ".."
        || name.contains('/')
        || name.contains('\\')
        || name.contains('\0')
    {
        return Err(Error::invalid_argument(
            "table name must be a non-empty path component without /, \\ or NUL",
        ));
    }
    Ok(())
}

fn sync_directory(dir: &Path) -> Result<()> {
    let f = File::open(dir)?;
    f.sync_all()?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Read path
// ---------------------------------------------------------------------------

/// An open, read-only table.
pub struct Table<R: RandomAccessFile> {
    file: R,
    file_size: u64,
    #[allow(dead_code)] // parsed eagerly as fail-fast; kept for completeness
    footer: Footer,
    index_block: Block,
}

impl Table<PosixReader> {
    /// Open a table directly from a filesystem path.
    pub fn open_path(path: &Path) -> Result<Table<PosixReader>> {
        let size = fs::metadata(path)?.len();
        Table::open(PosixReader::open(path)?, size)
    }
}

impl<R: RandomAccessFile> Table<R> {
    /// Open a table. Only the footer and the index block are read eagerly
    /// (fail fast); data blocks are read on demand.
    pub fn open(file: R, file_size: u64) -> Result<Table<R>> {
        if file_size < FOOTER_LEN as u64 {
            return Err(Error::corruption(format!(
                "file too small to contain a footer: {file_size} bytes"
            )));
        }
        let footer = {
            let mut buf = vec![0u8; FOOTER_LEN];
            let off = file_size - FOOTER_LEN as u64;
            let n = read_fill(&file, &mut buf, off)?;
            if n != FOOTER_LEN {
                return Err(Error::corruption("short footer read"));
            }
            Footer::decode(&buf)?
        };
        let index_block = read_block_checked(&file, file_size, footer.index)?;
        Ok(Table {
            file,
            file_size,
            footer,
            index_block,
        })
    }

    pub fn file_size(&self) -> u64 {
        self.file_size
    }

    pub fn data_block_count_from_index(&self) -> Result<usize> {
        // Index holds exactly one entry per data block.
        let mut cur = self.index_block.first()?;
        let mut n = 0;
        while cur.valid() {
            n += 1;
            cur.next()?;
        }
        Ok(n)
    }

    /// Point lookup: `Some(value)` iff `key` exists.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let handle = match self.index_handle_for(key)? {
            Some(h) => h,
            None => return Ok(None),
        };
        let block = self.read_block(handle)?;
        let cur = block.seek(key)?;
        if cur.valid() && cur.key() == key {
            Ok(Some(cur.value()?.to_vec()))
        } else {
            Ok(None)
        }
    }

    /// Open a forward range scan.
    pub fn scan(&self, options: ScanOptions) -> Result<ScanIter<'_, R>> {
        ScanIter::new(self, options)
    }

    fn index_handle_for(&self, key: &[u8]) -> Result<Option<BlockHandle>> {
        let cur = self.index_block.seek(key)?;
        if !cur.valid() {
            return Ok(None);
        }
        Ok(Some(decode_handle_value(cur.value()?)?))
    }

    fn read_block(&self, handle: BlockHandle) -> Result<Block> {
        read_block_checked(&self.file, self.file_size, handle)
    }

    /// Read every byte of the file and enforce every format invariant.
    pub fn validate(&self) -> Result<ValidationReport> {
        let mut report = ValidationReport {
            file_size: self.file_size,
            ..ValidationReport::default()
        };

        // 1. Footer must sit exactly at end of file and carry the magic.
        let mut footer_bytes = vec![0u8; FOOTER_LEN];
        let n = read_fill(
            &self.file,
            &mut footer_bytes,
            self.file_size - FOOTER_LEN as u64,
        )?;
        if n != FOOTER_LEN {
            return Err(Error::corruption("short footer read during validate"));
        }
        let footer = Footer::decode(&footer_bytes)?;

        // 2. Handle bounds and the block ordering footer -> data.
        let index_end = footer
            .index
            .end_exclusive()
            .and_then(|e| check_inside_file(e, self.file_size))?;
        if index_end != self.file_size - FOOTER_LEN as u64 {
            return Err(Error::corruption(
                "index block is not directly followed by the footer",
            ));
        }
        let meta_end = footer
            .metaindex
            .end_exclusive()
            .and_then(|e| check_inside_file(e, self.file_size))?;
        if meta_end != footer.index.offset {
            return Err(Error::corruption(
                "metaindex block is not directly followed by the index block",
            ));
        }

        // 3. Metaindex: structurally valid and empty in this version.
        let meta_block = read_block_checked(&self.file, self.file_size, footer.metaindex)?;
        let meta_stats = block::validate_entries(&meta_block)?;
        if meta_stats.entries != 0 {
            return Err(Error::corruption(format!(
                "metaindex must be empty, found {} entries",
                meta_stats.entries
            )));
        }

        // 4. Index block: walk entries and check handles are contiguous and
        //    bounded.
        let index_block = read_block_checked(&self.file, self.file_size, footer.index)?;
        let index_stats = block::validate_entries(&index_block)?;
        let mut expected_offset = 0u64;
        let mut prev_last_key: Option<Vec<u8>> = None;
        let mut prev_separator: Option<Vec<u8>> = None;
        let mut final_separator: Option<Vec<u8>> = None;
        let mut cur = index_block.first()?;
        while cur.valid() {
            let sep = cur.key().to_vec();
            final_separator = Some(sep.clone());
            let handle = decode_handle_value(cur.value()?)?;
            if handle.offset != expected_offset {
                return Err(Error::corruption(format!(
                    "data block gap/overlap at offset {} (expected {expected_offset})",
                    handle.offset
                )));
            }
            let end = handle
                .end_exclusive()
                .and_then(|e| check_inside_file(e, self.file_size))?;
            if end > footer.metaindex.offset {
                return Err(Error::corruption("data block runs past metaindex"));
            }
            expected_offset = end;

            // 5. The referenced data block itself.
            let data_block = read_block_checked(&self.file, self.file_size, handle)?;
            let stats = block::validate_entries(&data_block)?;
            report.total_entries += stats.entries;
            report.total_restarts += stats.restarts;
            report.data_blocks.push(BlockReport {
                offset: handle.offset,
                payload_size: handle.size,
                entries: stats.entries,
                restarts: stats.restarts,
            });

            // 6. Cross-block ordering and separator correctness.
            let mut block_cur = data_block.first()?;
            let first_key = block_cur.key().to_vec();
            if let Some(last) = &prev_last_key {
                if last.as_slice() >= first_key.as_slice() {
                    return Err(Error::corruption(
                        "keys do not increase across the data block boundary",
                    ));
                }
            }
            if let Some(prev_sep) = &prev_separator {
                if prev_sep.as_slice() >= first_key.as_slice() {
                    return Err(Error::corruption(
                        "index separator must be strictly less than the first key \
                         of the following block",
                    ));
                }
            }
            while block_cur.next()? {}
            // After the loop the cursor is invalid, but its key buffer still
            // holds the final key; read it through a dedicated walk instead.
            let last_key = {
                let mut c = data_block.first()?;
                let mut k = c.key().to_vec();
                while c.next()? {
                    k = c.key().to_vec();
                }
                k
            };
            if sep.as_slice() < last_key.as_slice() {
                return Err(Error::corruption(
                    "index separator is smaller than the last key of its block",
                ));
            }
            prev_last_key = Some(last_key);
            prev_separator = Some(sep);

            cur.next()?;
        }
        if expected_offset != footer.metaindex.offset {
            return Err(Error::corruption(
                "data blocks do not tightly precede the metaindex block",
            ));
        }
        if index_stats.entries != report.data_blocks.len() {
            return Err(Error::corruption("index entry count != data block count"));
        }

        // Final index successor must be strictly greater than the last data
        // key. An empty index means an empty table and is legitimate.
        match (final_separator, prev_last_key) {
            (Some(sep), Some(last)) if sep.as_slice() <= last.as_slice() => {
                return Err(Error::corruption(
                    "final index successor is not greater than the last data key",
                ));
            }
            (None, None) => {}
            _ => {}
        }

        report.ok = true;
        Ok(report)
    }
}

fn check_inside_file(end: u64, file_size: u64) -> Result<u64> {
    if end > file_size {
        return Err(Error::corruption("block handle reaches past end of file"));
    }
    Ok(end)
}

/// Read payload + trailer, enforce bounds/type/CRC, then parse the block.
fn read_block_checked<R: RandomAccessFile + ?Sized>(
    file: &R,
    file_size: u64,
    handle: BlockHandle,
) -> Result<Block> {
    if handle.size > MAX_BLOCK_SIZE as u64 {
        return Err(Error::corruption(format!(
            "block size {} exceeds maximum",
            handle.size
        )));
    }
    let end = handle
        .offset
        .checked_add(handle.size)
        .and_then(|v| v.checked_add(BLOCK_TRAILER_LEN as u64))
        .ok_or_else(|| Error::corruption("block handle overflow"))?;
    if end > file_size {
        return Err(Error::corruption(format!(
            "block at {}+{} reaches past end of file ({file_size})",
            handle.offset, handle.size
        )));
    }

    let mut payload = vec![0u8; handle.size as usize];
    let n = read_fill(file, &mut payload, handle.offset)?;
    if n as u64 != handle.size {
        return Err(Error::corruption("short block payload read"));
    }
    let mut trailer = [0u8; BLOCK_TRAILER_LEN];
    let n = read_fill(file, &mut trailer, handle.offset + handle.size)?;
    if n != BLOCK_TRAILER_LEN {
        return Err(Error::corruption("short block trailer read"));
    }

    let block_type = trailer[0];
    if block_type != BLOCK_TYPE_DATA {
        return Err(Error::corruption(format!(
            "unsupported block type {block_type} (only raw data blocks exist)"
        )));
    }
    let stored = coding::decode_u32_le(&trailer[1..]);
    let mut crc_input = Vec::with_capacity(1 + payload.len());
    crc_input.push(block_type);
    crc_input.extend_from_slice(&payload);
    let computed = coding::mask_crc(coding::crc32c(&crc_input));
    if stored != computed {
        return Err(Error::corruption(format!(
            "block checksum mismatch at offset {} (stored {stored:#010x}, computed {computed:#010x})",
            handle.offset
        )));
    }
    Block::parse(payload)
}

// ---------------------------------------------------------------------------
// Validation report
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct BlockReport {
    pub offset: u64,
    pub payload_size: u64,
    pub entries: usize,
    pub restarts: usize,
}

#[derive(Debug, Clone, Default)]
pub struct ValidationReport {
    pub ok: bool,
    pub file_size: u64,
    pub data_blocks: Vec<BlockReport>,
    pub total_entries: usize,
    pub total_restarts: usize,
}

// ---------------------------------------------------------------------------
// Range scans
// ---------------------------------------------------------------------------

/// One side of a range.
#[derive(Debug, Clone)]
pub enum Bound {
    Unbounded,
    Included(Vec<u8>),
    Excluded(Vec<u8>),
}

/// Scan parameters.
#[derive(Debug, Clone)]
pub struct ScanOptions {
    pub start: Bound,
    pub end: Bound,
    pub limit: Option<u64>,
}

impl Default for ScanOptions {
    fn default() -> Self {
        ScanOptions {
            start: Bound::Unbounded,
            end: Bound::Unbounded,
            limit: None,
        }
    }
}

impl Bound {
    #[allow(dead_code)]
    fn as_key(&self) -> Option<&[u8]> {
        match self {
            Bound::Unbounded => None,
            Bound::Included(k) | Bound::Excluded(k) => Some(k),
        }
    }
}

/// Per-block manual decode state (a self-referential `Cursor` over our own
/// owned block is impossible, so the iterator tracks positions itself).
struct DataState {
    block: Block,
    /// Offset of the next entry to decode (ignored while `primed`).
    pos: usize,
    /// Offset of the current entry when primed/returned.
    cur_offset: usize,
    key: Vec<u8>,
    primed: bool,
}

/// Forward iterator over a contiguous key range, hopping data blocks through
/// the sparse index on demand.
pub struct ScanIter<'a, R: RandomAccessFile> {
    table: &'a Table<R>,
    index_block: &'a Block,
    /// Offset of the next index entry to decode.
    idx_pos: usize,
    /// Reconstructed key of the current index entry.
    idx_key: Vec<u8>,
    /// Handle described by the current index entry.
    current_handle: Option<BlockHandle>,
    data: Option<DataState>,
    end: Bound,
    limit: u64,
    emitted: u64,
    done: bool,
}

impl<'a, R: RandomAccessFile> ScanIter<'a, R> {
    fn new(table: &'a Table<R>, options: ScanOptions) -> Result<ScanIter<'a, R>> {
        let limit = options.limit.unwrap_or(u64::MAX);
        let mut iter = ScanIter {
            table,
            index_block: &table.index_block,
            idx_pos: 0,
            idx_key: Vec::new(),
            current_handle: None,
            data: None,
            end: options.end,
            limit,
            emitted: 0,
            done: limit == 0,
        };

        if !iter.done {
            match &options.start {
                Bound::Unbounded => {
                    if !iter.advance_index()? {
                        iter.done = true;
                    } else {
                        iter.load_current_block()?;
                    }
                }
                Bound::Included(k) => iter.seek_start(k, false)?,
                Bound::Excluded(k) => iter.seek_start(k, true)?,
            }
        }
        Ok(iter)
    }

    /// Position at the first data block/entry relevant for `target`.
    fn seek_start(&mut self, target: &[u8], skip_equal: bool) -> Result<()> {
        let cur = self.index_block.seek(target)?;
        if !cur.valid() {
            self.done = true;
            return Ok(());
        }
        let entry_pos = cur.current_entry_offset();
        let h = self.index_block.entry_at(entry_pos)?;
        self.idx_key = cur.key().to_vec();
        let value =
            &self.index_block.payload()[h.value_offset..h.value_offset + h.value_len as usize];
        self.current_handle = Some(decode_handle_value(value)?);
        self.idx_pos = h.next_offset;

        let block = self
            .table
            .read_block(self.current_handle.expect("handle just decoded"))?;
        let mut dcur = block.seek(target)?;
        if skip_equal && dcur.valid() && dcur.key() == target {
            dcur.next()?;
        }
        if !dcur.valid() {
            self.data = None;
            return Ok(());
        }
        let offset = dcur.current_entry_offset();
        let h = block.entry_at(offset)?;
        let key = dcur.key().to_vec();
        self.data = Some(DataState {
            block,
            pos: h.next_offset,
            cur_offset: offset,
            key,
            primed: true,
        });
        Ok(())
    }

    /// Decode the next index entry; refresh `idx_key`/`current_handle`.
    fn advance_index(&mut self) -> Result<bool> {
        if self.idx_pos >= self.index_block.restart_offset() {
            return Ok(false);
        }
        let entry_pos = self.idx_pos;
        let h = self.index_block.entry_at(entry_pos)?;
        if h.shared as usize > self.idx_key.len() {
            return Err(Error::corruption("index entry shared length too large"));
        }
        self.idx_key.truncate(h.shared as usize);
        self.idx_key.extend_from_slice(
            &self.index_block.payload()
                [h.key_delta_offset..h.key_delta_offset + h.non_shared as usize],
        );
        let value =
            &self.index_block.payload()[h.value_offset..h.value_offset + h.value_len as usize];
        self.current_handle = Some(decode_handle_value(value)?);
        self.idx_pos = h.next_offset;
        Ok(true)
    }

    /// Read `current_handle`'s block and begin scanning at its first entry.
    fn load_current_block(&mut self) -> Result<()> {
        let handle = self
            .current_handle
            .ok_or_else(|| Error::corruption("internal: no current index handle"))?;
        let block = self.table.read_block(handle)?;
        self.data = Some(DataState {
            block,
            pos: 0,
            cur_offset: 0,
            key: Vec::new(),
            primed: false,
        });
        Ok(())
    }

    /// Return the next in-range `(key, value)` pair.
    ///
    /// Named `next_entry` rather than `Iterator::next` because advancing is
    /// fallible: every re-decode of on-disk bytes can surface corruption.
    pub fn next_entry(&mut self) -> Result<Option<(Vec<u8>, Vec<u8>)>> {
        loop {
            if self.done || self.emitted >= self.limit {
                return Ok(None);
            }

            if self.data.is_none() {
                if !self.advance_index()? {
                    self.done = true;
                    return Ok(None);
                }
                self.load_current_block()?;
            }

            let (entry_offset, key) = {
                let ds = self.data.as_mut().expect("data state just loaded");
                if ds.primed {
                    ds.primed = false;
                    (ds.cur_offset, ds.key.clone())
                } else {
                    if ds.pos >= ds.block.restart_offset() {
                        self.data = None;
                        continue;
                    }
                    let offset = ds.pos;
                    let h = ds.block.entry_at(offset)?;
                    if h.shared as usize > ds.key.len() {
                        return Err(Error::corruption(
                            "data entry shared length exceeds reconstructed key",
                        ));
                    }
                    ds.key.truncate(h.shared as usize);
                    ds.key.extend_from_slice(
                        &ds.block.payload()
                            [h.key_delta_offset..h.key_delta_offset + h.non_shared as usize],
                    );
                    ds.cur_offset = offset;
                    ds.pos = h.next_offset;
                    (offset, ds.key.clone())
                }
            };

            // Apply the end bound.
            match &self.end {
                Bound::Unbounded => {}
                Bound::Included(k) if key.as_slice() <= k.as_slice() => {}
                Bound::Excluded(k) if key.as_slice() < k.as_slice() => {}
                Bound::Included(_) | Bound::Excluded(_) => {
                    self.done = true;
                    return Ok(None);
                }
            }

            let ds = self.data.as_ref().expect("data state present");
            let h = ds.block.entry_at(entry_offset)?;
            let value =
                ds.block.payload()[h.value_offset..h.value_offset + h.value_len as usize].to_vec();

            self.emitted += 1;
            return Ok(Some((key, value)));
        }
    }
}

/// Convenience: collect a scan into a vector.
pub fn collect_scan<R: RandomAccessFile>(
    table: &Table<R>,
    options: ScanOptions,
) -> Result<Vec<(Vec<u8>, Vec<u8>)>> {
    let mut iter = table.scan(options)?;
    let mut out = Vec::new();
    while let Some(kv) = iter.next_entry()? {
        out.push(kv);
    }
    Ok(out)
}

/// Remove a table file in `dir` after validating `name`.
pub fn remove_table_file(dir: &Path, name: &str) -> Result<()> {
    validate_table_name(name)?;
    fs::remove_file(dir.join(name))?;
    Ok(())
}
