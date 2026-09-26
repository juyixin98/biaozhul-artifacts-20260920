//! The 32-bit unsigned integer set: a sorted map of high-16-bit keys to
//! hybrid [`Container`]s.
//!
//! # Wire format (`RBM1`)
//!
//! ```text
//! magic   4 bytes   b"RBM1"
//! version u8        0x01
//! flags   u8        reserved, must be 0x00
//! nkeys   varint    number of key/container pairs (>= 0)
//! repeat nkeys times, keys strictly ascending:
//!   key       u16le high 16 bits
//!   payload   one container, see container.rs
//! ```
//!
//! A decoded set never buffers more than the configured [`Limits`] allow:
//! the key count is validated before its pairs are read, every array's
//! declared count is validated before its elements are buffered, and a
//! running cardinality budget bounds the total decoded output.

use std::collections::BTreeMap;
use std::io::{Read, Write};

use crate::codec::{IoDecoder, IoEncoder, Reader, Writer};
use crate::container::Container;
use crate::error::{RbError, RbResult};

/// Magic bytes identifying an `RBM1` stream.
pub const MAGIC: [u8; 4] = *b"RBM1";
/// Supported format version.
pub const FORMAT_VERSION: u8 = 1;

/// Default ceiling on the number of non-empty containers in one set.
pub const DEFAULT_MAX_CONTAINERS: u64 = 65536;
/// Default ceiling on total decoded cardinality (output length).
pub const DEFAULT_MAX_VALUES: u64 = 1 << 28; // 268 million values

/// Decode / output budgets. A malicious stream can declare at most these
/// many keys and elements; exceeding either is an error before the bytes
/// are buffered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Maximum number of key/container pairs.
    pub max_containers: u64,
    /// Maximum summed cardinality across all containers.
    pub max_values: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_containers: DEFAULT_MAX_CONTAINERS,
            max_values: DEFAULT_MAX_VALUES,
        }
    }
}

impl Limits {
    /// Build with explicit ceilings.
    pub fn new(max_containers: u64, max_values: u64) -> Self {
        Limits {
            max_containers,
            max_values,
        }
    }
}

/// A set of `u32` values backed by sparse-array / bitmap containers.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Bitmap {
    containers: BTreeMap<u16, Container>,
}

impl Bitmap {
    /// Empty set.
    pub fn new() -> Self {
        Bitmap {
            containers: BTreeMap::new(),
        }
    }

    /// Build from any iterator of u32 values.
    pub fn from_values<I: IntoIterator<Item = u32>>(iter: I) -> Self {
        let mut bm = Bitmap::new();
        for v in iter {
            bm.insert(v);
        }
        bm
    }

    /// Total cardinality.
    pub fn len(&self) -> usize {
        self.containers.values().map(Container::len).sum()
    }

    /// True for the empty set.
    pub fn is_empty(&self) -> bool {
        self.containers.is_empty()
    }

    /// Number of non-empty containers (mostly for tests/stats).
    pub fn container_count(&self) -> usize {
        self.containers.len()
    }

    /// How many of the containers are currently dense bitmaps.
    pub fn bitmap_container_count(&self) -> usize {
        self.containers
            .values()
            .filter(|c| matches!(c, Container::Bitmap(..)))
            .count()
    }

    /// How many containers are currently sparse arrays.
    pub fn array_container_count(&self) -> usize {
        self.containers
            .values()
            .filter(|c| matches!(c, Container::Array(..)))
            .count()
    }

    /// Insert one value; true if it was newly added.
    pub fn insert(&mut self, value: u32) -> bool {
        let key = (value >> 16) as u16;
        let low = value as u16;
        self.containers.entry(key).or_default().insert(low)
    }

    /// Remove one value; true if it was present. Empty containers vanish.
    pub fn remove(&mut self, value: u32) -> bool {
        let key = (value >> 16) as u16;
        let low = value as u16;
        let removed = match self.containers.get_mut(&key) {
            Some(c) => c.remove(low),
            None => false,
        };
        if matches!(self.containers.get(&key), Some(c) if c.is_empty()) {
            self.containers.remove(&key);
        }
        removed
    }

    /// Membership test.
    pub fn contains(&self, value: u32) -> bool {
        let key = (value >> 16) as u16;
        let low = value as u16;
        match self.containers.get(&key) {
            Some(c) => c.contains(low),
            None => false,
        }
    }

    /// Remove all values.
    pub fn clear(&mut self) {
        self.containers.clear();
    }

