//! Low-level segment framing for the `CDCT` binary format.
//!
//! Layout (see `docs/FORMAT.md` for the authoritative spec):
//!
//! ```text
//! file    := "CDCT" VERSION(=1) segment*
//! segment := "SEGM" dict_count:uvarint row_count:uvarint
//!            dict_count × (len:uvarint utf8_bytes)
//!            row_count  × (id:uvarint)        // 0 = NULL, n>0 -> dict[n-1]
//! ```
//!
//! NULL is represented by the reserved id `0`, independently of the
//! dictionary: dictionary entries are never overloaded to mean NULL, and a
//! segment consisting solely of NULLs carries an empty dictionary.

use crate::error::{Error, Limits, Result};
use crate::varint;
use std::io::{Read, Write};

/// Magic bytes starting every file.
pub const FILE_MAGIC: &[u8; 4] = b"CDCT";
/// Magic bytes starting every segment.
pub const SEGMENT_MAGIC: &[u8; 4] = b"SEGM";
/// Format version written after [`FILE_MAGIC`].
pub const VERSION: u8 = 1;
/// Reserved id meaning NULL. Dictionary entry `i` is referenced by id `i + 1`.
pub const NULL_ID: u32 = 0;

/// Parsed segment header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SegmentHeader {
    /// Number of dictionary entries in this segment.
    pub dict_entries: u64,
    /// Number of rows in this segment.
    pub rows: u64,
}

/// Write the file preamble (magic + version).
pub fn write_file_header<W: Write + ?Sized>(w: &mut W) -> Result<()> {
    w.write_all(FILE_MAGIC).map_err(Error::io)?;
    w.write_all(&[VERSION]).map_err(Error::io)?;
    Ok(())
}

/// Read and validate the file preamble.
pub fn read_file_header<R: Read + ?Sized>(r: &mut R) -> Result<()> {
    let mut magic = [0u8; 4];
    varint::read_exact_or_truncated(r, &mut magic)?;
    if &magic != FILE_MAGIC {
        return Err(Error::invalid("bad file magic (not a CDCT stream)"));
    }
    let mut version = [0u8; 1];
    varint::read_exact_or_truncated(r, &mut version)?;
    if version[0] != VERSION {
        return Err(Error::invalid(format!(
            "unsupported format version {} (expected {VERSION})",
            version[0]
        )));
    }
    Ok(())
}

/// Write a segment header (magic + counts).
pub fn write_segment_header<W: Write + ?Sized>(w: &mut W, header: &SegmentHeader) -> Result<()> {
    w.write_all(SEGMENT_MAGIC).map_err(Error::io)?;
    varint::write_uvarint_to(w, header.dict_entries).map_err(Error::io)?;
    varint::write_uvarint_to(w, header.rows).map_err(Error::io)?;
    Ok(())
}

