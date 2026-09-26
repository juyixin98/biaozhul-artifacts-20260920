//! The 32-bit unsigned integer set: a map from the high 16 bits to a
//! [`Container`] of the low 16 bits.
//!
//! Empty containers are never stored, so the representation is canonical:
//! a value belongs to the set iff its key's container exists and contains
//! its low bits.

use std::collections::BTreeMap;

use crate::container::Container;

/// Hybrid sparse-array / bitmap set of `u32` values.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct IntSet {
    /// Keyed by the high 16 bits; each value holds the low 16 bits.
    containers: BTreeMap<u16, Container>,
}

impl IntSet {
    /// Create an empty set.
    pub fn new() -> Self {
        Self::default()
    }

    /// Build from arbitrary values (sorts and de-duplicates).
    pub fn from_values(values: &[u32]) -> Self {
        let mut s = Self::new();
        for &v in values {
            s.insert(v);
        }
        // Make representation canonical at chunk boundaries.
        s.finalize_chunks();
        s
    }

    #[inline]
    fn split(v: u32) -> (u16, u16) {
        ((v >> 16) as u16, v as u16)
    }

    /// Insert one value.
    pub fn insert(&mut self, v: u32) {
        let (hi, lo) = Self::split(v);
        self.containers
            .entry(hi)
            .or_insert_with(Container::empty)
            .insert(lo);
    }

    /// Add one whole sorted, unique group for key `hi`.
    ///
    /// Used by the streaming decoder, which sees values grouped by high
    /// key in ascending order. The caller guarantees ascending, unique low
    /// values within the group.
    pub fn push_group(&mut self, hi: u16, lows: Vec<u16>) {
        if lows.is_empty() {
            return;
        }
        // from_sorted_trusted still picks the canonical representation.
        self.containers
            .insert(hi, Container::from_sorted_trusted(lows));
    }

    /// Add one pre-built bitmap group (used by the bitmap decode path).
    /// Sparse bitmaps are canonicalised back to arrays so that the in-memory
    /// representation depends only on membership, not on the wire encoding.
    pub fn push_bitmap_group(
        &mut self,
        hi: u16,
        words: Box<[u64; crate::container::BITMAP_WORDS]>,
    ) {
        self.containers
            .insert(hi, Container::Bitmap(words).optimize());
    }

    /// Iterate `(high_key, container)` pairs in ascending key order.
    pub fn iter_chunks(&self) -> impl Iterator<Item = (&u16, &Container)> {
        self.containers.iter()
    }

    /// Membership test.
    pub fn contains(&self, v: u32) -> bool {
        let (hi, lo) = Self::split(v);
        self.containers
            .get(&hi)
            .map(|c| c.contains(lo))
            .unwrap_or(false)
    }

    /// Number of distinct values.
    pub fn len(&self) -> u64 {
        self.containers.values().map(Container::cardinality).sum()
    }

    /// Whether the set is empty.
    pub fn is_empty(&self) -> bool {
        self.containers.is_empty()
    }

