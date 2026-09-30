//! Ordered tries over typed tuples with explicit row multiplicity.
//!
//! Relations are stored as the *multiset* of their rows in lexicographic
//! order. Repeated input rows are not de-duplicated: identical rows increase
//! the multiplicity stored at the leaf. The trie is represented as a flat,
//! sorted list of unique tuples plus a parallel multiplicity vector; a
//! depth-stack cursor ([`TrieIterator`]) answers node ranges with binary
//! search, giving logarithmic `seek` without materializing an explicit nested
//! node structure.

use std::cmp::Ordering;

use super::value::Scalar;

/// An ordered trie for a fixed-arity relation.
#[derive(Debug, Clone)]
pub struct Trie {
    arity: usize,
    /// Sorted distinct leaf tuples.
    leaves: Vec<Vec<Scalar>>,
    /// Parallel to `leaves`: multiset multiplicity of each tuple.
    multiplicity: Vec<u64>,
    /// Number of input rows counting duplicates (multiset size).
    total_rows: u64,
}

impl Trie {
    /// Build from input rows. Rows are sorted lexicographically and adjacent
    /// duplicates merged into multiplicities. All rows must share an arity and
    /// contain no NULL (the batch layer enforces the NULL policy before this
    /// point).
    pub fn build(mut rows: Vec<Vec<Scalar>>) -> Self {
        let arity = rows.first().map(|r| r.len()).unwrap_or(0);
        for r in &rows {
            assert_eq!(r.len(), arity, "ragged rows cannot form a trie");
            assert!(
                r.iter().all(|v| !v.is_null()),
                "NULL must not reach a trie"
            );
        }
        let total_rows = rows.len() as u64;
        rows.sort_by(|a, b| lex_cmp(a, b));

        let mut leaves: Vec<Vec<Scalar>> = Vec::with_capacity(rows.len());
        let mut multiplicity: Vec<u64> = Vec::with_capacity(rows.len());
        for row in rows {
            match leaves.last_mut() {
                Some(prev) if prev[..] == row[..] => {
                    *multiplicity.last_mut().unwrap() += 1;
                }
                _ => {
                    multiplicity.push(1);
                    leaves.push(row);
                }
            }
        }
        Self {
            arity,
            leaves,
            multiplicity,
            total_rows,
        }
    }

    /// An empty trie of the given arity (a relation with zero rows).
    pub fn empty(arity: usize) -> Self {
        Self {
            arity,
            leaves: Vec::new(),
            multiplicity: Vec::new(),
            total_rows: 0,
        }
    }

    /// Build from distinct tuples carrying explicit multiplicities, e.g.
    /// planner-produced column permutations of a stored relation. Rows are
    /// re-sorted; defensively, equal tuples are merged by summing their
    /// multiplicities. `arity` is required so that an empty input still
    /// produces a trie shaped like the relation it represents.
    pub fn from_distinct_rows(
        mut rows: Vec<(Vec<Scalar>, u64)>,
        arity: usize,
    ) -> Self {
        for (row, _) in &rows {
            assert_eq!(row.len(), arity, "ragged rows cannot form a trie");
            assert!(
                row.iter().all(|v| !v.is_null()),
                "NULL must not reach a trie"
            );
        }
        rows.sort_by(|a, b| lex_cmp(&a.0, &b.0));

        let mut leaves: Vec<Vec<Scalar>> = Vec::with_capacity(rows.len());
        let mut multiplicity: Vec<u64> = Vec::with_capacity(rows.len());
        let mut total_rows = 0u64;
        for (row, mult) in rows {
            total_rows = total_rows.saturating_add(mult);
            match leaves.last_mut() {
                Some(prev) if prev[..] == row[..] => {
                    *multiplicity.last_mut().unwrap() += mult;
                }
                _ => {
                    multiplicity.push(mult);
                    leaves.push(row);
                }
            }
        }
        Self {
            arity,
            leaves,
            multiplicity,
            total_rows,
        }
    }

