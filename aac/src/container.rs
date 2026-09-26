//! Self-describing container for coded payloads: the `AC01` format.
//!
//! The length metadata lives in a fixed **trailer** rather than a header, so
//! encoding can stream source bytes to coded bytes with constant memory — no
//! length needs to be known before the payload is produced.
//!
//! Layout (all multi-byte integers big-endian):
//!
//! ```text
//! offset   size  field
//! 0        4     begin magic   ASCII "AC01"
//! 4        1     version       0x01
//! 5        N     payload       ceil(coded_bits/8) bytes, MSB-first bits;
//!                             unused low bits of the final byte are zero
//!                             padding
//! 5+N      8     coded_bits    exact number of payload bits (u64 BE)
//! 13+N     8     original_length  source byte count (u64 BE)
//! 21+N     4     crc32         CRC-32/IEEE over every preceding byte
//! 25+N     4     end magic     ASCII "ACED"
//! ```
//!
//! The footer is therefore exactly [`FOOTER_LEN`] (24) bytes. Truncation is
//! detected by the missing end magic / failing CRC; the bit-length field
//! validates final-byte padding; the original-length field cross-checks the
//! decoder result. Payload bit semantics are described in `FORMAT.md`.

use crate::coder::{Decoder, Encoder, Stats};
use crate::constants::DEFAULT_MAX_OUTPUT;
use crate::error::{Error, Result};
use std::io::{Read, Write};

/// Magic prefix of every container.
pub const MAGIC: &[u8; 4] = b"AC01";
/// Magic suffix closing every container.
pub const END_MAGIC: &[u8; 4] = b"ACED";
/// Container format version produced and accepted by this implementation.
pub const VERSION: u8 = 1;
/// Fixed prefix length: magic + version.
pub const PREFIX_LEN: usize = 5;
/// Fixed trailer length: coded_bits(8) + original_length(8) + crc(4) + magic(4).
pub const FOOTER_LEN: usize = 24;

fn be_u64(buf: &[u8]) -> u64 {
    let mut v = 0u64;
    for &b in buf {
        v = (v << 8) | u64::from(b);
    }
    v
}

/// IEEE CRC-32 (poly 0xEDB88320 reflected), table-driven, implemented from
/// scratch — no external dependency.
#[derive(Clone)]
pub struct Crc32 {
    table: [u32; 256],
    state: u32,
}

impl Crc32 {
    /// Build the lookup table and initialize the CRC state.
    pub fn new() -> Self {
        let mut table = [0u32; 256];
        for (i, slot) in table.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 {
                    0xEDB88320 ^ (c >> 1)
                } else {
                    c >> 1
                };
            }
            *slot = c;
        }
        Crc32 {
            table,
            state: 0xFFFF_FFFF,
        }
    }

    /// Feed one chunk.
    pub fn update(&mut self, data: &[u8]) {
        for &b in data {
            let idx = ((self.state ^ u32::from(b)) & 0xFF) as usize;
            self.state = (self.state >> 8) ^ self.table[idx];
        }
    }

    /// Final CRC value.
    pub fn finish(self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }

    /// One-shot CRC of a buffer.
    pub fn checksum(data: &[u8]) -> u32 {
        let mut c = Crc32::new();
        c.update(data);
        c.finish()
    }
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

/// Encode `input` into a complete `AC01` container, optionally bounding the
/// coded payload size.
pub fn pack(input: &[u8], max_payload_bytes: Option<u64>) -> Result<(Vec<u8>, Stats)> {
    let mut enc = Encoder::new(Vec::new());
    if let Some(limit) = max_payload_bytes {
        enc = enc.with_output_limit(limit);
    }
    enc.write_all_bytes(input)?;
    let (payload, stats) = enc.finish()?;

    let mut out = Vec::with_capacity(PREFIX_LEN + payload.len() + FOOTER_LEN);
    out.extend_from_slice(MAGIC);
    out.push(VERSION);
    out.extend_from_slice(&payload);
    out.extend_from_slice(&stats.coded_bits.to_be_bytes());
    out.extend_from_slice(&(input.len() as u64).to_be_bytes());
    let crc = Crc32::checksum(&out);
    out.extend_from_slice(&crc.to_be_bytes());
    out.extend_from_slice(END_MAGIC);
    Ok((out, stats))
}

