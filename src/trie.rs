//! Ordered tries and the TrieIterator used by Leapfrog Triejoin.
//!
//! Each relation is physically stored once, with its variables arranged in the
//! query's *global* order, as a *trie*: at level `i` the children are the
//! sorted, de-duplicated codes of the `i`-th variable, nested under a prefix.
//! Duplicate input rows are not stored twice; a leaf carries an explicit
//! **multiplicity** (`u64`), so the join is a multiset operator.
//!
//! [`TrieCursor`] implements the TrieIterator protocol
//! (`open`/`up`/`next`/`seek`/`key`). `seek` is a binary search — a
//! logarithmic *leap* rather than a linear scan, the property the algorithm is
//! named for. Each cursor counts its own physical accesses.

use crate::error::{ErrorCode, JoinError, Result};

/// Arena index of the root node.
pub const ROOT: u32 = 0;

#[derive(Debug, Clone)]
struct Node {
    /// Sorted, unique child keys at this level.
    keys: Vec<u32>,
    /// Internal level: arena index of the child node for each key.
    links: Vec<u32>,
    /// Leaf level: multiplicity of the full tuple ending at each key.
    counts: Vec<u64>,
    leaf: bool,
}

impl Node {
    /// First position whose key is `>= target` (binary-search leap).
    fn lower_bound(&self, pos: usize, target: u32) -> usize {
        pos + self.keys[pos..].partition_point(|k| *k < target)
    }
}

/// An ordered trie for one relation under the query's global variable order.
#[derive(Debug, Clone)]
pub struct RelationTrie {
    pub relation: String,
    /// Variable names in trie (level) order.
    pub variables: Vec<String>,
    nodes: Vec<Node>,
    /// Number of raw input rows (including duplicates and null-rejected rows).
    pub input_rows: usize,
    /// Number of rows retained after null filtering.
    pub retained_rows: usize,
}

impl RelationTrie {
    pub fn arity(&self) -> usize {
        self.variables.len()
    }

    pub fn is_empty(&self) -> bool {
        self.nodes[ROOT as usize].keys.is_empty()
    }

    /// Build from already domain-encoded, already null-filtered tuples.
    ///
    /// `tuples` align positionally with `variables`. Duplicates are collapsed
    /// into a leaf multiplicity. The caller supplies raw/retained row counts
    /// for diagnostics.
    pub fn from_encoded(
        relation: impl Into<String>,
        variables: Vec<String>,
        mut tuples: Vec<Vec<u32>>,
        input_rows: usize,
        retained_rows: usize,
    ) -> Result<Self> {
        let arity = variables.len();
        if arity == 0 {
            return Err(JoinError::new(
                ErrorCode::SchemaMismatch,
                "cannot build trie for zero-arity relation",
            ));
        }
        for t in &tuples {
            if t.len() != arity {
                return Err(JoinError::new(
                    ErrorCode::SchemaMismatch,
                    format!(
                        "relation `{}` tuple arity {} != {}",
                        relation.into(),
                        t.len(),
                        arity
                    ),
                ));
            }
        }
        tuples.sort_unstable();

        let mut nodes: Vec<Node> = Vec::new();
        build_level(&mut nodes, 0, arity, &tuples);
        debug_assert_eq!(nodes.len() >= 1, true);

        Ok(RelationTrie {
            relation: relation.into(),
            variables,
            nodes,
            input_rows,
            retained_rows,
        })
    }

    /// Distinct keys at the root (first variable); statistics/diagnostics use.
    pub fn root_keys(&self) -> &[u32] {
        &self.nodes[ROOT as usize].keys
    }

    /// Number of distinct nodes (trie size), for the "no blow-up" diagnostics.
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn cursor(&self) -> TrieCursor<'_> {
        TrieCursor::new(self)
    }
}

/// Recursively construct nodes. The parent node slot is pushed *before* its
/// children so the very first allocation is [`ROOT`]; child links are filled in
/// once the recursive calls return their indices.
fn build_level(nodes: &mut Vec<Node>, level: usize, arity: usize, tuples: &[Vec<u32>]) -> u32 {
    // Group this level by key, preserving sorted order.
    let mut groups: Vec<(u32, &[Vec<u32>])> = Vec::new();
    let mut i = 0;
    while i < tuples.len() {
        let key = tuples[i][level];
        let j = i + tuples[i..].partition_point(|t| t[level] == key);
        groups.push((key, &tuples[i..j]));
        i = j;
    }

    let idx = nodes.len() as u32;
    if level == arity - 1 {
        let keys: Vec<u32> = groups.iter().map(|(k, _)| *k).collect();
        // After null filtering each retained tuple is one row; equal encoded
        // tuples are duplicates and all survive, hence multiplicity = group len.
        let counts: Vec<u64> = groups.iter().map(|(_, g)| g.len() as u64).collect();
        let links = vec![u32::MAX; keys.len()];
        nodes.push(Node {
            keys,
            links,
            counts,
            leaf: true,
        });
        return idx;
    }

    // Reserve the parent slot, then build children (which append after it).
    nodes.push(Node {
        keys: Vec::new(),
        links: Vec::new(),
        counts: Vec::new(),
        leaf: false,
    });
    let mut keys = Vec::with_capacity(groups.len());
    let mut links = Vec::with_capacity(groups.len());
    for (key, group) in groups {
        keys.push(key);
        links.push(build_level(nodes, level + 1, arity, group));
    }
    nodes[idx as usize] = Node {
        keys,
        links,
        counts: Vec::new(),
        leaf: false,
    };
    idx
}