    pub fn arity(&self) -> usize {
        self.arity
    }

    pub fn is_empty(&self) -> bool {
        self.leaves.is_empty()
    }

    /// Multiset cardinality (input rows, including duplicates).
    pub fn total_rows(&self) -> u64 {
        self.total_rows
    }

    pub fn distinct_rows(&self) -> usize {
        self.leaves.len()
    }

    /// Multiplicity of a full tuple (0 when absent).
    pub fn multiplicity_of(&self, key: &[Scalar]) -> u64 {
        if key.len() != self.arity {
            return 0;
        }
        self.leaves
            .binary_search_by(|row| lex_cmp(row, key))
            .map(|i| self.multiplicity[i])
            .unwrap_or(0)
    }

    /// Index range (into `leaves`) of tuples sharing `prefix`.
    pub fn prefix_range(&self, prefix: &[Scalar]) -> std::ops::Range<usize> {
        let lower = self
            .leaves
            .partition_point(|row| lex_cmp_prefix(row, prefix) == Ordering::Less);
        let upper = lower
            + self.leaves[lower..]
                .partition_point(|row| lex_cmp_prefix(row, prefix) != Ordering::Greater);
        lower..upper
    }

    pub(crate) fn leaves(&self) -> &[Vec<Scalar>] {
        &self.leaves
    }

    pub(crate) fn multiplicity_at(&self, idx: usize) -> u64 {
        self.multiplicity[idx]
    }

    /// Every distinct leaf tuple paired with its multiset multiplicity.
    pub fn distinct_leaves(&self) -> Vec<(Vec<Scalar>, u64)> {
        self.leaves
            .iter()
            .enumerate()
            .map(|(i, row)| (row.clone(), self.multiplicity[i]))
            .collect()
    }
}

/// Compare two full tuples lexicographically.
pub fn lex_cmp(a: &[Scalar], b: &[Scalar]) -> Ordering {
    for (x, y) in a.iter().zip(b.iter()) {
        match x.cmp(y) {
            Ordering::Equal => continue,
            non_eq => return non_eq,
        }
    }
    a.len().cmp(&b.len())
}

/// Compare a tuple against a prefix: `Equal` means the tuple extends it.
fn lex_cmp_prefix(row: &[Scalar], prefix: &[Scalar]) -> Ordering {
    for (i, p) in prefix.iter().enumerate() {
        match row[i].cmp(p) {
            Ordering::Equal => continue,
            non_eq => return non_eq,
        }
    }
    Ordering::Equal
}

// ---------------------------------------------------------------------------
// TrieIterator
// ---------------------------------------------------------------------------

/// One open stack frame: the leaf range still consistent with the fixed
/// prefix, and the current distinct-key position inside that range.
#[derive(Debug, Clone)]
struct Frame {
    range: std::ops::Range<usize>,
    /// Index into `leaves` of the current node key, or `range.end` when the
    /// iterator is at the end of this level.
    pos: usize,
}

/// A depth-stack trie cursor implementing the Leapfrog TrieIterator
/// interface: `open`, `up`, `at_end`, `key`, `seek`, `next`.
///
/// Every call that performs a search (`open`/`seek`/`next`/`down`)
/// increments [`TrieIterator::accesses`], which is what verification uses to
/// prove the algorithm leaps rather than scanning.
#[derive(Debug)]
pub struct TrieIterator<'a> {
    trie: &'a Trie,
    /// `stack.len() == depth + 1`; the last frame is the current level.
    stack: Vec<Frame>,
    opens: u64,
    seeks: u64,
    nexts: u64,
    downs: u64,
}

