//! Composite sort keys: extraction from delimited byte records and
//! ascending/descending lexicographic comparison.
//!
//! A [`KeySpec`] is parsed from a textual grammar shared by the CLI and HTTP:
//!
//! ```text
//! spec      := [ field-spec ("," field-spec)* ]
//! field-spec := field-index [":" dir]
//! dir       := "asc" | "desc"          (default asc)
//! ```
//!
//! * Field 0 is the **whole line** (the delimiter is not searched); this
//!   matches `sort(1)` semantics where key 0 sorts by the entire record.
//! * Field indices `>= 1` split the line on the 1-byte delimiter
//!   (`,` by default, like CSV-without-quoting), so field 1 is the first
//!   column. Missing fields compare as empty bytes.
//! * Comparison is lexicographic on raw bytes (runs on any 8-bit data;
//!   for valid UTF-8 this equals code-point order). `desc` reverses the
//!   comparison of that one field.
//! * Records with equal keys tie-break on their original input sequence
//!   number in ascending order — this is what makes the sort stable and is
//!   applied by the caller (see [`crate::sort`]).

use crate::error::{Error, Result};

/// Sort direction of one key field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Order {
    Asc,
    Desc,
}

/// One component of a composite key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyField {
    pub field: usize,
    pub order: Order,
}

/// Parsed composite-key specification plus the column delimiter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeySpec {
    pub fields: Vec<KeyField>,
    pub delim: u8,
}

impl Default for KeySpec {
    /// Whole line, ascending — the `sort(1)` default.
    fn default() -> Self {
        KeySpec {
            fields: vec![KeyField {
                field: 0,
                order: Order::Asc,
            }],
            delim: b',',
        }
    }
}

impl KeySpec {
    /// Parse e.g. `2:desc,1:asc`. Empty / absent means whole-line ascending.
    pub fn parse(text: &str, delim: u8) -> Result<Self> {
        let mut fields = Vec::new();
        for part in text.split(',') {
            let part = part.trim();
            if part.is_empty() {
                continue;
            }
            let (idx_txt, dir_txt) = match part.split_once(':') {
                Some((a, b)) => (a.trim(), Some(b.trim())),
                None => (part, None),
            };
            let field = idx_txt
                .parse::<usize>()
                .map_err(|_| Error::Config(format!("invalid key field: {part}")))?;
            let order = match dir_txt {
                None | Some("asc") | Some("ascending") | Some("up") => Order::Asc,
                Some("desc") | Some("descending") | Some("down") => Order::Desc,
                Some(other) => {
                    return Err(Error::Config(format!(
                        "invalid sort direction '{other}' (want asc or desc)"
                    )))
                }
            };
            fields.push(KeyField { field, order });
        }
        if fields.is_empty() {
            fields.push(KeyField {
                field: 0,
                order: Order::Asc,
            });
        }
        Ok(KeySpec { fields, delim })
    }

    /// Extract every component of the key from one record.
    pub fn extract<'a>(&self, line: &'a [u8]) -> Vec<&'a [u8]> {
        self.fields
            .iter()
            .map(|f| extract_field(line, f.field, self.delim))
            .collect()
    }

    /// Compare extracted keys (without the sequence tie-break).
    pub fn compare_keys(&self, a: &[&[u8]], b: &[&[u8]]) -> std::cmp::Ordering {
        for (kf, (av, bv)) in self.fields.iter().zip(a.iter().zip(b.iter())) {
            let cmp = av.cmp(bv);
            if cmp != std::cmp::Ordering::Equal {
                return if kf.order == Order::Desc {
                    cmp.reverse()
                } else {
                    cmp
                };
            }
        }
        std::cmp::Ordering::Equal
    }
}

/// Slice out field `n` (0 = whole line, 1 = first column, ...).
/// Missing columns are an empty slice.
pub fn extract_field(line: &[u8], n: usize, delim: u8) -> &[u8] {
    if n == 0 {
        return line;
    }
    let mut col = 1usize;
    let mut start = 0usize;
    for (i, &b) in line.iter().enumerate() {
        if b == delim {
            if col == n {
                return &line[start..i];
            }
            col += 1;
            start = i + 1;
        }
    }
    if col == n {
        &line[start..]
    } else {
        &[][..]
    }
}

/// Number of line bytes that must be buffered before every requested field is
/// known, so giant lines can have their key resolved while the rest streams
/// from disk. It is the position just past the start of the last needed
/// column: delimiter index of the (max_field-1)'th delimiter + 1.
///
/// Returns `None` if all requested fields are present within `line`'s length;
/// in that case the caller still needs to find where the *last present*
/// column ends, which callers resolve via [`prefix_end_for_key`] (the whole
/// line when field 0 is requested).
pub fn key_needs_whole_line(spec: &KeySpec) -> bool {
    spec.fields.iter().any(|f| f.field == 0)
}

