//! Hybrid containers for the low 16 bits of a 32-bit value.
//!
//! Two representations share one key space of 65536 possible values:
//!
//! * [`Container::Array`] — a sorted `Vec<u16>`, used while the container
//!   holds at most [`ARRAY_LIMIT`] values. Memory grows with cardinality.
//! * [`Container::Bitmap`] — a fixed 8192-byte bitmap (1024 × u64), used
//!   once cardinality exceeds the limit. Memory is constant.
//!
//! A container *upgrades* array → bitmap on insertion past the limit and
//! *downgrades* bitmap → array when removals drop cardinality back to the
//! limit. Both directions preserve exactly the same value set; only the
//! encoding changes. This is the classic Roaring Bitmap container scheme
//! ("游程位图" / run-compressed bitmap family), implemented from scratch.

use crate::error::{RbError, RbResult};

/// Maximum cardinality of an array container before it converts to bitmap.
pub const ARRAY_LIMIT: usize = 4096;

/// Number of u64 words in a bitmap container (65536 bits).
pub const BITMAP_WORDS: usize = 1024;

/// Serialized container kinds.
pub const TAG_ARRAY: u8 = 0x01;
/// Bitmap container tag.
pub const TAG_BITMAP: u8 = 0x02;

/// One container: the values of a single high-16-bit key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Container {
    /// Sorted sparse array of low-16 values.
    Array(Vec<u16>),
    /// Dense bitmap of 65536 bits plus cached cardinality.
    Bitmap(Box<[u64; BITMAP_WORDS]>, u32),
}

impl Container {
    /// Empty array container.
    pub fn new() -> Self {
        Container::Array(Vec::new())
    }

    /// Number of values currently stored.
    pub fn len(&self) -> usize {
        match self {
            Container::Array(v) => v.len(),
            Container::Bitmap(_, card) => *card as usize,
        }
    }

    /// True when no values are stored.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Membership test.
    pub fn contains(&self, v: u16) -> bool {
        match self {
            Container::Array(a) => a.binary_search(&v).is_ok(),
            Container::Bitmap(words, _) => {
                let (word, bit) = (v / 64, v % 64);
                words[word as usize] & (1u64 << bit) != 0
            }
        }
    }

    /// Insert `v`; returns true when the value was not already present.
    pub fn insert(&mut self, v: u16) -> bool {
        match self {
            Container::Array(a) => match a.binary_search(&v) {
                Ok(_) => false,
                Err(pos) => {
                    a.insert(pos, v);
                    if a.len() > ARRAY_LIMIT {
                        self.upgrade();
                    }
                    true
                }
            },
            Container::Bitmap(words, card) => {
                let (word, bit) = (v / 64, v % 64);
                let mask = 1u64 << bit;
                let w = &mut words[word as usize];
                if *w & mask != 0 {
                    return false;
                }
                *w |= mask;
                *card += 1;
                true
            }
        }
    }

    /// Remove `v`; returns true when the value was present.
    pub fn remove(&mut self, v: u16) -> bool {
        match self {
            Container::Array(a) => match a.binary_search(&v) {
                Ok(pos) => {
                    a.remove(pos);
                    true
                }
                Err(_) => false,
            },
            Container::Bitmap(words, card) => {
                let (word, bit) = (v / 64, v % 64);
                let mask = 1u64 << bit;
                let w = &mut words[word as usize];
                if *w & mask == 0 {
                    return false;
                }
                *w &= !mask;
                *card -= 1;
                if *card as usize <= ARRAY_LIMIT {
                    self.downgrade();
                }
                true
            }
        }
    }

    /// Convert array → bitmap in place. No-op when already a bitmap.
    fn upgrade(&mut self) {
        if let Container::Array(a) = self {
            let mut words = Box::new([0u64; BITMAP_WORDS]);
            for &v in a.iter() {
                words[(v / 64) as usize] |= 1u64 << (v % 64);
            }
            let card = a.len() as u32;
            *self = Container::Bitmap(words, card);
        }
    }

    /// Convert bitmap → array in place. No-op when already an array.
    fn downgrade(&mut self) {
        if let Container::Bitmap(words, card) = self {
            let mut a = Vec::with_capacity(*card as usize);
            for (wi, &word) in words.iter().enumerate() {
                let mut w = word;
                while w != 0 {
                    let bit = w.trailing_zeros() as u16;
                    a.push(wi as u16 * 64 + bit);
                    w &= w - 1;
                }
            }
            *self = Container::Array(a);
        }
    }