/// Validate an `AC01` container and decode its payload.
///
/// `max_output_bytes` bounds decoded length independently of the
/// `original_length` field (the field is untrusted input).
pub fn unpack(container: &[u8], max_output_bytes: Option<u64>) -> Result<(Vec<u8>, Stats)> {
    if container.len() < PREFIX_LEN + FOOTER_LEN {
        return Err(Error::InvalidFormat(format!(
            "file too short: {} bytes, need at least {}",
            container.len(),
            PREFIX_LEN + FOOTER_LEN
        )));
    }
    if &container[0..4] != MAGIC {
        return Err(Error::InvalidFormat(
            "bad begin magic: expected \"AC01\"".to_string(),
        ));
    }
    let version = container[4];
    if version != VERSION {
        return Err(Error::InvalidFormat(format!(
            "unsupported version {version}, supported: {VERSION}"
        )));
    }
    let footer = &container[container.len() - FOOTER_LEN..];
    if &footer[20..24] != END_MAGIC {
        return Err(Error::InvalidFormat(
            "missing end magic \"ACED\" (stream truncated?)".to_string(),
        ));
    }
    let coded_bits = be_u64(&footer[0..8]);
    let original_length = be_u64(&footer[8..16]);
    let crc_stored = u32::from_be_bytes([footer[16], footer[17], footer[18], footer[19]]);
    let crc_calc = Crc32::checksum(&container[..container.len() - 8]);
    if crc_stored != crc_calc {
        return Err(Error::InvalidFormat(format!(
            "CRC mismatch: stored {crc_stored:08x}, computed {crc_calc:08x} \
             (file truncated or corrupted)"
        )));
    }

    let limit = max_output_bytes.unwrap_or(DEFAULT_MAX_OUTPUT);
    if original_length > limit {
        return Err(Error::OutputLimitExceeded { limit });
    }

    let payload = &container[PREFIX_LEN..container.len() - FOOTER_LEN];
    let payload_bytes = payload.len() as u64;
    if coded_bits == 0 || (coded_bits + 7) / 8 != payload_bytes {
        return Err(Error::InvalidFormat(format!(
            "coded_bits={coded_bits} inconsistent with payload length {payload_bytes}"
        )));
    }
    // Padding bits in the final payload byte must be zero.
    let pad = (8 - coded_bits % 8) % 8;
    if pad > 0 {
        let last = *payload.last().unwrap();
        if last & ((1u8 << pad) - 1) != 0 {
            return Err(Error::InvalidFormat(
                "non-zero padding bits in final payload byte".to_string(),
            ));
        }
    }

    let dec = Decoder::new(payload).with_output_limit(limit);
    let (data, stats) = dec.decode_to_vec()?;
    if data.len() as u64 != original_length {
        return Err(Error::InvalidFormat(format!(
            "decoded length {} does not match footer field {original_length}",
            data.len()
        )));
    }
    Ok((data, stats))
}

// ---------------------------------------------------------------------------
// Streaming variants (constant memory)
// ---------------------------------------------------------------------------

/// A `Write` adapter that CRCs everything passing through it.
struct CrcWriter<'a, W: Write> {
    inner: W,
    crc: &'a mut Crc32,
}

impl<W: Write> Write for CrcWriter<'_, W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.crc.update(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

/// Stream-encode `input` as an `AC01` container to `out` with fixed-size
/// buffers. An optional payload cap aborts with
/// [`Error::OutputLimitExceeded`].
pub fn pack_stream<R: Read, W: Write>(
    mut input: R,
    out: W,
    max_payload_bytes: Option<u64>,
) -> Result<Stats> {
    let mut crc = Crc32::new();
    let mut counted = CrcWriter {
        inner: out,
        crc: &mut crc,
    };

    counted.write_all(MAGIC)?;
    counted.write_all(&[VERSION])?;

    let mut enc = Encoder::new(&mut counted);
    if let Some(limit) = max_payload_bytes {
        enc = enc.with_output_limit(limit);
    }
    let mut buf = [0u8; crate::IO_CHUNK];
    let mut original_length = 0u64;
    loop {
        let n = input.read(&mut buf)?;
        if n == 0 {
            break;
        }
        enc.write_all_bytes(&buf[..n])?;
        original_length += n as u64;
    }
    let (_, stats) = enc.finish()?;

    // CRC covers prefix, payload and the two length fields — but not the CRC
    // itself nor the end magic.
    counted.write_all(&stats.coded_bits.to_be_bytes())?;
    counted.write_all(&original_length.to_be_bytes())?;
    let crc_value = counted.crc.clone().finish();
    counted.inner.write_all(&crc_value.to_be_bytes())?;
    counted.inner.write_all(END_MAGIC)?;
    counted.inner.flush()?;
    debug_assert_eq!(stats.input_bytes, original_length);
    Ok(stats)
}