/// One stack frame: positioned inside an arena node.
#[derive(Debug, Clone, Copy)]
struct Frame {
    node: u32,
    pos: usize,
}

/// TrieIterator over a [`RelationTrie`].
///
/// Trie depth is independent of the *global* join level: a relation missing
/// some join variables simply is not `open`ed at those levels (handled by the
/// operator via a level map). The cursor owns its access counters, retrievable
/// through [`TrieCursor::into_stats`].
pub struct TrieCursor<'a> {
    trie: &'a RelationTrie,
    stack: Vec<Frame>,
    stats: CursorStats,
}

impl<'a> TrieCursor<'a> {
    fn new(trie: &'a RelationTrie) -> Self {
        TrieCursor {
            trie,
            stack: vec![Frame {
                node: ROOT,
                pos: 0,
            }],
            stats: CursorStats::default(),
        }
    }

    /// Consume the cursor, returning its physical-access counters.
    pub fn into_stats(self) -> CursorStats {
        self.stats
    }

    /// Borrowed view of accumulated counters.
    pub fn stats(&self) -> &CursorStats {
        &self.stats
    }

    #[inline]
    fn frame(&self) -> &Frame {
        self.stack.last().expect("cursor always has a frame")
    }

    #[inline]
    fn frame_mut(&mut self) -> &mut Frame {
        self.stack.last_mut().expect("cursor always has a frame")
    }

    #[inline]
    fn node(&self) -> &Node {
        &self.trie.nodes[self.frame().node as usize]
    }

    /// Current trie depth (0 = first variable).
    pub fn at_level(&self) -> usize {
        self.stack.len() - 1
    }

    pub fn at_end(&self) -> bool {
        let f = self.frame();
        f.pos >= self.trie.nodes[f.node as usize].keys.len()
    }

    /// Current key; `u32::MAX` when past the end.
    pub fn key(&self) -> u32 {
        let f = self.frame();
        self.trie.nodes[f.node as usize]
            .keys
            .get(f.pos)
            .copied()
            .unwrap_or(u32::MAX)
    }

    /// Leaf multiplicity at the current full prefix (leaf level only).
    pub fn multiplicity(&self) -> u64 {
        let n = self.node();
        debug_assert!(n.leaf, "multiplicity() only valid at leaf level");
        n.counts.get(self.frame().pos).copied().unwrap_or(0)
    }

    /// Descend one level, positioned on the first child key.
    pub fn open(&mut self) {
        self.stats.opens += 1;
        let child = self.node().links[self.frame().pos];
        self.stack.push(Frame {
            node: child,
            pos: 0,
        });
    }

    /// Ascend one level.
    pub fn up(&mut self) {
        self.stats.ups += 1;
        debug_assert!(self.stack.len() > 1, "up() below root");
        self.stack.pop();
    }

    /// Linear advance by one.
    pub fn next(&mut self) {
        self.stats.nexts += 1;
        self.frame_mut().pos += 1;
    }

    /// Reposition to the first key `>= target` *without linear scanning*.
    pub fn seek(&mut self, target: u32) {
        self.stats.seeks += 1;
        let node_idx = self.frame().node;
        let pos = self.frame().pos;
        let n = &self.trie.nodes[node_idx as usize];
        let new_pos = if pos < n.keys.len() && n.keys[pos] >= target {
            pos
        } else {
            n.lower_bound(pos, target)
        };
        self.frame_mut().pos = new_pos;
    }
}

/// Physical-access counters for one cursor.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CursorStats {
    pub seeks: u64,
    pub nexts: u64,
    pub opens: u64,
    pub ups: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enc(t: &[Vec<i64>]) -> Vec<Vec<u32>> {
        t.iter().map(|r| r.iter().map(|x| *x as u32).collect()).collect()
    }

    #[test]
    fn sorted_unique_keys_with_multiplicity() {
        let t = RelationTrie::from_encoded(
            "r",
            vec!["a".into(), "b".into()],
            enc(&[
                vec![2, 20],
                vec![1, 10],
                vec![1, 10], // duplicate -> multiplicity 2
                vec![1, 11],
            ]),
            4,
            4,
        )
        .unwrap();
        assert_eq!(t.root_keys(), &[1, 2]);
        let mut c = t.cursor();
        assert_eq!(c.key(), 1); // a=1
        c.open();
        assert_eq!(c.key(), 10); // b=10
        assert_eq!(c.multiplicity(), 2);
        c.next();
        assert_eq!(c.multiplicity(), 1); // b=11
    }

    #[test]
    fn empty_trie_is_safe() {
        let t =
            RelationTrie::from_encoded("r", vec!["a".into()], vec![], 0, 0).unwrap();
        assert!(t.is_empty());
        let c = t.cursor();
        assert!(c.at_end());
    }

    #[test]
    fn seek_leaps_without_scanning() {
        let t = RelationTrie::from_encoded(
            "r",
            vec!["a".into()],
            enc(&[vec![1], vec![2], vec![3], vec![4], vec![5]]),
            5,
            5,
        )
        .unwrap();
        let mut c = t.cursor();
        c.seek(4);
        assert_eq!(c.key(), 4);
        let s = c.stats();
        assert_eq!(s.seeks, 1);
        assert_eq!(s.nexts, 0, "seek must not linear-scan");
    }
}