    /// Force the canonical representation for the current cardinality.
    /// Used after bulk operations that build a bitmap directly.
    pub fn normalize(&mut self) {
        // Compute both decisions before mutating so no pattern binding is
        // alive across the &mut self reborrow.
        let too_large_for_array = matches!(self, Container::Array(a) if a.len() > ARRAY_LIMIT);
        let small_enough_for_array =
            matches!(self, Container::Bitmap(_, card) if *card as usize <= ARRAY_LIMIT);
        if too_large_for_array {
            self.upgrade();
        } else if small_enough_for_array {
            self.downgrade();
        }
    }

    /// Iterate values in ascending order.
    pub fn iter(&self) -> ContainerIter<'_> {
        match self {
            Container::Array(a) => ContainerIter {
                inner: IterInner::Array(a.iter()),
            },
            Container::Bitmap(words, _) => ContainerIter {
                inner: IterInner::Bitmap {
                    words,
                    word_idx: 0,
                    current: words.first().copied().unwrap_or(0),
                },
            },
        }
    }

    /// Smallest value, if any.
    pub fn min(&self) -> Option<u16> {
        self.iter().next()
    }

    /// Largest value, if any.
    pub fn max(&self) -> Option<u16> {
        match self {
            Container::Array(a) => a.last().copied(),
            Container::Bitmap(words, _) => {
                for (wi, &word) in words.iter().enumerate().rev() {
                    if word != 0 {
                        return Some(wi as u16 * 64 + (63 - word.leading_zeros()) as u16);
                    }
                }
                None
            }
        }
    }

    /// Union: every value present in either container.
    pub fn union(&self, other: &Container) -> Container {
        use Container::*;
        let mut out = match (self, other) {
            (Bitmap(w1, _), Bitmap(w2, _)) => {
                let mut words = Box::new([0u64; BITMAP_WORDS]);
                let mut card = 0u32;
                for i in 0..BITMAP_WORDS {
                    words[i] = w1[i] | w2[i];
                    card += words[i].count_ones();
                }
                Bitmap(words, card)
            }
            (Array(a), Array(b)) => Array(merge_sorted(a, b)),
            (Array(a), Bitmap(w, _)) | (Bitmap(w, _), Array(a)) => {
                let mut words = w.clone();
                let mut card = 0u32;
                for &v in a {
                    words[(v / 64) as usize] |= 1u64 << (v % 64);
                }
                for &word in words.iter() {
                    card += word.count_ones();
                }
                Bitmap(words, card)
            }
        };
        out.normalize();
        out
    }

    /// Intersection: values present in both containers.
    pub fn intersect(&self, other: &Container) -> Container {
        use Container::*;
        let mut out = match (self, other) {
            (Bitmap(w1, _), Bitmap(w2, _)) => {
                let mut words = Box::new([0u64; BITMAP_WORDS]);
                let mut card = 0u32;
                for i in 0..BITMAP_WORDS {
                    words[i] = w1[i] & w2[i];
                    card += words[i].count_ones();
                }
                Bitmap(words, card)
            }
            (Array(a), Array(b)) => Array(intersect_sorted(a, b)),
            (Array(a), Bitmap(w, _)) | (Bitmap(w, _), Array(a)) => {
                let mut out = Vec::new();
                for &v in a {
                    if w[(v / 64) as usize] & (1u64 << (v % 64)) != 0 {
                        out.push(v);
                    }
                }
                Array(out)
            }
        };
        out.normalize();
        out
    }

    /// Difference: values in `self` that are absent from `other`.
    pub fn difference(&self, other: &Container) -> Container {
        use Container::*;
        let mut out = match (self, other) {
            (Array(a), Array(b)) => Array(difference_sorted(a, b)),
            (Array(a), Bitmap(w, _)) => {
                let mut out = Vec::new();
                for &v in a {
                    if w[(v / 64) as usize] & (1u64 << (v % 64)) == 0 {
                        out.push(v);
                    }
                }
                Array(out)
            }
            (Bitmap(w, _), Array(a)) => {
                let mut words = w.clone();
                for &v in a {
                    words[(v / 64) as usize] &= !(1u64 << (v % 64));
                }
                let mut card = 0u32;
                for &word in words.iter() {
                    card += word.count_ones();
                }
                Bitmap(words, card)
            }
            (Bitmap(w1, _), Bitmap(w2, _)) => {
                let mut words = Box::new([0u64; BITMAP_WORDS]);
                let mut card = 0u32;
                for i in 0..BITMAP_WORDS {
                    words[i] = w1[i] & !w2[i];
                    card += words[i].count_ones();
                }
                Bitmap(words, card)
            }
        };
        out.normalize();
        out
    }

    /// Encode this container (payload only; the key lives one level up).
    ///
    /// Layout: `tag:u8` then
    /// * array: `count:varint`, `count` × `u16le` ascending
    /// * bitmap: `1024` × `u64le` (cardinality is recomputed on decode)
    pub fn encode_into(&self, w: &mut crate::codec::Writer) {
        match self {
            Container::Array(a) => {
                w.u8(TAG_ARRAY);
                w.len_varint(a.len() as u64);
                for &v in a {
                    w.u16(v);
                }
            }
            Container::Bitmap(words, _) => {
                w.u8(TAG_BITMAP);
                for &word in words.iter() {
                    w.u64(word);
                }
            }
        }
    }

    /// Decode one container payload. `limit` bounds the declared array
    /// element count; it must never exceed 65536 by construction, but the
    /// caller's (possibly stricter) limit wins.
    pub fn decode_from(r: &mut crate::codec::Reader<'_>, limit: u64) -> RbResult<Container> {
        let tag = r.u8()?;
        match tag {
            TAG_ARRAY => {
                let cap = limit.min(65536);
                let n = r.bounded_varint(cap, "array element count")? as usize;
                let mut a = Vec::with_capacity(n);
                let mut prev: Option<u16> = None;
                for _ in 0..n {
                    let v = r.u16()?;
                    if let Some(p) = prev {
                        if v <= p {
                            return Err(RbError::InvalidInput(format!(
                                "array container values not strictly ascending: {v} after {p}"
                            )));
                        }
                    }
                    prev = Some(v);
                    a.push(v);
                }
                Ok(Container::Array(a))
            }
            TAG_BITMAP => {
                let mut words = Box::new([0u64; BITMAP_WORDS]);
                let mut card = 0u32;
                for w in words.iter_mut() {
                    *w = r.u64()?;
                    card += w.count_ones();
                }
                Ok(Container::Bitmap(words, card))
            }
            other => Err(RbError::InvalidTag {
                ctx: "container",
                tag: other,
            }),
        }
    }
}

