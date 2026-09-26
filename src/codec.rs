//! Streaming encoder and decoder for the v1 binary container format.
//!
//! ## Streaming model
//!
//! The encoder processes **one stripe at a time**: it reads `k` data shards
//! of at most `stripe_size` bytes from the input, keeps only that stripe in
//! memory, computes parity and immediately writes the framed records. Peak
//! working memory is therefore about `n * stripe_size` bytes regardless of
//! the input length.
//!
//! The decoder parses the framed stream sequentially and likewise keeps only
//! the current stripe buffered; recovered data is emitted before the next
//! stripe is read. Both directions are bounded by explicit [`Limits`].

use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom, Write};

use sha2::{Digest, Sha256};

use crate::coding;
use crate::container::{Header, MAGIC, MAX_CHUNK_LEN, MAX_HEADER_LEN, TAG_CHUNK, TAG_EOS};
use crate::error::{Error, Result};

/// Default bound on codec working memory (256 MiB).
pub const DEFAULT_MAX_MEMORY: u64 = 256 << 20;
/// Default bound on decoded output length (1 GiB).
pub const DEFAULT_MAX_OUTPUT: u64 = 1 << 30;

/// Explicit resource bounds. Working memory and decoded output length are
/// never allowed to grow past these values.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// Maximum bytes of working memory the codec may allocate for shard
    /// buffers (approximately `n * stripe_size` per active stripe).
    pub max_memory: u64,
    /// Maximum number of decoded original bytes the decoder may emit.
    pub max_output: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_memory: DEFAULT_MAX_MEMORY,
            max_output: DEFAULT_MAX_OUTPUT,
        }
    }
}

impl Limits {
    fn check_memory(&self, needed: u64) -> Result<()> {
        if needed > self.max_memory {
            Err(Error::MemoryLimitExceeded {
                requested: needed,
                limit: self.max_memory,
            })
        } else {
            Ok(())
        }
    }
}

/// Result summary returned by [`encode_stream`] and [`decode_stream`].
#[derive(Debug, Clone)]
pub struct CodecReport {
    /// Number of stripes coded/decoded.
    pub stripes: u32,
    /// Number of original bytes read (encode) / written (decode).
    pub bytes: u64,
    /// Lowercase-hex SHA-256 of the original input (always computed).
    pub sha256: String,
}

// ---------------------------------------------------------------------------
// Wire helpers
// ---------------------------------------------------------------------------

fn write_u8(w: &mut dyn Write, value: u8) -> Result<()> {
    w.write_all(&[value])?;
    Ok(())
}

fn write_u32(w: &mut dyn Write, value: u32) -> Result<()> {
    w.write_all(&value.to_be_bytes())?;
    Ok(())
}

struct Reader<'a> {
    inner: &'a mut dyn Read,
}

impl<'a> Reader<'a> {
    fn read_exact(&mut self, buf: &mut [u8]) -> Result<()> {
        self.inner.read_exact(buf).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                Error::BadRecord("truncated record: stream ended mid-frame".into())
            } else {
                Error::Io(e)
            }
        })
    }

    fn u8(&mut self) -> Result<u8> {
        let mut buf = [0u8; 1];
        self.read_exact(&mut buf)?;
        Ok(buf[0])
    }

    fn u32(&mut self) -> Result<u32> {
        let mut buf = [0u8; 4];
        self.read_exact(&mut buf)?;
        Ok(u32::from_be_bytes(buf))
    }
}

