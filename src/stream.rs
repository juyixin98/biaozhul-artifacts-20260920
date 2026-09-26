//! 有界流式压缩 / 解压。
//!
//! - 压缩端按块（默认 1 MiB）读取输入：每块独立统计频率、建树、写码长
//!   表与码流，因此内存占用与块大小成正比而非与输入总长成正比。
//! - 解压端从 `Read` 逐位取码（每次只从下层拉一个字节），不在内存中
//!   缓冲整块码流；输出按 [`Limits`] 设硬上限。
//! - 所有长度声明都先于解码被校验，伪造的长度字段只能导致明确错误而
//!   不能导致巨量分配。

use crate::bits::BitWriter;
use crate::error::{Error, Result};
use crate::format::{
    read_block_header, read_length_table, read_prelude, write_block_header, write_length_table,
    write_prelude, BlockHeader, BLOCK_HEADER_LEN, PRELUDE_LEN,
};
use crate::huffman::{
    build_canonical_codes, build_decode_trie, build_lengths, frequencies, DecodeTable,
};
use std::io::{Read, Write};

/// 默认解码输出总上限：1 GiB。
pub const DEFAULT_MAX_OUTPUT_BYTES: u64 = 1 << 30;
/// 默认单块原始字节数上限：16 MiB。
pub const DEFAULT_MAX_BLOCK_BYTES: u64 = 16 << 20;
/// 默认压缩块大小：1 MiB。
pub const DEFAULT_BLOCK_SIZE: usize = 1 << 20;

/// 解码资源限制。
#[derive(Debug, Clone)]
pub struct Limits {
    /// 单块声明原始字节数上限。
    pub max_block_bytes: u64,
    /// 解码输出总字节数上限。
    pub max_output_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_block_bytes: DEFAULT_MAX_BLOCK_BYTES,
            max_output_bytes: DEFAULT_MAX_OUTPUT_BYTES,
        }
    }
}

/// 不完整码表（Kraft 和 < 1）处理策略。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum IncompletePolicy {
    /// 拒绝不完整码表（默认、推荐；编码器从不产生这种表）。
    #[default]
    Reject,
    /// 允许不完整码表；位流若走到未定义位串则解码报错。
    Allow,
}

/// 压缩统计。
#[derive(Debug, Clone)]
pub struct CompressStats {
    pub input_bytes: u64,
    pub output_bytes: u64,
    pub blocks: u64,
    /// 各块不同符号数之和（每块至少 0、至多 256）。
    pub distinct_symbols_total: u64,
}

impl CompressStats {
    /// 压缩率（输出/输入）；空输入定义为 0。
    pub fn ratio(&self) -> f64 {
        if self.input_bytes == 0 {
            0.0
        } else {
            self.output_bytes as f64 / self.input_bytes as f64
        }
    }
}

/// 解压统计。
#[derive(Debug, Clone)]
pub struct DecompressStats {
    pub compressed_bytes: u64,
    pub output_bytes: u64,
    pub blocks: u64,
}

fn io_err(e: std::io::Error) -> Error {
    Error::Io(e.kind().to_string())
}

/// 字节计数写入器。
struct CountingWriter<W: Write> {
    inner: W,
    written: u64,
}

impl<W: Write> CountingWriter<W> {
    fn new(inner: W) -> Self {
        Self { inner, written: 0 }
    }
}

impl<W: Write> Write for CountingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.written += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

/// 带 1 字节前瞻的分块读取器：让压缩端在写每块时就知道是否 BFINAL。
struct BlockChunker<'a, R: Read> {
    inner: &'a mut R,
    pending: Option<u8>,
}

impl<'a, R: Read> BlockChunker<'a, R> {
    fn new(inner: &'a mut R) -> Self {
        Self {
            inner,
            pending: None,
        }
    }