/// A `Read` adapter that serves every byte of the underlying stream **except
/// the final [`FOOTER_LEN`]**, which it withholds in an internal sliding
/// window. Served bytes are folded into a running CRC. This makes decoding
/// streamable with constant memory while the footer stays available for
/// validation.
struct FooterReader<R: Read> {
    inner: R,
    /// Sliding window holding the most recent bytes (shift buffer).
    buf: [u8; FOOTER_LEN],
    /// Valid bytes currently held (`0..=FOOTER_LEN`).
    fill: usize,
    /// Underlying stream reached EOF.
    eof: bool,
    /// Running CRC over every byte served plus the seeded prefix.
    crc: Crc32,
    /// Number of payload bytes served so far.
    served: u64,
    /// Last payload byte served (for final-byte padding validation).
    last_served: Option<u8>,
}

impl<R: Read> FooterReader<R> {
    /// Wrap a reader, seeding the CRC with already-consumed prefix bytes.
    fn with_prefix(inner: R, prefix: &[u8]) -> Self {
        let mut crc = Crc32::new();
        crc.update(prefix);
        FooterReader {
            inner,
            buf: [0u8; FOOTER_LEN],
            fill: 0,
            eof: false,
            crc,
            served: 0,
            last_served: None,
        }
    }

    #[inline]
    fn payload_bytes_served(&self) -> u64 {
        self.served
    }

    #[inline]
    fn last_payload_byte(&self) -> Option<u8> {
        self.last_served
    }

    /// The withheld footer (valid once the underlying stream hit EOF).
    fn footer(&self) -> &[u8] {
        &self.buf[..self.fill]
    }
}

impl<R: Read> Read for FooterReader<R> {
    fn read(&mut self, dst: &mut [u8]) -> std::io::Result<usize> {
        if dst.is_empty() {
            return Ok(0);
        }
        let mut n = 0usize;
        while n < dst.len() {
            // First, fill the window.
            while self.fill < FOOTER_LEN && !self.eof {
                let mut one = [0u8; 1];
                match self.inner.read(&mut one)? {
                    0 => self.eof = true,
                    _ => {
                        self.buf[self.fill] = one[0];
                        self.fill += 1;
                    }
                }
            }
            if self.eof {
                // The stream ended; everything still in the window is part of
                // (a possibly truncated) footer and must not be served.
                break;
            }
            // Window is full: serve its oldest byte only if at least one more
            // byte follows beyond the window.
            let mut one = [0u8; 1];
            match self.inner.read(&mut one)? {
                0 => {
                    self.eof = true;
                    break;
                }
                _ => {
                    let out = self.buf[0];
                    self.buf.copy_within(1.., 0);
                    self.buf[FOOTER_LEN - 1] = one[0];
                    dst[n] = out;
                    n += 1;
                    self.served += 1;
                    self.last_served = Some(out);
                    self.crc.update(core::slice::from_ref(&out));
                }
            }
        }
        Ok(n)
    }
}