impl Default for Container {
    fn default() -> Self {
        Container::new()
    }
}

enum IterInner<'a> {
    Array(std::slice::Iter<'a, u16>),
    Bitmap {
        words: &'a [u64; BITMAP_WORDS],
        word_idx: usize,
        current: u64,
    },
}

/// Ascending iterator over a container's values.
pub struct ContainerIter<'a> {
    inner: IterInner<'a>,
}

impl Iterator for ContainerIter<'_> {
    type Item = u16;

    fn next(&mut self) -> Option<u16> {
        match &mut self.inner {
            IterInner::Array(it) => it.next().copied(),
            IterInner::Bitmap {
                words,
                word_idx,
                current,
            } => loop {
                if *current != 0 {
                    let bit = current.trailing_zeros() as u16;
                    *current &= *current - 1;
                    return Some(*word_idx as u16 * 64 + bit);
                }
                *word_idx += 1;
                if *word_idx >= BITMAP_WORDS {
                    return None;
                }
                *current = words[*word_idx];
            },
        }
    }
}

/// Merge two sorted slices, deduplicating (set union).
fn merge_sorted(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        if a[i] < b[j] {
            out.push(a[i]);
            i += 1;
        } else if a[i] > b[j] {
            out.push(b[j]);
            j += 1;
        } else {
            out.push(a[i]);
            i += 1;
            j += 1;
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

/// Sorted intersection of two slices.
fn intersect_sorted(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        if a[i] < b[j] {
            i += 1;
        } else if a[i] > b[j] {
            j += 1;
        } else {
            out.push(a[i]);
            i += 1;
            j += 1;
        }
    }
    out
}

/// Sorted difference `a \ b`.
fn difference_sorted(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::with_capacity(a.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() {
        while j < b.len() && b[j] < a[i] {
            j += 1;
        }
        if j >= b.len() || b[j] != a[i] {
            out.push(a[i]);
        }
        i += 1;
    }
    out
}