    /// 尽量填满 `buf`，返回 `(读取字节数, 是否最后一块)`。
    /// 空输入第一次调用返回 `(0, true)`。
    fn next_block(&mut self, buf: &mut [u8]) -> Result<(usize, bool)> {
        let mut filled = 0usize;
        if let Some(b) = self.pending.take() {
            buf[0] = b;
            filled = 1;
        }
        while filled < buf.len() {
            match self.inner.read(&mut buf[filled..]) {
                Ok(0) => break,
                Ok(n) => filled += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(io_err(e)),
            }
        }
        if filled < buf.len() {
            return Ok((filled, true));
        }
        // 恰好填满：多读 1 字节探测 EOF，读到的字节留给下一块。
        let mut one = [0u8; 1];
        loop {
            match self.inner.read(&mut one) {
                Ok(0) => return Ok((filled, true)),
                Ok(_) => {
                    self.pending = Some(one[0]);
                    return Ok((filled, false));
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(io_err(e)),
            }
        }
    }
}

/// 流式压缩：从 `input` 读取，向 `out` 写 `.chfc` 容器。
///
/// `block_size` 为每块处理的原始字节数（也即主要内存占用）。
pub fn compress_stream<R: Read, W: Write>(
    input: &mut R,
    out: &mut W,
    block_size: usize,
) -> Result<CompressStats> {
    if block_size == 0 {
        return Err(Error::BadRequest("block_size must be >= 1".into()));
    }
    let mut w = CountingWriter::new(out);
    write_prelude(&mut w)?;

    let mut chunker = BlockChunker::new(input);
    let mut buf = vec![0u8; block_size];
    let mut total_input = 0u64;
    let mut blocks = 0u64;
    let mut distinct_total = 0u64;

    loop {
        let (n, last) = chunker.next_block(&mut buf)?;
        blocks += 1;
        total_input = total_input.saturating_add(n as u64);

        if n == 0 {
            // 空输入：唯一一块、BFINAL、全零。
            if blocks != 1 {
                return Err(Error::FrequencyLogic("empty non-first block"));
            }
            write_block_header(
                &mut w,
                &BlockHeader {
                    bfinal: true,
                    single: false,
                    original_len: 0,
                    payload_bits: 0,
                    num_symbols: 0,
                },
            )?;
            break;
        }

        let data = &buf[..n];
        let freq = frequencies(data);
        let lengths = build_lengths(&freq);
        let num_symbols = lengths.iter().filter(|&&l| l != 0).count() as u16;
        distinct_total += num_symbols as u64;

        let single = num_symbols == 1;
        let (payload_bytes, payload_bits) = if single {
            // 单符号码字约定为 0：码流即 n 个 0 位，存放为全零字节。
            (vec![0u8; n.div_ceil(8)], n as u64)
        } else {
            let book = build_canonical_codes(&lengths)?;
            let mut sym_code: [Vec<u8>; 256] = std::array::from_fn(|_| Vec::new());
            for e in &book.entries {
                sym_code[e.symbol as usize] = e.code.bits().collect();
            }
            let mut payload = BitWriter::with_capacity(n);
            for &b in data {
                for &bit in &sym_code[b as usize] {
                    payload.write_bit(bit);
                }
            }
            payload.finish()
        };

        write_block_header(
            &mut w,
            &BlockHeader {
                bfinal: last,
                single,
                original_len: n as u64,
                payload_bits,
                num_symbols,
            },
        )?;
        write_length_table(&mut w, &lengths)?;
        w.write_all(&payload_bytes).map_err(io_err)?;

        if last {
            break;
        }
    }

    w.flush().map_err(io_err)?;
    Ok(CompressStats {
        input_bytes: total_input,
        output_bytes: w.written,
        blocks,
        distinct_symbols_total: distinct_total,
    })
}

/// 便捷封装：压缩内存中的字节片。
pub fn compress_bytes(input: &[u8], block_size: usize) -> Result<Vec<u8>> {
    let mut src = std::io::Cursor::new(input);
    let mut out = Vec::with_capacity(input.len() / 2);
    compress_stream(&mut src, &mut out, block_size)?;
    Ok(out)
}

/// 定长位流读取器：逐字节从 `Read` 拉取，每次最多预取一个字节，
/// 因此块边界（按字节对齐）对下层流精确可见。
struct StreamBitReader<'a, R: Read> {
    inner: &'a mut R,
    current: u8,
    /// current 中尚未消费的位数。
    bits_left: u32,
    consumed: u64,
    limit: u64,
}