/// Stream-decode an `AC01` container read from `input` into `out` with
/// constant-size buffers, verifying prefix, footer, CRC, bit padding and
/// original length. Truncation yields [`Error::InvalidFormat`] or
/// [`Error::UnexpectedEnd`].
pub fn unpack_stream<R: Read, W: Write>(
    mut input: R,
    out: &mut W,
    max_output_bytes: Option<u64>,
) -> Result<Stats> {
    // The fixed prefix is read directly (not through the window) and then
    // seeded into the reader's CRC.
    let mut prefix = [0u8; PREFIX_LEN];
    read_full(&mut input, &mut prefix)?;
    if &prefix[0..4] != MAGIC {
        return Err(Error::InvalidFormat(
            "bad begin magic: expected \"AC01\"".to_string(),
        ));
    }
    if prefix[4] != VERSION {
        return Err(Error::InvalidFormat(format!(
            "unsupported version {}, supported: {VERSION}",
            prefix[4]
        )));
    }

    let limit = max_output_bytes.unwrap_or(DEFAULT_MAX_OUTPUT);
    let mut fr = FooterReader::with_prefix(input, &prefix);

    let stats = {
        let dec = Decoder::new(&mut fr).with_output_limit(limit);
        dec.decode_to_writer(out)?
    };

    // Drain the rest of the payload so the sliding window holds exactly the
    // final FOOTER_LEN bytes. Constant memory: discarded into a small buffer.
    let mut sink = [0u8; 512];
    loop {
        let n = fr.read(&mut sink)?;
        if n == 0 {
            break;
        }
    }

    if fr.footer().len() < FOOTER_LEN {
        return Err(Error::InvalidFormat(
            "container ends before the fixed footer (truncated)".to_string(),
        ));
    }
    let footer = fr.footer();
    if &footer[20..24] != END_MAGIC {
        return Err(Error::InvalidFormat(
            "missing end magic \"ACED\" (stream truncated?)".to_string(),
        ));
    }
    let coded_bits = be_u64(&footer[0..8]);
    let original_length = be_u64(&footer[8..16]);
    let crc_stored = u32::from_be_bytes([footer[16], footer[17], footer[18], footer[19]]);

    if coded_bits == 0 {
        return Err(Error::InvalidFormat(
            "coded_bits must be non-zero".to_string(),
        ));
    }
    let expected_payload_bytes = (coded_bits + 7) / 8;
    if fr.payload_bytes_served() != expected_payload_bytes {
        return Err(Error::InvalidFormat(format!(
            "coded_bits={coded_bits} implies {expected_payload_bytes} payload bytes, \
             stream contained {}",
            fr.payload_bytes_served()
        )));
    }
    // Padding bits in the final payload byte must be zero.
    let pad = (8 - coded_bits % 8) % 8;
    if pad > 0 {
        let last = fr.last_payload_byte().unwrap_or(0);
        if last & ((1u8 << pad) - 1) != 0 {
            return Err(Error::InvalidFormat(
                "non-zero padding bits in final payload byte".to_string(),
            ));
        }
    }
    if original_length > limit {
        return Err(Error::OutputLimitExceeded { limit });
    }

    // CRC covers prefix + payload + coded_bits + original_length (footer[0..16]).
    let mut crc = fr.crc.clone();
    crc.update(&footer[0..16]);
    let crc_calc = crc.finish();
    if crc_stored != crc_calc {
        return Err(Error::InvalidFormat(format!(
            "CRC mismatch: stored {crc_stored:08x}, computed {crc_calc:08x} \
             (file truncated or corrupted)"
        )));
    }

    if stats.output_bytes != original_length {
        return Err(Error::InvalidFormat(format!(
            "decoded length {} does not match footer field {original_length}",
            stats.output_bytes
        )));
    }
    Ok(stats)
}

/// Fill `buf` completely from `r`, converting a short read into a format
/// error.
fn read_full<R: Read>(r: &mut R, mut buf: &mut [u8]) -> Result<()> {
    while !buf.is_empty() {
        match r.read(buf)? {
            0 => {
                return Err(Error::InvalidFormat(
                    "unexpected end of container before payload/footer".to_string(),
                ))
            }
            n => buf = &mut buf[n..],
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_known_vector() {
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF43926);
    }

    #[test]
    fn footer_layout_constants() {
        assert_eq!(MAGIC, b"AC01");
        assert_eq!(END_MAGIC, b"ACED");
        assert_eq!(FOOTER_LEN, 24);
        assert_eq!(PREFIX_LEN, 5);
    }

    #[test]
    fn truncated_container_is_rejected() {
        let (blob, _) = pack(b"hello truncation world hello", None).unwrap();
        for cut in 0..blob.len() {
            assert!(
                unpack(&blob[..cut], None).is_err(),
                "truncation at {cut} unexpectedly accepted"
            );
        }
    }

    #[test]
    fn corrupted_byte_is_rejected() {
        let (mut blob, _) = pack(b"corruption test payload", None).unwrap();
        blob[PREFIX_LEN + 3] ^= 0xFF;
        assert!(matches!(unpack(&blob, None), Err(Error::InvalidFormat(_))));
    }

    #[test]
    fn bad_magic_and_version_rejected() {
        let (mut blob, _) = pack(b"x", None).unwrap();
        blob[0] = b'X';
        assert!(unpack(&blob, None).is_err());
        let (mut blob, _) = pack(b"x", None).unwrap();
        blob[4] = 9;
        assert!(unpack(&blob, None).is_err());
        // Broken end magic.
        let (mut blob, _) = pack(b"x", None).unwrap();
        let last = blob.len() - 1;
        blob[last] ^= 0x01;
        assert!(unpack(&blob, None).is_err());
    }

    #[test]
    fn streaming_and_buffered_containers_are_identical() {
        for data in [&b""[..], &b"abc"[..], &vec![9u8; 5000][..]] {
            let (buffered, bs) = pack(data, None).unwrap();
            let mut streamed = Vec::new();
            let ss = pack_stream(data, &mut streamed, None).unwrap();
            assert_eq!(buffered, streamed);
            assert_eq!(bs.coded_bits, ss.coded_bits);

            let mut out = Vec::new();
            let ds = unpack_stream(streamed.as_slice(), &mut out, None).unwrap();
            assert_eq!(out, data);
            assert_eq!(ds.rescales, ss.rescales);
        }
    }
}
