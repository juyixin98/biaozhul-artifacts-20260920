//! The two container kinds and their set algebra.
//!
//! A container covers exactly the low 16 bits of the key space, i.e. 65 536
//! slots. It is stored either as a sorted, de-duplicated `u16` array
//! (sparse) or as 1 024 `u64` words (dense). Containers convert between the
//! two representations while preserving membership exactly.

/// Number of 16-bit slots in a container (`2^16`).
pub const SLOTS: usize = 1 << 16;
/// Number of 64-bit words in a bitmap container.
pub const BITMAP_WORDS: usize = SLOTS / 64;
/// A container with more than this many values must be a bitmap; at or below
/// it may stay an array. Crossover point from the Roaring analysis: 4 096
/// is where the 8 KiB bitmap becomes smaller than two bytes per value.
pub const ARRAY_MAX: usize = 4096;

/// One 16-bit-key universe, in either representation.
#[derive(Clone, PartialEq, Eq)]
pub enum Container {
    /// Sorted, unique values. Length is at most [`ARRAY_MAX`] after
    /// [`Container::optimize`].
    Array(Vec<u16>),
    /// Fixed 1 024-word bitset; bit `w*64 + b` is word `w`, mask `1 << b`.
    Bitmap(Box<[u64; BITMAP_WORDS]>),
}

impl Default for Container {
    fn default() -> Self {
        Container::Array(Vec::new())
    }
}

impl std::fmt::Debug for Container {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Container::Array(a) => write!(f, "Array(len={})", a.len()),
            Container::Bitmap(_) => write!(f, "Bitmap(pop={})", self.cardinality()),
        }
    }
}

#[inline]
fn word_index(x: u16) -> usize {
    (x as usize) >> 6
}
#[inline]
fn bit_mask(x: u16) -> u64 {
    1u64 << ((x as usize) & 63)
}

impl Container {
    /// Empty container in the (canonical) array representation.
    pub fn empty() -> Self {
        Container::Array(Vec::new())
    }

    /// Build from arbitrary values: sorts and de-duplicates, then picks the
    /// cheaper representation.
    pub fn from_values(mut values: Vec<u16>) -> Self {
        values.sort_unstable();
        values.dedup();
        if values.len() > ARRAY_MAX {
            let mut bm = [0u64; BITMAP_WORDS];
            for &v in &values {
                bm[word_index(v)] |= bit_mask(v);
            }
            Container::Bitmap(Box::new(bm))
        } else {
            Container::Array(values)
        }
    }

    /// Build from data the caller guarantees sorted and unique.
    ///
    /// # Safety contract
    /// The caller (the decoder) must have validated ordering and uniqueness
    /// against the byte stream; this function still enforces representation
    /// choice but does not re-sort.
    pub fn from_sorted_trusted(values: Vec<u16>) -> Self {
        if values.len() > ARRAY_MAX {
            Container::from_values(values)
        } else {
            Container::Array(values)
        }
    }

    /// Number of distinct values held.
    pub fn cardinality(&self) -> u64 {
        match self {
            Container::Array(a) => a.len() as u64,
            Container::Bitmap(b) => b.iter().map(|w| w.count_ones() as u64).sum(),
        }
    }

    /// Membership test.
    pub fn contains(&self, x: u16) -> bool {
        match self {
            Container::Array(a) => a.binary_search(&x).is_ok(),
            Container::Bitmap(b) => b[word_index(x)] & bit_mask(x) != 0,
        }
    }

    /// Insert one value (builder-style growth path).
    pub fn insert(&mut self, x: u16) {
        match self {
            Container::Array(a) => match a.binary_search(&x) {
                Ok(_) => {}
                Err(pos) => {
                    a.insert(pos, x);
                    if a.len() > ARRAY_MAX {
                        let mut bm = Box::new([0u64; BITMAP_WORDS]);
                        for &v in a.iter() {
                            bm[word_index(v)] |= bit_mask(v);
                        }
                        *self = Container::Bitmap(bm);
                    }
                }
            },
            Container::Bitmap(b) => {
                b[word_index(x)] |= bit_mask(x);
            }
        }
    }