    /// Ascending iteration over all values.
    pub fn iter(&self) -> impl Iterator<Item = u32> + '_ {
        self.containers.iter().flat_map(|(&hi, c)| {
            c.iter()
                .map(move |lo| (u32::from(hi) << 16) | u32::from(lo))
        })
    }

    /// Collect all values in ascending order.
    pub fn to_vec(&self) -> Vec<u32> {
        self.iter().collect()
    }

    /// High keys with a (non-empty) container, ascending.
    pub fn keys(&self) -> impl Iterator<Item = u16> + '_ {
        self.containers.keys().copied()
    }

    /// Borrow the container for one high key, if present.
    pub fn container(&self, hi: u16) -> Option<&Container> {
        self.containers.get(&hi)
    }

    /// Number of non-empty containers (chunk count).
    pub fn chunk_count(&self) -> usize {
        self.containers.len()
    }

    // --- set algebra -----------------------------------------------------

    /// Set union: values in either operand.
    pub fn union(&self, other: &IntSet) -> IntSet {
        let mut out = BTreeMap::new();
        for (&hi, c) in &self.containers {
            match other.containers.get(&hi) {
                Some(o) => out.insert(hi, c.union(o)),
                None => out.insert(hi, c.clone()),
            };
        }
        for (&hi, c) in &other.containers {
            out.entry(hi).or_insert_with(|| c.clone());
        }
        IntSet { containers: out }
    }

    /// Set intersection: values in both operands.
    pub fn intersection(&self, other: &IntSet) -> IntSet {
        let (small, large) = if self.containers.len() <= other.containers.len() {
            (self, other)
        } else {
            (other, self)
        };
        let mut out = BTreeMap::new();
        for (&hi, c) in &small.containers {
            if let Some(o) = large.containers.get(&hi) {
                let r = c.intersection(o);
                if r.cardinality() > 0 {
                    out.insert(hi, r);
                }
            }
        }
        IntSet { containers: out }
    }

    /// Set difference: values in `self` but not in `other`.
    pub fn difference(&self, other: &IntSet) -> IntSet {
        let mut out = BTreeMap::new();
        for (&hi, c) in &self.containers {
            match other.containers.get(&hi) {
                None => {
                    out.insert(hi, c.clone());
                }
                Some(o) => {
                    let r = c.difference(o);
                    if r.cardinality() > 0 {
                        out.insert(hi, r);
                    }
                }
            }
        }
        IntSet { containers: out }
    }

    /// Promote any over-dense array containers (keeps membership identical).
    fn finalize_chunks(&mut self) {
        for c in self.containers.values_mut() {
            if let Container::Array(a) = c {
                if a.len() > crate::container::ARRAY_MAX {
                    let taken = std::mem::take(a);
                    *c = Container::from_values(taken);
                }
            }
        }
    }
}

impl FromIterator<u32> for IntSet {
    fn from_iter<I: IntoIterator<Item = u32>>(iter: I) -> Self {
        let mut s = Self::new();
        for v in iter {
            s.insert(v);
        }
        s.finalize_chunks();
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn reference(vs: &[u32]) -> BTreeSet<u32> {
        vs.iter().copied().collect()
    }

    #[test]
    fn build_contains_iter_sorted() {
        let raw = vec![3u32, 1, 2, 1 << 16, u32::MAX, 2, 0];
        let s = IntSet::from_values(&raw);
        assert_eq!(s.len(), 6);
        assert_eq!(s.to_vec(), vec![0, 1, 2, 3, 1 << 16, u32::MAX]);
        assert!(s.contains(u32::MAX));
        assert!(!s.contains(4));
        assert_eq!(s.chunk_count(), 3);
    }

    #[test]
    fn algebra_vs_btreeset_randomish() {
        let a: Vec<u32> = (0..20_000u32)
            .map(|i| i.wrapping_mul(48271) % 80_000)
            .collect();
        let b: Vec<u32> = (0..20_000u32)
            .map(|i| i.wrapping_mul(69621) % 80_000)
            .collect();
        let ra = reference(&a);
        let rb = reference(&b);
        let sa = IntSet::from_values(&a);
        let sb = IntSet::from_values(&b);

        let ru: BTreeSet<u32> = ra.union(&rb).copied().collect();
        let ri: BTreeSet<u32> = ra.intersection(&rb).copied().collect();
        let rd: BTreeSet<u32> = ra.difference(&rb).copied().collect();

        assert_eq!(
            sa.union(&sb).to_vec().into_iter().collect::<BTreeSet<_>>(),
            ru
        );
        assert_eq!(
            sa.intersection(&sb)
                .to_vec()
                .into_iter()
                .collect::<BTreeSet<_>>(),
            ri
        );
        assert_eq!(
            sa.difference(&sb)
                .to_vec()
                .into_iter()
                .collect::<BTreeSet<_>>(),
            rd
        );
    }

    #[test]
    fn chunk_boundaries_preserved() {
        let vals = vec![0x0000_ffff, 0x0001_0000, 0xffff_0000, 0xffff_ffff];
        let s = IntSet::from_values(&vals);
        // High keys 0x0000, 0x0001, 0xffff -> three distinct chunks.
        assert_eq!(s.chunk_count(), 3);
        assert_eq!(
            s.to_vec(),
            vec![0x0000_ffff, 0x0001_0000, 0xffff_0000, u32::MAX]
        );
    }
}
