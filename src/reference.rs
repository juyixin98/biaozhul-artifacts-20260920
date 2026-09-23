//! In-memory reference sort used as the ground truth in acceptance tests.
//!
//! It applies the *same* composite-key comparator and sequence tie-break as
//! the external engine, but holds everything in memory and sorts once. Input
//! and output conventions match the engine: lines split on `\n`, a preceding
//! `\r` stripped at a CRLF boundary, and exactly one `\n` emitted per record.

use std::io::{Read, Write};

use crate::error::Result;
use crate::key::{compare_records, KeySpec};

struct RefRecord {
    line: Vec<u8>,
    keys: Vec<Vec<u8>>,
    seq: u64,
}

/// Sort all of `input` in memory and write normalized output.
pub fn reference_sort(input: &mut dyn Read, output: &mut dyn Write, spec: &KeySpec) -> Result<u64> {
    let mut data = Vec::new();
    input.read_to_end(&mut data)?;

    let mut records: Vec<RefRecord> = Vec::new();
    let mut seq = 0u64;
    let mut start = 0usize;
    for i in 0..data.len() {
        if data[i] == b'\n' {
            let mut line = &data[start..i];
            if line.last() == Some(&b'\r') {
                line = &line[..line.len() - 1];
            }
            let keys = spec.extract(line).into_iter().map(|k| k.to_vec()).collect();
            records.push(RefRecord {
                line: line.to_vec(),
                keys,
                seq,
            });
            seq += 1;
            start = i + 1;
        }
    }
    // Trailing line without newline.
    if start < data.len() {
        let line = &data[start..];
        let keys = spec.extract(line).into_iter().map(|k| k.to_vec()).collect();
        records.push(RefRecord {
            line: line.to_vec(),
            keys,
            seq,
        });
    }

    records.sort_by(|a, b| {
        let ak: Vec<&[u8]> = a.keys.iter().map(|k| k.as_slice()).collect();
        let bk: Vec<&[u8]> = b.keys.iter().map(|k| k.as_slice()).collect();
        compare_records(spec, &ak, a.seq, &bk, b.seq)
    });

    for r in &records {
        output.write_all(&r.line)?;
        output.write_all(b"\n")?;
    }
    output.flush()?;
    Ok(records.len() as u64)
}

/// Convenience: sort bytes and return sorted bytes.
pub fn reference_sort_bytes(data: &[u8], spec: &KeySpec) -> Result<Vec<u8>> {
    let mut out = Vec::with_capacity(data.len());
    reference_sort(&mut &data[..], &mut out, spec)?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reference_basic_and_stable() {
        let spec = KeySpec::parse("1", b',').unwrap();
        let data = b"2,b\n1,y\n2,a\n1,x\n";
        let out = reference_sort_bytes(data, &spec).unwrap();
        // group 1: input order y then x => stable keeps y,x ; group 2: b,a
        assert_eq!(out, b"1,y\n1,x\n2,b\n2,a\n");
    }
}