impl<'a> TrieIterator<'a> {
    /// Open the iterator at the root of `trie`, positioned at the first
    /// distinct depth-0 key.
    pub fn open(trie: &'a Trie) -> Self {
        let range = 0..trie.leaves.len();
        let pos = range.start;
        Self {
            trie,
            stack: vec![Frame { range, pos }],
            opens: 1,
            seeks: 0,
            nexts: 0,
            downs: 0,
        }
    }

    pub fn trie_arity(&self) -> usize {
        self.trie.arity
    }

    /// Current depth: 0 means no variable fixed yet, d means prefix depth d.
    pub fn depth(&self) -> usize {
        self.stack.len() - 1
    }

    /// Primitive trie accesses performed so far (open + seek + next + down).
    pub fn accesses(&self) -> u64 {
        self.opens + self.seeks + self.nexts + self.downs
    }

    /// Breakdown `(opens, seeks, nexts, downs)` for diagnostics.
    pub fn access_breakdown(&self) -> (u64, u64, u64, u64) {
        (self.opens, self.seeks, self.nexts, self.downs)
    }

    fn frame(&self) -> &Frame {
        self.stack.last().unwrap()
    }

    fn frame_mut(&mut self) -> &mut Frame {
        self.stack.last_mut().unwrap()
    }

    /// True when positioned past the last key at the current level.
    pub fn at_end(&self) -> bool {
        let f = self.frame();
        f.pos >= f.range.end
    }