/// Write a chunk record, splitting payloads larger than [`MAX_CHUNK_LEN`].
fn emit_chunks(out: &mut dyn Write, stripe: u32, shard: u8, payload: &[u8]) -> Result<()> {
    if payload.is_empty() {
        // A zero-length chunk explicitly records the shard's presence.
        write_u8(out, TAG_CHUNK)?;
        write_u32(out, stripe)?;
        write_u8(out, shard)?;
        write_u32(out, 0)?;
        return Ok(());
    }
    let max = MAX_CHUNK_LEN as usize;
    for part in payload.chunks(max) {
        write_u8(out, TAG_CHUNK)?;
        write_u32(out, stripe)?;
        write_u8(out, shard)?;
        write_u32(out, part.len() as u32)?;
        out.write_all(part)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Encoder
// ---------------------------------------------------------------------------

/// Stream `input` through a systematic Reed-Solomon encoder into `output`.
///
/// * `total_len` must equal the exact number of bytes readable from `input`;
///   the final-stripe padding layout is derived from it.
/// * When `tag_hash` is true the header carries a SHA-256 of the plaintext so
///   decoders can detect silent corruption externally. Because the header
///   precedes the data but the hash is only known after reading it, a
///   fixed-size provisional header (64 `'0'` hash digits) is emitted and
///   rewritten in place at the end; `output` must therefore support seeking.
///   The stream remains single-stripe buffered — no full-spooling occurs.
#[allow(clippy::too_many_arguments)]
pub fn encode_stream<W>(
    input: &mut dyn Read,
    output: &mut W,
    k: u16,
    m: u16,
    stripe_size: u32,
    total_len: u64,
    tag_hash: bool,
    limits: Limits,
) -> Result<CodecReport>
where
    W: Write + Seek,
{
    let mut header = Header::new(k, m, stripe_size, total_len)?;
    let k = header.k();
    let m = header.m();
    let s = header.stripe_size();
    let stripes = header.stripe_count();

    // Per-stripe working set: k padded data shards + m parity shards.
    let working_set = (k + m) as u64 * s as u64;
    limits.check_memory(working_set)?;
    if total_len > limits.max_output {
        return Err(Error::OutputLimitExceeded {
            limit: limits.max_output,
            attempted: total_len,
        });
    }

    if tag_hash {
        // Reserve exactly 64 hex characters so the patched header stays the
        // same byte length as the provisional one.
        header.sha256 = Some("0".repeat(64));
    }

    // Header frame.
    output.write_all(MAGIC)?;
    let provisional = serde_json::to_vec(&header)?;
    write_u32(output, provisional.len() as u32)?;
    let header_json_offset = output.stream_position()?;
    output.write_all(&provisional)?;

    let mut hasher = Sha256::new();
    let mut bytes_read: u64 = 0;
    let mut data: Vec<Vec<u8>> = vec![Vec::new(); k];

    for stripe in 0..stripes {
        // Read exactly k*S original bytes (fewer on the final stripe) into
        // fixed-width data shards, one shard at a time.
        for (shard_id, shard) in data.iter_mut().enumerate() {
            shard.clear();
            let want = header.shard_len(stripe, shard_id as u8) as usize;
            let mut remaining = want;
            shard.resize(want, 0);
            let mut filled = 0;
            while remaining > 0 {
                let got = input.read(&mut shard[filled..want])?;
                if got == 0 {
                    break;
                }
                hasher.update(&shard[filled..filled + got]);
                bytes_read += got as u64;
                filled += got;
                remaining -= got;
            }
            if filled != want {
                return Err(Error::BadHeader(format!(
                    "input ended early: stripe {stripe} shard {shard_id} got {filled} bytes, expected {want}"
                )));
            }
        }

        if bytes_read > total_len {
            return Err(Error::BadHeader(format!(
                "input longer than declared total_len={total_len}"
            )));
        }

        // Zero-pad to coding width and generate parity.
        let padded: Vec<Vec<u8>> = data
            .iter()
            .map(|shard| {
                let mut buf = shard.clone();
                buf.resize(s, 0);
                buf
            })
            .collect();
        let data_refs: Vec<&[u8]> = padded.iter().map(|v| v.as_slice()).collect();
        let parity = coding::encode_stripe(&data_refs, m)?;

        for (shard, bytes) in data.iter().enumerate() {
            emit_chunks(output, stripe, shard as u8, bytes)?;
        }
        for (p, bytes) in parity.iter().enumerate() {
            emit_chunks(output, stripe, (k + p) as u8, bytes)?;
        }
    }

    // Detect an input shorter than declared.
    let mut tail = [0u8; 1];
    if input.read(&mut tail)? != 0 {
        return Err(Error::BadHeader(format!(
            "input longer than declared total_len={total_len}"
        )));
    }
    if bytes_read != total_len {
        return Err(Error::BadHeader(format!(
            "input ended early: read {bytes_read} bytes, total_len declared {total_len}"
        )));
    }

    // End-of-stream record.
    write_u8(output, TAG_EOS)?;
    write_u32(output, stripes)?;
    output.flush()?;

    let hash = hex_encode(&hasher.finalize_reset());
    if tag_hash {
        header.sha256 = Some(hash.clone());
        let patched = serde_json::to_vec(&header)?;
        assert_eq!(
            patched.len(),
            provisional.len(),
            "patched header must keep provisional length"
        );
        output.seek(SeekFrom::Start(header_json_offset))?;
        output.write_all(&patched)?;
        output.seek(SeekFrom::End(0))?;
    }

    Ok(CodecReport {
        stripes,
        bytes: bytes_read,
        sha256: hash,
    })
}

// ---------------------------------------------------------------------------
// Decoder
// ---------------------------------------------------------------------------

/// Selector describing shards treated as lost (erased) before decoding.
///
/// The decoder itself simply sees absent chunk records; the control layer
/// models shard loss by discarding the matching records with this filter.
#[derive(Debug, Clone, Default)]
pub struct LossPattern {
    /// Shard ids lost on every stripe.
    pub shards: Vec<u8>,
    /// Exact `(stripe, shard)` cells that are lost.
    pub cells: Vec<(u32, u8)>,
}

impl LossPattern {
    fn contains(&self, stripe: u32, shard: u8) -> bool {
        self.shards.contains(&shard) || self.cells.contains(&(stripe, shard))
    }
}

/// Decode a framed container stream into `output`, recovering erased shards.
///
/// `loss` simulates known erasures by skipping matching chunk records (the
/// mathematical analogue of whole shards being unavailable). When
/// `expect_hash` is true, the decoded plaintext is additionally verified
/// against the header SHA-256; without such an external checksum, silent
/// corruption cannot be detected by Reed-Solomon decoding.
pub fn decode_stream(
    input: &mut dyn Read,
    output: &mut dyn Write,
    loss: &LossPattern,
    expect_hash: bool,
    limits: Limits,
) -> Result<CodecReport> {
    let mut reader = Reader { inner: input };

    let mut magic = [0u8; 4];
    reader.read_exact(&mut magic)?;
    if &magic != MAGIC {
        return Err(Error::BadMagic);
    }
    let header_len = reader.u32()?;
    if header_len == 0 || header_len > MAX_HEADER_LEN {
        return Err(Error::BadHeader(format!(
            "header length {header_len} out of range 1..={MAX_HEADER_LEN}"
        )));
    }
    let mut header_bytes = vec![0u8; header_len as usize];
    reader.read_exact(&mut header_bytes)?;
    let header: Header = serde_json::from_slice(&header_bytes)?;
    header.validate()?;

    if header.total_len > limits.max_output {
        return Err(Error::OutputLimitExceeded {
            limit: limits.max_output,
            attempted: header.total_len,
        });
    }
    let working_set = header.n() as u64 * header.stripe_size as u64;
    limits.check_memory(working_set)?;

    let stripe_count = header.stripe_count();

    let mut hasher = Sha256::new();
    let mut bytes_written: u64 = 0;

    let mut expect_stripe: u32 = 0;
    let mut shards: BTreeMap<u8, Vec<u8>> = BTreeMap::new();
    let mut last_shard: Option<u8> = None;
    let mut stripes_finalized: u32 = 0;

    loop {
        let tag = match reader.u8() {
            Ok(tag) => tag,
            Err(Error::BadRecord(_)) => {
                return Err(Error::UnexpectedEos {
                    stripe: expect_stripe,
                })
            }
            Err(e) => return Err(e),
        };
        match tag {
            TAG_CHUNK => {
                let stripe = reader.u32()?;
                let shard = reader.u8()?;
                let chunk_len = reader.u32()?;
                if chunk_len > MAX_CHUNK_LEN {
                    return Err(Error::BadRecord(format!(
                        "chunk length {chunk_len} exceeds {MAX_CHUNK_LEN}"
                    )));
                }
                if stripe >= stripe_count {
                    return Err(Error::BadRecord(format!(
                        "stripe index {stripe} >= stripe_count {stripe_count}"
                    )));
                }
                if shard as usize >= header.n() {
                    return Err(Error::BadRecord(format!(
                        "shard id {shard} out of range 0..{}",
                        header.n()
                    )));
                }
                if stripe < expect_stripe {
                    return Err(Error::BadRecord(format!(
                        "stripe index went backwards: {stripe} after {expect_stripe}"
                    )));
                }
                if stripe > expect_stripe + 1 {
                    return Err(Error::BadRecord(format!(
                        "non-consecutive stripe: expected {} or {}, got {stripe} (a stripe was skipped)",
                        expect_stripe,
                        expect_stripe + 1
                    )));
                }

                // First record of the next stripe finalizes the previous one.
                if stripe == expect_stripe + 1 {
                    finalize_stripe(
                        &header,
                        expect_stripe,
                        &shards,
                        output,
                        &mut hasher,
                        &mut bytes_written,
                        limits.max_output,
                    )?;
                    stripes_finalized = expect_stripe + 1;
                    shards = BTreeMap::new();
                    last_shard = None;
                    expect_stripe = stripe;
                }

                // Loss filter: "lost" records are drained but not retained.
                if loss.contains(stripe, shard) {
                    let mut limited = reader.inner.take(chunk_len as u64);
                    std::io::copy(&mut limited, &mut std::io::sink())?;
                    continue;
                }

                // Shard ids within a stripe must be nondecreasing; a smaller
                // id marks a duplicated/overlapping shard.
                if let Some(prev) = last_shard {
                    if shard < prev {
                        return Err(Error::BadRecord(format!(
                            "shard id went backwards in stripe {stripe}: {shard} after {prev}"
                        )));
                    }
                }
                last_shard = Some(shard);

                let expected = header.shard_len(stripe, shard);
                let buf = shards.entry(shard).or_default();
                if buf.len() as u64 + chunk_len as u64 > expected {
                    return Err(Error::LengthMismatch {
                        stripe,
                        shard,
                        got: buf.len() as u64 + chunk_len as u64,
                        expected,
                    });
                }
                let old_len = buf.len();
                buf.resize(old_len + chunk_len as usize, 0);
                reader.read_exact(&mut buf[old_len..])?;
            }
            TAG_EOS => {
                let eos_count = reader.u32()?;
                if eos_count != stripe_count {
                    return Err(Error::BadRecord(format!(
                        "EOS stripe_count {eos_count} disagrees with header ({stripe_count})"
                    )));
                }
                if expect_stripe < stripe_count {
                    finalize_stripe(
                        &header,
                        expect_stripe,
                        &shards,
                        output,
                        &mut hasher,
                        &mut bytes_written,
                        limits.max_output,
                    )?;
                    stripes_finalized = expect_stripe + 1;
                }
                if stripes_finalized != stripe_count {
                    return Err(Error::UnexpectedEos {
                        stripe: stripes_finalized,
                    });
                }
                break;
            }
            other => {
                return Err(Error::BadRecord(format!(
                    "unknown record tag 0x{other:02x}"
                )))
            }
        }
    }

    output.flush()?;
    if bytes_written != header.total_len {
        return Err(Error::BadHeader(format!(
            "decoded {bytes_written} bytes but header declared {}",
            header.total_len
        )));
    }

    let actual_hash = hex_encode(&hasher.finalize_reset());
    if expect_hash {
        if let Some(expected) = &header.sha256 {
            if expected != &actual_hash {
                return Err(Error::HashMismatch {
                    expected: expected.clone(),
                    actual: actual_hash,
                });
            }
        }
    }

    Ok(CodecReport {
        stripes: stripe_count,
        bytes: bytes_written,
        sha256: actual_hash,
    })
}

/// Validate wire lengths, zero-pad legitimate short final data shards and
/// reconstruct the stripe, emitting recovered data shards in id order.
#[allow(clippy::too_many_arguments)]
fn finalize_stripe(
    header: &Header,
    stripe: u32,
    present: &BTreeMap<u8, Vec<u8>>,
    output: &mut dyn Write,
    hasher: &mut Sha256,
    bytes_written: &mut u64,
    max_output: u64,
) -> Result<()> {
    let k = header.k();
    let m = header.m();
    let s = header.stripe_size();

    if present.len() < k {
        return Err(Error::NotEnoughShards {
            stripe,
            present: present.len(),
            needed: k,
        });
    }

    // Validate wire lengths and zero-pad legitimate short final data shards
    // up to the coding width. Any other shortfall is a length mismatch.
    let mut padded: BTreeMap<u8, Vec<u8>> = BTreeMap::new();
    for (&shard, bytes) in present {
        let expected = header.shard_len(stripe, shard);
        if (bytes.len() as u64) < expected {
            return Err(Error::LengthMismatch {
                stripe,
                shard,
                got: bytes.len() as u64,
                expected,
            });
        }
        let mut buf = bytes.clone();
        if buf.len() < s {
            buf.resize(s, 0);
        }
        padded.insert(shard, buf);
    }

    let recovered = coding::recover_stripe(k, m, stripe, s, &padded)?;

    // Emit data shards in id order with padding stripped.
    for shard in 0..k as u8 {
        let wire_len = header.shard_len(stripe, shard) as usize;
        let data = match present.get(&shard) {
            Some(bytes) => &bytes[..wire_len],
            None => &recovered[&shard][..wire_len],
        };
        if *bytes_written + wire_len as u64 > max_output {
            return Err(Error::OutputLimitExceeded {
                limit: max_output,
                attempted: *bytes_written + wire_len as u64,
            });
        }
        output.write_all(data)?;
        hasher.update(data);
        *bytes_written += wire_len as u64;
    }
    Ok(())
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}