    /// Ascending iterator over every value.
    pub fn iter(&self) -> BitmapIter<'_> {
        BitmapIter {
            outer: self.containers.iter(),
            inner: None,
            current_key: 0,
        }
    }

    /// Collect all values into a `Vec` (caller bounds the size, e.g. via
    /// [`Limits::max_values`]).
    pub fn to_vec(&self, limit: usize) -> RbResult<Vec<u32>> {
        if self.len() > limit {
            return Err(RbError::LengthExceeded {
                what: "decoded output value list",
                declared: self.len() as u64,
                limit: limit as u64,
            });
        }
        Ok(self.iter().collect())
    }

    fn combine<F>(&self, other: &Bitmap, f: F) -> Bitmap
    where
        F: Fn(&Container, &Container) -> Container,
    {
        let mut out = BTreeMap::new();
        for (k, c) in self.containers.iter() {
            match other.containers.get(k) {
                Some(o) => {
                    let merged = f(c, o);
                    if !merged.is_empty() {
                        out.insert(*k, merged);
                    }
                }
                None => {
                    // Union identity needs `c`; intersection with an absent
                    // key is empty. The closure decides: we only carry a
                    // container through verbatim for union-like callers via
                    // the dedicated entry points below.
                    let merged = f(c, &Container::new());
                    if !merged.is_empty() {
                        out.insert(*k, merged);
                    }
                }
            }
        }
        for (k, c) in other.containers.iter() {
            if self.containers.contains_key(k) {
                continue;
            }
            let merged = f(&Container::new(), c);
            if !merged.is_empty() {
                out.insert(*k, merged);
            }
        }
        Bitmap { containers: out }
    }

    /// Set union.
    pub fn union(&self, other: &Bitmap) -> Bitmap {
        self.combine(other, |a, b| a.union(b))
    }

    /// Set intersection.
    pub fn intersect(&self, other: &Bitmap) -> Bitmap {
        let mut out = BTreeMap::new();
        for (k, c) in self.containers.iter() {
            if let Some(o) = other.containers.get(k) {
                let merged = c.intersect(o);
                if !merged.is_empty() {
                    out.insert(*k, merged);
                }
            }
        }
        Bitmap { containers: out }
    }

    /// Asymmetric set difference: all values in `self` absent from `other`.
    pub fn difference(&self, other: &Bitmap) -> Bitmap {
        let mut out = BTreeMap::new();
        for (k, c) in self.containers.iter() {
            let merged = match other.containers.get(k) {
                Some(o) => c.difference(o),
                None => c.clone(),
            };
            if !merged.is_empty() {
                out.insert(*k, merged);
            }
        }
        Bitmap { containers: out }
    }

    /// Encode the full set to a fresh byte vector.
    pub fn encode(&self) -> Vec<u8> {
        let mut w = Writer::with_capacity(6 + self.encoded_len_hint());
        self.encode_into(&mut w);
        w.into_bytes()
    }

    /// Encode into an existing [`Writer`].
    pub fn encode_into(&self, w: &mut Writer) {
        w.bytes(&MAGIC);
        w.u8(FORMAT_VERSION);
        w.u8(0); // flags
        w.len_varint(self.containers.len() as u64);
        for (key, c) in self.containers.iter() {
            w.u16(*key);
            c.encode_into(w);
        }
    }

    /// Encode straight to any [`std::io::Write`] without buffering the
    /// whole document in memory.
    pub fn encode_stream<W: Write>(&self, out: &mut W) -> RbResult<()> {
        let mut enc = IoEncoder::new(out);
        enc.bytes(&MAGIC)?;
        enc.u8(FORMAT_VERSION)?;
        enc.u8(0)?;
        enc.len_varint(self.containers.len() as u64)?;
        for (key, c) in self.containers.iter() {
            enc.u16(*key)?;
            match c {
                Container::Array(a) => {
                    enc.u8(crate::container::TAG_ARRAY)?;
                    enc.len_varint(a.len() as u64)?;
                    for &v in a {
                        enc.bytes(&v.to_le_bytes())?;
                    }
                }
                Container::Bitmap(words, _) => {
                    enc.u8(crate::container::TAG_BITMAP)?;
                    for &word in words.iter() {
                        enc.bytes(&word.to_le_bytes())?;
                    }
                }
            }
        }
        enc.flush()
    }

    /// Rough encoded-size estimate for preallocation.
    fn encoded_len_hint(&self) -> usize {
        self.containers
            .values()
            .map(|c| match c {
                Container::Array(a) => 3 + 2 * a.len(),
                Container::Bitmap(..) => 1 + 8 * crate::container::BITMAP_WORDS,
            })
            .sum()
    }

    /// Decode from a borrowed byte slice under `limits`.
    pub fn decode(buf: &[u8], limits: Limits) -> RbResult<Bitmap> {
        let mut r = Reader::new(buf);
        let magic = r.take_n(4, "magic")?;
        if magic != MAGIC {
            return Err(RbError::InvalidInput(format!(
                "bad magic: expected {:?}, got {:?}",
                MAGIC, magic
            )));
        }
        let version = r.u8()?;
        if version != FORMAT_VERSION {
            return Err(RbError::InvalidInput(format!(
                "unsupported format version {version}"
            )));
        }
        let flags = r.u8()?;
        if flags != 0 {
            return Err(RbError::InvalidInput(format!(
                "unsupported flags 0x{flags:02x}"
            )));
        }
        let nkeys = r.bounded_varint(limits.max_containers, "container count")?;

        let mut bm = Bitmap::new();
        let mut prev_key: Option<u16> = None;
        let mut value_budget = limits.max_values;

        for _ in 0..nkeys {
            let key = r.u16()?;
            if let Some(p) = prev_key {
                if key <= p {
                    return Err(if key == p {
                        RbError::DuplicateKey(key)
                    } else {
                        RbError::OutOfOrderKeys {
                            prev: p,
                            current: key,
                        }
                    });
                }
            }
            prev_key = Some(key);

            let per_container = value_budget.min(65536);
            let container = Container::decode_from(&mut r, per_container)?;
            let clen = container.len() as u64;
            if clen > value_budget {
                return Err(RbError::LengthExceeded {
                    what: "decoded output values",
                    declared: limits.max_values - value_budget + clen,
                    limit: limits.max_values,
                });
            }
            value_budget -= clen;
            bm.containers.insert(key, container);
        }

        // Trailing garbage is rejected to keep the format self-delimiting.
        if r.remaining() != 0 {
            return Err(RbError::InvalidInput(format!(
                "{} trailing byte(s) after encoded set",
                r.remaining()
            )));
        }
        Ok(bm)
    }

    /// Decode from any [`std::io::Read`] under `limits`. Used by the JSON
    /// control entry once a base64 payload is in memory; streaming straight
    /// from a socket works the same way.
    pub fn decode_stream<R: Read>(input: &mut R, limits: Limits) -> RbResult<Bitmap> {
        let mut dec = IoDecoder::new(input);
        let mut magic = [0u8; 4];
        dec.take_into(&mut magic, "magic")?;
        if magic != MAGIC {
            return Err(RbError::InvalidInput(format!(
                "bad magic: expected {:?}, got {:?}",
                MAGIC, magic
            )));
        }
        let version = dec.u8()?;
        if version != FORMAT_VERSION {
            return Err(RbError::InvalidInput(format!(
                "unsupported format version {version}"
            )));
        }
        let flags = dec.u8()?;
        if flags != 0 {
            return Err(RbError::InvalidInput(format!(
                "unsupported flags 0x{flags:02x}"
            )));
        }
        let nkeys = dec.bounded_varint(limits.max_containers, "container count")?;

        let mut bm = Bitmap::new();
        let mut prev_key: Option<u16> = None;
        let mut value_budget = limits.max_values;

        for _ in 0..nkeys {
            let key = dec.u16()?;
            if let Some(p) = prev_key {
                if key <= p {
                    return Err(if key == p {
                        RbError::DuplicateKey(key)
                    } else {
                        RbError::OutOfOrderKeys {
                            prev: p,
                            current: key,
                        }
                    });
                }
            }
            prev_key = Some(key);

            let tag = dec.u8()?;
            let container = match tag {
                crate::container::TAG_ARRAY => {
                    // Per-container ceiling: at most the 65536 values of a
                    // key, and no more than the global output budget.
                    let per = value_budget.min(65536);
                    let n = dec.bounded_varint(per, "array element count")? as usize;
                    let mut a = Vec::with_capacity(n);
                    let mut prev_v: Option<u16> = None;
                    for _ in 0..n {
                        let v = dec.u16()?;
                        if let Some(p) = prev_v {
                            if v <= p {
                                return Err(RbError::InvalidInput(format!(
                                    "array values not strictly ascending: {v} after {p}"
                                )));
                            }
                        }
                        prev_v = Some(v);
                        a.push(v);
                    }
                    value_budget -= n as u64;
                    Container::Array(a)
                }
                crate::container::TAG_BITMAP => {
                    // Fixed 8 KiB regardless of cardinality, so no length
                    // prefix can inflate the allocation; the actual
                    // cardinality is checked against the output budget
                    // immediately after the words are read.
                    let mut words = Box::new([0u64; crate::container::BITMAP_WORDS]);
                    let mut card = 0u32;
                    for w in words.iter_mut() {
                        let mut buf = [0u8; 8];
                        dec.take_into(&mut buf, "bitmap word")?;
                        *w = u64::from_le_bytes(buf);
                        card += w.count_ones();
                    }
                    if card as u64 > value_budget {
                        return Err(RbError::LengthExceeded {
                            what: "decoded output values",
                            declared: limits.max_values - value_budget + card as u64,
                            limit: limits.max_values,
                        });
                    }
                    value_budget -= card as u64;
                    Container::Bitmap(words, card)
                }
                other => {
                    return Err(RbError::InvalidTag {
                        ctx: "container",
                        tag: other,
                    })
                }
            };
            bm.containers.insert(key, container);
        }
        Ok(bm)
    }
}

/// Ascending iterator over all values of a [`Bitmap`].
pub struct BitmapIter<'a> {
    outer: std::collections::btree_map::Iter<'a, u16, Container>,
    inner: Option<ContainerIterWrapper<'a>>,
    current_key: u16,
}

struct ContainerIterWrapper<'a>(crate::container::ContainerIter<'a>);

impl<'a> Iterator for BitmapIter<'a> {
    type Item = u32;

    fn next(&mut self) -> Option<u32> {
        loop {
            if let Some(it) = self.inner.as_mut() {
                if let Some(low) = it.0.next() {
                    return Some((u32::from(self.current_key) << 16) | u32::from(low));
                }
            }
            let (key, c) = self.outer.next()?;
            self.current_key = *key;
            self.inner = Some(ContainerIterWrapper(c.iter()));
        }
    }
}