impl<'a, R: Read> StreamBitReader<'a, R> {
    fn new(inner: &'a mut R, limit_bits: u64) -> Self {
        Self {
            inner,
            current: 0,
            bits_left: 0,
            consumed: 0,
            limit: limit_bits,
        }
    }

    fn remaining_bits(&self) -> u64 {
        self.limit - self.consumed
    }

    /// 读一位；越过块预算或下层提前 EOF 都报截断。
    fn read_bit(&mut self) -> Result<u8> {
        if self.consumed >= self.limit {
            return Err(Error::UnexpectedEndOfCode);
        }
        if self.bits_left == 0 {
            let mut byte = [0u8; 1];
            match self.inner.read_exact(&mut byte) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Err(Error::TruncatedStream)
                }
                Err(e) => return Err(io_err(e)),
            }
            self.current = byte[0];
            self.bits_left = 8;
        }
        let bit = (self.current >> (self.bits_left - 1)) & 1;
        self.bits_left -= 1;
        self.consumed += 1;
        Ok(bit)
    }

    /// 消费完预算后，校验末字节填充位为 0。
    fn check_padding(&self) -> Result<()> {
        let rem = self.limit & 7;
        if rem != 0 {
            // 读取最后 rem 位时拉入了末字节，低 8-rem 位是填充。
            let pad = (8 - rem) as u32;
            debug_assert_eq!(self.bits_left, pad);
            if self.current & ((1u8 << pad) - 1) != 0 {
                return Err(Error::NonZeroPadding);
            }
        }
        Ok(())
    }
}

/// 输出缓冲满时落盘。
fn flush_out<W: Write>(w: &mut CountingWriter<W>, buf: &mut [u8], len: &mut usize) -> Result<()> {
    if *len > 0 {
        w.write_all(&buf[..*len]).map_err(io_err)?;
        *len = 0;
    }
    Ok(())
}