    /// Iterate all values in ascending order.
    pub fn iter(&self) -> Values<'_> {
        match self {
            Container::Array(a) => Values::Array(a.iter()),
            Container::Bitmap(b) => Values::Bitmap {
                words: b.iter().enumerate(),
                cur: 0,
                wi: 0,
            },
        }
    }

    /// Collect all values, ascending.
    pub fn to_vec(&self) -> Vec<u16> {
        self.iter().collect()
    }

    /// Convert to the representation that uses the fewer bytes while keeping
    /// membership identical. Array at the crossover stays an array.
    pub fn optimize(self) -> Self {
        match self {
            Container::Array(a) => {
                if a.len() > ARRAY_MAX {
                    Container::from_values(a)
                } else {
                    Container::Array(a)
                }
            }
            Container::Bitmap(b) => {
                let pop: usize = b.iter().map(|w| w.count_ones() as usize).sum();
                if pop <= ARRAY_MAX {
                    let mut out = Vec::with_capacity(pop);
                    for (wi, word) in b.iter().enumerate() {
                        let mut w = *word;
                        while w != 0 {
                            let lsb = w.trailing_zeros();
                            out.push((wi * 64 + lsb as usize) as u16);
                            w &= w - 1;
                        }
                    }
                    Container::Array(out)
                } else {
                    Container::Bitmap(b)
                }
            }
        }
    }

    /// Set union.
    pub fn union(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => Container::from_values(merge_union(a, b)),
            (Container::Bitmap(x), Container::Bitmap(y)) => {
                let mut out = Box::new([0u64; BITMAP_WORDS]);
                for i in 0..BITMAP_WORDS {
                    out[i] = x[i] | y[i];
                }
                Container::Bitmap(out).optimize()
            }
            (bm @ Container::Bitmap(_), Container::Array(a))
            | (Container::Array(a), bm @ Container::Bitmap(_)) => {
                let mut out = bm.clone_bitmap();
                for &v in a {
                    out[word_index(v)] |= bit_mask(v);
                }
                Container::Bitmap(out).optimize()
            }
        }
    }

    /// Set intersection.
    pub fn intersection(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => Container::Array(merge_intersect(a, b)),
            (Container::Bitmap(x), Container::Bitmap(y)) => {
                let mut out = Box::new([0u64; BITMAP_WORDS]);
                for i in 0..BITMAP_WORDS {
                    out[i] = x[i] & y[i];
                }
                Container::Bitmap(out).optimize()
            }
            (Container::Bitmap(b), Container::Array(a))
            | (Container::Array(a), Container::Bitmap(b)) => Container::Array(
                a.iter()
                    .copied()
                    .filter(|&v| b[word_index(v)] & bit_mask(v) != 0)
                    .collect(),
            ),
        }
    }

    /// Set difference: values in `self` but not in `other`.
    pub fn difference(&self, other: &Container) -> Container {
        match (self, other) {
            (Container::Array(a), Container::Array(b)) => Container::Array(merge_difference(a, b)),
            (Container::Array(a), Container::Bitmap(b)) => Container::Array(
                a.iter()
                    .copied()
                    .filter(|&v| b[word_index(v)] & bit_mask(v) == 0)
                    .collect(),
            ),
            (Container::Bitmap(x), Container::Array(a)) => {
                let mut out = Box::new([0u64; BITMAP_WORDS]);
                out.copy_from_slice(x.as_ref());
                for &v in a {
                    out[word_index(v)] &= !bit_mask(v);
                }
                Container::Bitmap(out).optimize()
            }
            (Container::Bitmap(x), Container::Bitmap(y)) => {
                let mut out = Box::new([0u64; BITMAP_WORDS]);
                for i in 0..BITMAP_WORDS {
                    out[i] = x[i] & !y[i];
                }
                Container::Bitmap(out).optimize()
            }
        }
    }

    fn clone_bitmap(&self) -> Box<[u64; BITMAP_WORDS]> {
        match self {
            Container::Bitmap(b) => b.clone(),
            Container::Array(_) => panic!("clone_bitmap called on array"),
        }
    }
}

/// Ascending iterator over a container's values.
pub enum Values<'a> {
    #[doc(hidden)]
    Array(std::slice::Iter<'a, u16>),
    #[doc(hidden)]
    Bitmap {
        words: std::iter::Enumerate<std::slice::Iter<'a, u64>>,
        /// Index of the word whose unconsumed bits live in `cur`.
        wi: usize,
        /// Remaining (unconsumed) bits of the current word.
        cur: u64,
    },
}