/// Given a prefix buffer `buf` of a single line (not including `\n`), decide
/// whether it already contains all requested key columns.
pub fn prefix_has_key(spec: &KeySpec, buf: &[u8]) -> bool {
    if key_needs_whole_line(spec) {
        false // whole-line key needs the entire record by definition
    } else {
        let max_field = spec.fields.iter().map(|f| f.field).max().unwrap_or(0);
        // Number of delimiters seen: field N starts after N-1 delimiters, and
        // the field extends until the next delimiter or newline. If fewer than
        // N-1 delimiters have been seen we must read more; if exactly N-1, the
        // column may still grow, but the scanner keeps buffering until either
        // the next delimiter or the line end — returning false here makes the
        // scanner continue until delimiter_count >= N-1 AND (the Nth column is
        // terminated). Termination is handled by the caller when it detects
        // the next delim or newline, so require N delimiters as "column N is
        // complete" only when the column actually exists in the buffer; if the
        // line ends first the scanner finalizes anyway.
        let delim_count = buf.iter().filter(|&&b| b == spec.delim).count();
        // We need the *value* of column max_field, which is complete once we
        // have seen at least max_field delimiters (the next column began) ...
        delim_count >= max_field
        // ... or the line has ended (scanner signals that separately by
        // finalizing the record).
    }
}

/// Comparator on `(key, seq)`: key first per spec, seq ascending for stability.
pub fn compare_records(
    spec: &KeySpec,
    a_key: &[&[u8]],
    a_seq: u64,
    b_key: &[&[u8]],
    b_seq: u64,
) -> std::cmp::Ordering {
    match spec.compare_keys(a_key, b_key) {
        std::cmp::Ordering::Equal => a_seq.cmp(&b_seq),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn field_extraction() {
        let line = b"alpha,beta,gamma";
        assert_eq!(extract_field(line, 0, b','), line);
        assert_eq!(extract_field(line, 1, b','), b"alpha");
        assert_eq!(extract_field(line, 2, b','), b"beta");
        assert_eq!(extract_field(line, 3, b','), b"gamma");
        assert_eq!(extract_field(line, 9, b','), b"");
        let pipe = b"a|b";
        assert_eq!(extract_field(pipe, 2, b'|'), b"b");
    }

    #[test]
    fn parse_spec() {
        let s = KeySpec::parse("2:desc,1", b',').unwrap();
        assert_eq!(s.fields.len(), 2);
        assert_eq!(
            s.fields[0],
            KeyField {
                field: 2,
                order: Order::Desc
            }
        );
        assert_eq!(
            s.fields[1],
            KeyField {
                field: 1,
                order: Order::Asc
            }
        );
        assert!(KeySpec::parse("1:sideways", b',').is_err());
        assert_eq!(KeySpec::parse("", b',').unwrap(), KeySpec::default());
    }

    #[test]
    fn composite_directions_and_stability() {
        // rows: (group, name)
        let rows: Vec<&[u8]> = vec![b"2,b", b"1,z", b"2,a", b"1,m"];
        // group asc, name desc
        let spec = KeySpec::parse("1:asc,2:desc", b',').unwrap();
        let mut indexed: Vec<(Vec<Vec<u8>>, usize)> = rows
            .iter()
            .map(|r| (spec.extract(r).into_iter().map(|b| b.to_vec()).collect(), 0))
            .collect();
        for (i, x) in indexed.iter_mut().enumerate() {
            x.1 = i;
        }
        indexed.sort_by(|a, b| {
            let ak: Vec<&[u8]> = a.0.iter().map(|v| v.as_slice()).collect();
            let bk: Vec<&[u8]> = b.0.iter().map(|v| v.as_slice()).collect();
            compare_records(&spec, &ak, a.1 as u64, &bk, b.1 as u64)
        });
        let order: Vec<usize> = indexed.iter().map(|x| x.1).collect();
        // 1,z then 1,m (name desc within group 1), then 2,b then 2,a
        assert_eq!(order, vec![1, 3, 0, 2]);
    }

    #[test]
    fn equal_keys_stable_by_seq() {
        let spec = KeySpec::default();
        let k: Vec<&[u8]> = vec![b"same"];
        assert_eq!(
            compare_records(&spec, &k, 5, &k, 6),
            std::cmp::Ordering::Less
        );
    }

    #[test]
    fn prefix_key_detection() {
        let spec = KeySpec::parse("2", b',').unwrap();
        assert!(!prefix_has_key(&spec, b"alpha"));
        assert!(!prefix_has_key(&spec, b"alpha,beta")); // col2 not terminated
        assert!(prefix_has_key(&spec, b"alpha,beta,"));
        assert!(prefix_has_key(&spec, b"alpha,beta,gamma")); // 2 delims -> col2 ended
        let whole = KeySpec::default();
        assert!(!prefix_has_key(&whole, b"anything at all"));
    }
}
