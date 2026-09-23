//! Composite key extraction and comparison.
//!
//! A key spec is a list of key parts. Each part selects one tab-separated
//! field of the line (field 0 is the whole line) and compares it bytewise,
//! optionally descending:
//!
//! ```text
//! "2:asc,1:desc"   -> first by field 2 ascending, then field 1 descending
//! "3"              -> field 3 ascending only
//! "0" / ""         -> whole line ascending
//! ```
//!
//! Separators: parts are separated by `;` and the field index from its
//! direction by `:` (this leaves `,` free inside data). Ties that remain after
//! all key parts are equal are broken by the record's original input sequence
//! number, always ascending — that is what makes the sort stable for duplicate
//! keys.

use crate::error::{Error, Result};
use crate::format::Record;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Asc,
    Desc,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KeyPart {
    /// 0 means the whole line; n >= 1 means the nth tab-separated field.
    pub field: usize,
    pub dir: Direction,
}

impl KeyPart {
    pub fn asc(field: usize) -> Self {
        KeyPart { field, dir: Direction::Asc }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeySpec {
    pub parts: Vec<KeyPart>,
}

impl KeySpec {
    /// Default: whole-line ascending.
    pub fn default_key() -> Self {
        KeySpec { parts: vec![KeyPart::asc(0)] }
    }

    /// Parses query/CLI syntax. Empty string means the default key.
    pub fn parse(spec: &str) -> Result<Self> {
        if spec.is_empty() {
            return Ok(Self::default_key());
        }
        let mut parts = Vec::new();
        for raw in spec.split(';') {
            let raw = raw.trim();
            if raw.is_empty() {
                return Err(Error::bad("empty key part"));
            }
            let (field_s, dir_s) = match raw.split_once(':') {
                Some((f, d)) => (f.trim(), Some(d.trim())),
                None => (raw, None),
            };
            let field: usize = field_s
                .parse()
                .map_err(|_| Error::bad(format!("invalid field index in key part '{raw}'")))?;
            let dir = match dir_s {
                None | Some("asc") | Some("1") => Direction::Asc,
                Some("desc") | Some("-1") => Direction::Desc,
                Some(other) => {
                    return Err(Error::bad(format!(
                        "invalid direction '{other}' in key part '{raw}' (want asc|desc)"
                    )))
                }
            };
            parts.push(KeyPart { field, dir });
        }
        if parts.is_empty() {
            return Err(Error::bad("key spec has no parts"));
        }
        Ok(KeySpec { parts })
    }

    pub fn canonical(&self) -> String {
        self.parts
            .iter()
            .map(|p| match p.dir {
                Direction::Asc => format!("{}:asc", p.field),
                Direction::Desc => format!("{}:desc", p.field),
            })
            .collect::<Vec<_>>()
            .join(";")
    }

    /// Compare two records by the composite key, then by sequence number
    /// (ascending) for stability.
    pub fn cmp(&self, a: &Record, b: &Record) -> std::cmp::Ordering {
        for part in &self.parts {
            let av = field_bytes(&a.line, part.field);
            let bv = field_bytes(&b.line, part.field);
            let ord = av.cmp(bv);
            if ord != std::cmp::Ordering::Equal {
                return match part.dir {
                    Direction::Asc => ord,
                    Direction::Desc => ord.reverse(),
                };
            }
        }
        // Stability: original input order always wins ties.
        a.seq.cmp(&b.seq)
    }
}

/// Returns the selected field as bytes. Field 0 is the whole line; fields
/// beyond the end of the record compare as empty (the usual SQL-ish rule).
fn field_bytes(line: &[u8], field: usize) -> &[u8] {
    if field == 0 {
        return line;
    }
    let mut start = 0;
    for i in 1..field {
        match line[start..].iter().position(|&b| b == b'\t') {
            Some(p) => start += p + 1,
            None => return &[],
        }
        let _ = i;
    }
    match line[start..].iter().position(|&b| b == b'\t') {
        Some(p) => &line[start..start + p],
        None => &line[start..],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering::*;

    fn rec(seq: u64, line: &str) -> Record {
        Record { seq, line: line.as_bytes().to_vec() }
    }

    #[test]
    fn parse_specs() {
        assert_eq!(KeySpec::parse("").unwrap(), KeySpec::default_key());
        assert_eq!(
            KeySpec::parse("2:desc;1:asc").unwrap().parts,
            vec![KeyPart { field: 2, dir: Direction::Desc }, KeyPart::asc(1)]
        );
        assert!(KeySpec::parse("x").is_err());
        assert!(KeySpec::parse("1:sideways").is_err());
    }

    #[test]
    fn whole_line_and_fields() {
        let k = KeySpec::parse("0").unwrap();
        assert_eq!(k.cmp(&rec(0, "apple"), &rec(1, "banana")), Less);

        let k = KeySpec::parse("1").unwrap();
        let a = rec(0, "zeta\t1");
        let b = rec(1, "alpha\t2");
        assert_eq!(k.cmp(&a, &b), Greater); // field 1: zeta > alpha

        let k = KeySpec::parse("2").unwrap();
        assert_eq!(k.cmp(&a, &b), Less); // field 2: 1 < 2
    }

    #[test]
    fn descending_and_composite() {
        let k = KeySpec::parse("1:desc;2:asc").unwrap();
        let a = rec(0, "x\t1");
        let b = rec(1, "x\t2");
        let c = rec(2, "y\t0");
        // y desc -> after x; within x, field2 1 before 2
        assert_eq!(k.cmp(&a, &b), Less);
        assert_eq!(k.cmp(&a, &c), Greater);
    }

    #[test]
    fn duplicate_keys_are_stable() {
        let k = KeySpec::parse("1").unwrap();
        let a = rec(5, "same\tx");
        let b = rec(9, "same\tx");
        assert_eq!(k.cmp(&a, &b), Less); // earlier seq first regardless of key
        assert_eq!(k.cmp(&b, &a), Greater);
    }

    #[test]
    fn missing_field_treated_as_empty() {
        let k = KeySpec::parse("5:desc").unwrap();
        let a = rec(0, "onlyonefield");
        let b = rec(1, "onlyonefield");
        assert_eq!(k.cmp(&a, &b), Less); // tie broken by seq
    }
}