impl<'a> Iterator for Values<'a> {
    type Item = u16;

    fn next(&mut self) -> Option<u16> {
        match self {
            Values::Array(it) => it.next().copied(),
            Values::Bitmap { words, wi, cur } => loop {
                if *cur != 0 {
                    let lsb = cur.trailing_zeros();
                    *cur &= *cur - 1;
                    return Some((*wi * 64 + lsb as usize) as u16);
                }
                let (idx, word) = words.next()?;
                *wi = idx;
                *cur = *word;
            },
        }
    }
}

// --- sorted-merge primitives ------------------------------------------------

fn merge_union(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(b[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

fn merge_intersect(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out
}

fn merge_difference(a: &[u16], b: &[u16]) -> Vec<u16> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crossover_converts_on_build() {
        let sparse = Container::from_values(vec![1, 2, 3]);
        assert!(matches!(sparse, Container::Array(_)));
        let dense: Vec<u16> = (0..=ARRAY_MAX as u16).collect();
        assert!(matches!(
            Container::from_values(dense),
            Container::Bitmap(_)
        ));
    }

    #[test]
    fn insert_converts_and_keeps_membership() {
        let mut c = Container::empty();
        for v in (0..10_000u32).step_by(2) {
            c.insert(v as u16);
        }
        assert!(matches!(c, Container::Bitmap(_)));
        for v in 0..10_000u32 {
            assert_eq!(c.contains(v as u16), v % 2 == 0);
        }
    }

    #[test]
    fn bitmap_iter_ascending() {
        let vals: Vec<u16> = vec![0, 1, 63, 64, 65, 65535, 100, 100];
        let c = Container::from_values(vals);
        let got: Vec<u16> = c.iter().collect();
        assert_eq!(got, vec![0, 1, 63, 64, 65, 100, 65535]);
    }

    #[test]
    fn optimize_round_trips() {
        let dense: Vec<u16> = (0..5000u16).collect();
        let c = Container::from_values(dense.clone());
        assert!(matches!(c, Container::Bitmap(_)));
        let back = c.optimize();
        // still dense -> stays bitmap, same members
        let rebuilt = Container::from_values(back.to_vec());
        assert_eq!(rebuilt.to_vec(), dense);

        let almost: Vec<u16> = (0..=ARRAY_MAX as u16).collect(); // 4097 -> bitmap
        let c = Container::from_values(almost);
        assert!(matches!(c, Container::Bitmap(_)));
    }

    #[test]
    fn algebra_matches_naive() {
        let a: Vec<u16> = vec![1, 3, 5, 7, 100, 65535];
        let b: Vec<u16> = vec![3, 7, 8, 100, 101];
        for ca in [Container::from_values(a.clone()), dense_version(&a)] {
            for cb in [Container::from_values(b.clone()), dense_version(&b)] {
                let u: std::collections::BTreeSet<u16> = a.iter().chain(&b).copied().collect();
                let i: std::collections::BTreeSet<u16> =
                    a.iter().filter(|x| b.contains(x)).copied().collect();
                let d: std::collections::BTreeSet<u16> =
                    a.iter().filter(|x| !b.contains(x)).copied().collect();
                assert_eq!(
                    ca.union(&cb)
                        .to_vec()
                        .into_iter()
                        .collect::<std::collections::BTreeSet<_>>(),
                    u
                );
                assert_eq!(
                    ca.intersection(&cb)
                        .to_vec()
                        .into_iter()
                        .collect::<std::collections::BTreeSet<_>>(),
                    i
                );
                assert_eq!(
                    ca.difference(&cb)
                        .to_vec()
                        .into_iter()
                        .collect::<std::collections::BTreeSet<_>>(),
                    d
                );
            }
        }
    }

    fn dense_version(v: &[u16]) -> Container {
        // Force bitmap even for small sets by building then skipping optimize.
        let mut bm = Box::new([0u64; BITMAP_WORDS]);
        for &x in v {
            bm[word_index(x)] |= bit_mask(x);
        }
        Container::Bitmap(bm)
    }
}