    /// Key at the current level; `None` when at the end.
    pub fn key(&self) -> Option<&'a Scalar> {
        let f = self.frame();
        if f.pos >= f.range.end {
            None
        } else {
            Some(&self.trie.leaves[f.pos][self.depth()])
        }
    }

    /// Advance to the first distinct key strictly greater than the current
    /// one at this level. No-op at the end.
    pub fn next(&mut self) {
        if self.at_end() {
            return;
        }
        let depth = self.depth();
        let cur = self.frame().pos;
        let end = self.frame().range.end;
        // Jump over every leaf sharing the current distinct key. The range
        // is sorted on this depth component, so the boundary is a binary
        // search: advancing a key is logarithmic even when one key dominates
        // the relation (high-skew case).
        let key = self.trie.leaves[cur][depth].clone();
        let advance = self.trie.leaves[cur + 1..end]
            .partition_point(|row| row[depth] <= key);
        self.frame_mut().pos = cur + 1 + advance;
        self.nexts += 1;
    }

    /// Position at the first distinct key at this level that is `>= target`,
    /// or at the end if none exists. Leaves are sorted, so this is a binary
    /// search inside the current range restricted to the depth component.
    pub fn seek(&mut self, target: &Scalar) {
        let depth = self.depth();
        let range = self.frame().range.clone();
        if range.start >= range.end {
            return;
        }
        // Binary search first index in range with component >= target.
        let mut lo = range.start;
        let mut hi = range.end;
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.trie.leaves[mid][depth] < *target {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        self.frame_mut().pos = lo;
        self.seeks += 1;
    }

    /// Descend one level, opening the range consistent with the key at the
    /// current level. Must not be called at the end or below a leaf.
    pub fn down(&mut self) {
        assert!(!self.at_end(), "cannot descend at end of level");
        assert!(
            self.depth() < self.trie.arity,
            "cannot descend below a leaf"
        );
        let depth = self.depth();
        let Frame {
            range: parent_range,
            pos: parent_pos,
        } = self.frame().clone();
        let key = self.trie.leaves[parent_pos][depth].clone();

        // The parent range is sorted by the depth component, so both edges
        // are found by binary search (leaping, never scanning).
        let lo = parent_range.start
            + self.trie.leaves[parent_range.start..parent_pos].partition_point(|row| {
                row[depth] < key
            });
        let hi = parent_pos
            + self.trie.leaves[parent_pos..parent_range.end]
                .partition_point(|row| row[depth] <= key);
        self.stack.push(Frame { range: lo..hi, pos: lo });
        self.downs += 1;
    }

    /// Reset the current level to its first key. Used when a *new* parent key
    /// is fixed: an atom whose variables skip the parent level is independent
    /// of that key, so its domain must be enumerated from scratch under each
    /// new branch (the Cartesian/cyclic case).
    pub fn reopen(&mut self) {
        let start = self.frame().range.start;
        self.frame_mut().pos = start;
        self.opens += 1;
    }

    /// Move up one level, restoring the parent position. Must not be called
    /// at depth 0.
    pub fn up(&mut self) {
        assert!(self.stack.len() > 1, "cannot move up from the root");
        self.stack.pop();
    }

    /// Leaf index at a fixed prefix depth (position into [`Trie::leaves`]).
    pub fn frame_pos(&self, depth: usize) -> usize {
        self.stack[depth].pos
    }

    /// Multiplicity of the tuple the cursor is parked on at full depth.
    pub fn leaf_multiplicity(&self) -> u64 {
        assert_eq!(self.depth(), self.trie.arity, "not positioned at a leaf");
        let pos = self.frame().pos;
        self.trie.multiplicity[pos]
    }

    /// In-crate frame snapshot used by the resumable cursor.
    pub(crate) fn snapshot_frames(&self) -> Vec<(usize, usize, usize)> {
        self.stack
            .iter()
            .map(|f| (f.range.start, f.range.end, f.pos))
            .collect()
    }

    pub(crate) fn restore_frames(&mut self, frames: &[(usize, usize, usize)]) {
        assert!(!frames.is_empty(), "cursor must keep the root frame");
        self.stack = frames
            .iter()
            .map(|&(start, end, pos)| Frame {
                range: start..end,
                pos,
            })
            .collect();
    }

    /// Validate and restore cursor frames. Every frame must index inside the
    /// trie leaf array and keep `start <= pos <= end`; otherwise the token was
    /// tampered with and is rejected rather than causing an out-of-bounds
    /// access.
    pub(crate) fn restore_frames_checked(
        &mut self,
        frames: &[(usize, usize, usize)],
    ) -> crate::error::Result<()> {
        use crate::error::{Error, ReasonCode};
        if frames.is_empty() {
            return Err(Error::validation(
                ReasonCode::CursorDecode,
                "cursor iterator is missing its root frame",
            ));
        }
        if frames.len() > self.trie.arity + 1 {
            return Err(Error::validation(
                ReasonCode::CursorDecode,
                "cursor iterator carries more frames than the trie has levels",
            ));
        }
        let n = self.trie.leaves.len();
        for &(start, end, pos) in frames {
            if start > n || end > n || start > end || pos < start || pos > end {
                return Err(Error::validation(
                    ReasonCode::CursorDecode,
                    "cursor iterator frame indexes are outside the trie",
                ));
            }
        }
        self.restore_frames(frames);
        Ok(())
    }

    /// Key visible at a parked (not current) frame. Returns `None` when that
    /// frame is positioned at the end of its range.
    pub(crate) fn frame_key(&self, frame_depth: usize) -> Option<&Scalar> {
        let f = self.stack.get(frame_depth)?;
        if f.pos >= f.range.end {
            None
        } else {
            Some(&self.trie.leaves[f.pos][frame_depth])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(i: i64) -> Scalar {
        Scalar::Int(i)
    }

    #[test]
    fn builds_sorted_multiset_with_multiplicity() {
        let trie = Trie::build(vec![
            vec![s(1), s(2)],
            vec![s(1), s(2)],
            vec![s(1), s(3)],
            vec![s(0), s(9)],
        ]);
        assert_eq!(trie.arity(), 2);
        assert_eq!(trie.total_rows(), 4);
        assert_eq!(trie.distinct_rows(), 3);
        assert_eq!(trie.multiplicity_of(&[s(1), s(2)]), 2);
        assert_eq!(trie.multiplicity_of(&[s(1), s(3)]), 1);
        assert_eq!(trie.multiplicity_of(&[s(1), s(4)]), 0);
    }

    #[test]
    fn prefix_ranges() {
        let trie = Trie::build(vec![
            vec![s(1), s(2)],
            vec![s(1), s(3)],
            vec![s(2), s(1)],
            vec![s(3), s(1)],
        ]);
        assert_eq!(trie.prefix_range(&[s(1)]), 0..2);
        assert_eq!(trie.prefix_range(&[s(2)]), 2..3);
        assert_eq!(trie.prefix_range(&[s(4)]), 4..4);
        assert_eq!(trie.prefix_range(&[s(1), s(3)]), 1..2);
        assert_eq!(trie.prefix_range(&[s(1), s(9)]), 2..2);
    }

    #[test]
    fn cursor_walks_distinct_keys_and_descends() {
        let trie = Trie::build(vec![
            vec![s(1), s(2)],
            vec![s(1), s(3)],
            vec![s(2), s(1)],
            vec![s(3), s(1)],
        ]);
        let mut it = TrieIterator::open(&trie);
        assert_eq!(it.depth(), 0);
        let mut roots = Vec::new();
        while let Some(k) = it.key() {
            match k {
                Scalar::Int(i) => roots.push(*i),
                _ => panic!("unexpected key"),
            }
            it.next();
        }
        assert_eq!(roots, vec![1, 2, 3]);
        assert!(it.at_end());

        // Re-open, descend under 1 -> children 2, 3.
        let mut it = TrieIterator::open(&trie);
        it.down();
        assert_eq!(it.depth(), 1);
        let mut children = Vec::new();
        while let Some(k) = it.key() {
            match k {
                Scalar::Int(i) => children.push(*i),
                _ => panic!("unexpected key"),
            }
            it.next();
        }
        assert_eq!(children, vec![2, 3]);
        it.up();
        assert_eq!(it.depth(), 0);
        assert_eq!(it.key(), Some(&s(1)));
    }

    #[test]
    fn cursor_seek_leaps_without_scanning() {
        // 1000 keys; seeking near the end must take few accesses.
        let rows: Vec<Vec<Scalar>> = (0..1000).map(|i| vec![s(i)]).collect();
        let trie = Trie::build(rows);
        let mut it = TrieIterator::open(&trie);
        let before = it.accesses();
        it.seek(&s(900));
        let after = it.accesses();
        assert_eq!(it.key(), Some(&s(900)));
        assert!(after - before <= 2, "seek must be logarithmic, got {}", after - before);
        // Seeking past the end lands at end.
        it.seek(&s(10_000));
        assert!(it.at_end());
    }

    #[test]
    fn empty_trie_cursor_is_at_end() {
        let trie = Trie::empty(2);
        let it = TrieIterator::open(&trie);
        assert!(it.at_end());
        assert!(it.key().is_none());
    }

    #[test]
    fn next_jumps_logarithmically_over_a_skewed_key_group() {
        // One dominant key owns 20_000 child rows, then a second root key.
        let mut rows: Vec<Vec<Scalar>> = (0..20_000).map(|i| vec![s(1), s(i)]).collect();
        rows.push(vec![s(2), s(0)]);
        let trie = Trie::build(rows);
        let mut it = TrieIterator::open(&trie);
        assert_eq!(it.key(), Some(&s(1)));
        it.next();
        // Advancing past the dominant group is one (binary-search) next.
        assert_eq!(it.key(), Some(&s(2)));

        // Same property one level down, inside the dominant group.
        let mut it = TrieIterator::open(&trie);
        it.down();
        assert_eq!(it.key(), Some(&s(0)));
        it.seek(&s(19_990));
        assert_eq!(it.key(), Some(&s(19_990)));
        it.next();
        assert_eq!(it.key(), Some(&s(19_991)));
        it.next();
        assert_eq!(it.key(), Some(&s(19_992)));
    }
}