/// 流式解压：从 `input` 读 `.chfc`，向 `out` 写原始字节，全程受 `limits`
/// 与 `policy` 约束。
pub fn decompress_stream<R: Read, W: Write>(
    input: &mut R,
    out: &mut W,
    limits: &Limits,
    policy: IncompletePolicy,
) -> Result<DecompressStats> {
    let mut w = CountingWriter::new(out);
    read_prelude(input)?;

    let mut blocks = 0u64;
    let mut total_output = 0u64;
    let mut compressed_bytes = PRELUDE_LEN as u64;

    loop {
        let header = match read_block_header(input)? {
            Some(h) => h,
            None => {
                if blocks == 0 {
                    return Err(Error::TruncatedHeader);
                }
                return Err(Error::TruncatedStream);
            }
        };
        blocks += 1;
        compressed_bytes += BLOCK_HEADER_LEN as u64;

        // 长度声明先校验，再决定任何分配 / 解码。
        if header.original_len > limits.max_block_bytes {
            return Err(Error::LimitExceeded {
                limit: limits.max_block_bytes,
                actual: header.original_len,
                what: "block original_len",
            });
        }
        total_output =
            total_output
                .checked_add(header.original_len)
                .ok_or(Error::LimitExceeded {
                    limit: u64::MAX,
                    actual: u64::MAX,
                    what: "output length overflow",
                })?;
        if total_output > limits.max_output_bytes {
            return Err(Error::OutputTooLong {
                declared: total_output,
                limit: limits.max_output_bytes,
            });
        }
        // 码长至多 255：合法码流位数不可能超过 original_len*255。
        if !header.single && header.num_symbols >= 2 {
            let max_bits = header
                .original_len
                .checked_mul(crate::huffman::MAX_CODE_LEN as u64)
                .unwrap_or(u64::MAX);
            if header.payload_bits > max_bits {
                return Err(Error::InvalidLengthTable(
                    "payload_bits exceeds original_len * max_code_len",
                ));
            }
        }

        let table_bytes = header.num_symbols as u64 * 2;
        if table_bytes > limits.max_block_bytes {
            return Err(Error::LimitExceeded {
                limit: limits.max_block_bytes,
                actual: table_bytes,
                what: "length table bytes",
            });
        }
        compressed_bytes += table_bytes;

        let lengths = read_length_table(input, header.num_symbols)?;

        if header.num_symbols == 0 {
            if !header.bfinal {
                return Err(Error::InvalidLengthTable(
                    "empty block is only legal as the final block",
                ));
            }
            break;
        }

        let mut bits = StreamBitReader::new(input, header.payload_bits);
        let mut decoded_this_block = 0u64;
        let mut out_buf = [0u8; 4096];
        let mut buf_len = 0usize;

        if header.single {
            let symbol = lengths.iter().position(|&l| l != 0).unwrap() as u8;
            while bits.remaining_bits() > 0 {
                // 单符号码字约定为 0：任何 1 位都是未定义码字。
                if bits.read_bit()? != 0 {
                    return Err(Error::UndefinedCodeword);
                }
                out_buf[buf_len] = symbol;
                buf_len += 1;
                decoded_this_block += 1;
                if buf_len == out_buf.len() {
                    flush_out(&mut w, &mut out_buf, &mut buf_len)?;
                }
            }
        } else {
            let table = build_decode_trie(&lengths, policy == IncompletePolicy::Allow)?;
            let mut node = DecodeTable::ROOT;
            while bits.remaining_bits() > 0 {
                let bit = bits.read_bit()?;
                node = match table.descend(node, bit) {
                    Some(n) => n,
                    None => return Err(Error::UndefinedCodeword),
                };
                if let Some(symbol) = table.leaf_symbol(node) {
                    out_buf[buf_len] = symbol;
                    buf_len += 1;
                    decoded_this_block += 1;
                    if decoded_this_block > header.original_len {
                        return Err(Error::LengthMismatch {
                            declared: header.original_len,
                            decoded: decoded_this_block,
                        });
                    }
                    if buf_len == out_buf.len() {
                        flush_out(&mut w, &mut out_buf, &mut buf_len)?;
                    }
                    node = DecodeTable::ROOT;
                }
            }
            // 位流在码字内部结束（没回到根）= 截断的码。
            if node != DecodeTable::ROOT {
                return Err(Error::UnexpectedEndOfCode);
            }
        }

        flush_out(&mut w, &mut out_buf, &mut buf_len)?;
        bits.check_padding()?;
        compressed_bytes += header.payload_bits.div_ceil(8);

        if decoded_this_block != header.original_len {
            return Err(Error::LengthMismatch {
                declared: header.original_len,
                decoded: decoded_this_block,
            });
        }

        if header.bfinal {
            break;
        }
    }

    // BFINAL 之后不得有任何尾随字节。
    let mut probe = [0u8; 1];
    match input.read_exact(&mut probe) {
        Ok(()) => Err(Error::TrailingData),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
            w.flush().map_err(io_err)?;
            Ok(DecompressStats {
                compressed_bytes,
                output_bytes: total_output,
                blocks,
            })
        }
        Err(e) => Err(io_err(e)),
    }
}

/// 便捷封装：解压内存中的字节片。
pub fn decompress_bytes(
    input: &[u8],
    limits: &Limits,
    policy: IncompletePolicy,
) -> Result<Vec<u8>> {
    let mut src = std::io::Cursor::new(input);
    let mut out = Vec::new();
    decompress_stream(&mut src, &mut out, limits, policy)?;
    Ok(out)
}