/// Read a segment header.
///
/// Returns `Ok(None)` on clean end-of-stream (no bytes at all). A partial
/// magic is [`Error::Truncated`]; a wrong magic is [`Error::Invalid`], which
/// also catches trailing garbage after the last segment.
pub fn read_segment_header<R: Read + ?Sized>(r: &mut R) -> Result<Option<SegmentHeader>> {
    let mut magic = [0u8; 4];
    // Distinguish clean EOF from a truncated magic.
    let mut filled = 0usize;
    while filled < 4 {
        match r.read(&mut magic[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(Error::io(e)),
        }
    }
    if filled == 0 {
        return Ok(None);
    }
    if filled < 4 {
        return Err(Error::truncated("segment magic"));
    }
    if &magic != SEGMENT_MAGIC {
        return Err(Error::invalid("bad segment magic"));
    }
    let dict_entries = varint::read_uvarint_from(r)?;
    let rows = varint::read_uvarint_from(r)?;
    Ok(Some(SegmentHeader { dict_entries, rows }))
}

/// Read a segment dictionary of `count` entries, enforcing `limits`.
///
/// Validates UTF-8 and per-string/aggregate byte caps. The returned vector is
/// indexed by `id - 1` (id 0 is NULL and never appears here).
pub fn read_dictionary<R: Read + ?Sized>(
    r: &mut R,
    count: u64,
    limits: &Limits,
) -> Result<Vec<String>> {
    if count > limits.max_dict_entries {
        return Err(Error::limit(format!(
            "dictionary has {count} entries (max {})",
            limits.max_dict_entries
        )));
    }
    let mut dict = Vec::with_capacity(count as usize);
    let mut total_bytes: u64 = 0;
    for _ in 0..count {
        let len = varint::read_uvarint_from(r)?;
        if len > limits.max_string_bytes {
            return Err(Error::limit(format!(
                "dictionary string of {len} bytes (max {})",
                limits.max_string_bytes
            )));
        }
        total_bytes += len;
        if total_bytes > limits.max_dict_bytes {
            return Err(Error::limit(format!(
                "dictionary exceeds {} bytes",
                limits.max_dict_bytes
            )));
        }
        let raw = varint::read_vec_exact(r, len as usize)?;
        let s = String::from_utf8(raw)?;
        dict.push(s);
    }
    Ok(dict)
}

/// Write one dictionary entry (`len` + bytes).
pub fn write_dict_entry<W: Write + ?Sized>(w: &mut W, s: &str) -> Result<()> {
    varint::write_uvarint_to(w, s.len() as u64).map_err(Error::io)?;
    w.write_all(s.as_bytes()).map_err(Error::io)?;
    Ok(())
}

/// Write one row id.
pub fn write_id<W: Write + ?Sized>(w: &mut W, id: u32) -> Result<()> {
    varint::write_uvarint_to(w, id as u64).map_err(Error::io)
}

/// Read one row id, validating it against `dict_entries`.
pub fn read_id<R: Read + ?Sized>(r: &mut R, dict_entries: u64) -> Result<u32> {
    let raw = varint::read_uvarint_from(r)?;
    if raw > dict_entries {
        return Err(Error::bad_ref(format!(
            "row id {raw} but dictionary has {dict_entries} entries"
        )));
    }
    raw.try_into()
        .map_err(|_| Error::invalid(format!("row id {raw} exceeds u32 range")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_header_roundtrip() {
        let mut buf = Vec::new();
        write_file_header(&mut buf).unwrap();
        read_file_header(&mut &buf[..]).unwrap();
    }

    #[test]
    fn bad_file_magic_rejected() {
        let buf = b"XXXX\x01".to_vec();
        assert!(matches!(
            read_file_header(&mut &buf[..]),
            Err(Error::Invalid(_))
        ));
    }

    #[test]
    fn bad_version_rejected() {
        let buf = b"CDCT\x7f".to_vec();
        assert!(matches!(
            read_file_header(&mut &buf[..]),
            Err(Error::Invalid(_))
        ));
    }

    #[test]
    fn segment_header_eof_is_none() {
        let buf: &[u8] = &[];
        assert_eq!(read_segment_header(&mut &*buf).unwrap(), None);
    }

    #[test]
    fn truncated_segment_magic_is_error() {
        let buf: &[u8] = b"SE";
        assert!(matches!(
            read_segment_header(&mut &*buf),
            Err(Error::Truncated(_))
        ));
    }

    #[test]
    fn trailing_garbage_rejected() {
        let buf: &[u8] = b"GARBAGE!!";
        assert!(matches!(
            read_segment_header(&mut &*buf),
            Err(Error::Invalid(_))
        ));
    }

    #[test]
    fn id_above_dict_size_rejected() {
        // id 3 with only 2 dict entries
        let buf = [3u8];
        assert!(matches!(
            read_id(&mut &buf[..], 2),
            Err(Error::BadReference(_))
        ));
    }
}
