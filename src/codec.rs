//! Streaming encoder and decoder for the ECSR container.
//!
//! Memory use is bounded to one stripe at a time: (k+m) * stripe_size bytes,
//! additionally capped by `max_stripe_memory`. Decoded output length is
//! capped by the caller-supplied `max_output_bytes`.

use crate::error::{Error, Result};
use crate::format::{self, Header, DEFAULT_MAX_STRIPE_MEMORY, HARD_MAX_OUTPUT_BYTES};
use crate::stripe::StripeCoder;
use std::io::{Read, Write};

/// Parameters for one encode run.
#[derive(Debug, Clone, Copy)]
pub struct EncodeParams {
    pub data_shards: u8,
    pub parity_shards: u8,
    pub stripe_size: u32,
    /// Cap on (k+m)*stripe_size; defaults to DEFAULT_MAX_STRIPE_MEMORY.
    pub max_stripe_memory: u64,
}

impl Default for EncodeParams {
    fn default() -> Self {
        EncodeParams {
            data_shards: 4,
            parity_shards: 2,
            stripe_size: 4096,
            max_stripe_memory: DEFAULT_MAX_STRIPE_MEMORY,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EncodeStats {
    pub input_bytes: u64,
    pub output_bytes: u64,
    pub stripes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodeStats {
    pub output_bytes: u64,
    pub stripes: u64,
    pub reconstructed_shards: u64,
}

/// Streaming-encode `input_len` bytes from `reader` into an ECSR container
/// on `writer`. `input_len` must be the exact number of bytes available;
/// it is recorded in the header so the decoder can strip padding.
pub fn encode<R: Read, W: Write>(
    params: &EncodeParams,
    input_len: u64,
    mut reader: R,
    mut writer: W,
) -> Result<EncodeStats> {
    let header = Header {
        data_shards: params.data_shards,
        parity_shards: params.parity_shards,
        stripe_size: params.stripe_size,
        original_len: input_len,
    };
    header.validate(params.max_stripe_memory)?;

    let k = header.data_shards as usize;
    let m = header.parity_shards as usize;
    let stripe_size = header.stripe_size as usize;
    let coder = StripeCoder::new(k, m)?;

    header.write_to(&mut writer)?;
    let mut output_bytes = format::HEADER_LEN as u64;

    let stripe_data = k * stripe_size;
    let mut data_buf = vec![0u8; stripe_data];
    let mut parity_buf = vec![0u8; m * stripe_size];
    let mut remaining = input_len;
    let mut stripes = 0u64;

    while remaining > 0 {
        let take = remaining.min(stripe_data as u64) as usize;
        reader.read_exact(&mut data_buf[..take])?;
        // Zero-pad the tail of the last stripe.
        data_buf[take..].fill(0);
        remaining -= take as u64;

        {
            let data_refs: Vec<&[u8]> = data_buf.chunks_exact(stripe_size).collect();
            let mut parity_refs: Vec<&mut [u8]> =
                parity_buf.chunks_exact_mut(stripe_size).collect();
            coder.encode_parity(&data_refs, &mut parity_refs);
        }

        for block in data_buf.chunks_exact(stripe_size) {
            format::write_block(&mut writer, block)?;
            output_bytes += 4 + stripe_size as u64;
        }
        for block in parity_buf.chunks_exact(stripe_size) {
            format::write_block(&mut writer, block)?;
            output_bytes += 4 + stripe_size as u64;
        }
        stripes += 1;
    }

    Ok(EncodeStats {
        input_bytes: input_len,
        output_bytes,
        stripes,
    })
}

/// Streaming-decode an ECSR container from `reader`, writing the recovered
/// original bytes to `writer`.
///
/// * `missing_shards`: indices of shards to treat as erased (known erasures).
///   At most `parity_shards` of them are recoverable.
/// * `max_output_bytes`: hard cap on the decoded length (see
///   `DEFAULT_MAX_OUTPUT_BYTES` / `HARD_MAX_OUTPUT_BYTES` in [`crate::format`]).
pub fn decode<R: Read, W: Write>(
    mut reader: R,
    mut writer: W,
    missing_shards: &[u32],
    max_output_bytes: u64,
) -> Result<DecodeStats> {
    if max_output_bytes > HARD_MAX_OUTPUT_BYTES {
        return Err(Error::InvalidParams(format!(
            "max_output_bytes {max_output_bytes} exceeds hard ceiling {HARD_MAX_OUTPUT_BYTES}"
        )));
    }

    let header = Header::read_from(&mut reader)?;
    // The container is untrusted input: enforce the same limits as encode.
    header.validate(DEFAULT_MAX_STRIPE_MEMORY)?;

    let k = header.data_shards as usize;
    let m = header.parity_shards as usize;
    let n = k + m;
    let stripe_size = header.stripe_size as usize;

    if header.original_len > max_output_bytes {
        return Err(Error::OutputLimitExceeded {
            needed: header.original_len,
            limit: max_output_bytes,
        });
    }

    // Validate the erasure list.
    let mut missing = vec![false; n];
    let mut missing_count = 0usize;
    for &idx in missing_shards {
        if idx as usize >= n || missing[idx as usize] {
            return Err(Error::InvalidShardIndex(idx));
        }
        missing[idx as usize] = true;
        missing_count += 1;
    }
    if missing_count > m {
        return Err(Error::TooManyErasures {
            missing: missing_count,
            parity: m,
        });
    }

    let coder = StripeCoder::new(k, m)?;
    let num_stripes = header.num_stripes();
    let mut written = 0u64;
    let mut reconstructed = 0u64;

    // Reusable per-stripe buffers; `present[i] == false` marks an erasure.
    let mut blocks: Vec<Vec<u8>> = (0..n).map(|_| vec![0u8; stripe_size]).collect();

    for _ in 0..num_stripes {
        let mut stripe_blocks: Vec<Option<Vec<u8>>> = Vec::with_capacity(n);
        for (i, block) in blocks.iter_mut().enumerate() {
            format::read_block(&mut reader, header.stripe_size, block)?;
            if missing[i] {
                stripe_blocks.push(None);
            } else {
                stripe_blocks.push(Some(std::mem::take(block)));
            }
        }

        let missing_data_before = (0..k).filter(|&i| stripe_blocks[i].is_none()).count() as u64;
        coder.reconstruct_data(&mut stripe_blocks)?;
        reconstructed += missing_data_before;

        // Emit data shards in order, truncating the final stripe to
        // original_len so the zero padding never leaks into the output.
        for slot in stripe_blocks.iter().take(k) {
            let block = slot
                .as_ref()
                .expect("data shard present after reconstruct_data");
            let remaining = header.original_len - written;
            let take = remaining.min(stripe_size as u64) as usize;
            writer.write_all(&block[..take])?;
            written += take as u64;
        }

        // Return buffers for reuse in the next stripe.
        for (i, slot) in stripe_blocks.into_iter().enumerate() {
            blocks[i] = slot.unwrap_or_else(|| vec![0u8; stripe_size]);
        }
    }

    // The body must end exactly here; trailing bytes indicate corruption.
    let mut probe = [0u8; 1];
    if reader.read(&mut probe)? != 0 {
        return Err(Error::CorruptFormat("trailing bytes after body".to_string()));
    }

    Ok(DecodeStats {
        output_bytes: written,
        stripes: num_stripes,
        reconstructed_shards: reconstructed,
    })
}
