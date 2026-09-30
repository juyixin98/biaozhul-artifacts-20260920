//! Leapfrog Triejoin operator.
//!
//! At each global level the *active* relations (those containing the level's
//! variable) expose trie iterators. The level finds the next common key by
//! leaping whichever iterator lags behind up to the running maximum
//! (logarithmic [`TrieCursor::seek`]); after a match is consumed it advances a
//! single round-robin *driver* with one [`TrieCursor::next`] and leaps the rest.
//! Relations missing a variable stay parked at their prefix, so no pairwise
//! intermediate product is ever built.
//!
//! Free-variable levels have exactly one active relation and are walked with
//! `next`. Leaf multiplicity is the product over relations, expanding the
//! multiset correctly.
//!
//! The run is interruptible at a row budget and **resumable**: the opaque
//! [`Checkpoint`] records the bound code at every level and the offset inside
//! the current multiplicity run. Resuming reopens the immutable tries and seeks
//! straight back to the checkpoint prefix (no re-enumeration), then continues.

use std::collections::BTreeMap;

use crate::error::{ErrorCode, JoinError, Result};
use crate::plan::JoinPlan;
use crate::trie::{CursorStats, RelationTrie, TrieCursor};
use crate::types::{Datum, Domain, NullPolicy};

/// Aggregated physical-work counters for one join execution.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct JoinStats {
    pub seeks: u64,
    pub nexts: u64,
    pub opens: u64,
    pub ups: u64,
    /// Distinct full assignments visited.
    pub assignments: u64,
    /// Multiset rows emitted (assignments expanded by multiplicity).
    pub rows_emitted: u64,
}

/// One built relation trie plus the global position of each of its levels.
pub struct TrieSet {
    pub plan: JoinPlan,
    pub tries: Vec<RelationTrie>,
    /// One order-preserving domain per global variable.
    pub domains: Vec<Domain>,
    /// trie-level -> global-level, per relation.
    pub level_map: Vec<Vec<usize>>,
    /// Rows dropped by the null policy, per relation (diagnostics).
    pub null_rejected: Vec<usize>,
}

impl TrieSet {
    /// Build all tries for a validated plan.
    ///
    /// `catalog_rows[ri]` are the relation's rows in **catalog column order**;
    /// they are projected into global order here. Domains are populated across
    /// every relation *before* freezing so codes are shared and stable.
    pub fn build(
        plan: JoinPlan,
        catalog_rows: &[Vec<Vec<Datum>>],
    ) -> Result<TrieSet> {
        let n_rel = plan.relations.len();
        if catalog_rows.len() != n_rel {
            return Err(JoinError::internal("TrieSet::build relation count mismatch"));
        }
        let n_vars = plan.ordered_vars.len();
        let mut domains = vec![Domain::new(); n_vars];

        // Global level of each relation's trie variable.
        let level_map: Vec<Vec<usize>> = (0..n_rel)
            .map(|ri| {
                plan.relations[ri]
                    .trie_vars
                    .iter()
                    .map(|v| {
                        plan.ordered_vars
                            .iter()
                            .position(|x| x == v)
                            .expect("trie var present in global order")
                    })
                    .collect()
            })
            .collect();

        let reject_join = plan.null_policy == NullPolicy::SqlMatchNullsReject;

        // Pass 1: project rows, apply null policy, stage domains.
        // `projected[ri]` keeps retained rows in trie order.
        let mut projected: Vec<Vec<Vec<Datum>>> = Vec::with_capacity(n_rel);
        let mut null_rejected = vec![0usize; n_rel];
        for (ri, pr) in plan.relations.iter().enumerate() {
            let mut rows = Vec::new();
            for row in &catalog_rows[ri] {
                if row.len() != pr.source_columns.len() {
                    return Err(JoinError::new(
                        ErrorCode::SchemaMismatch,
                        format!(
                            "relation `{}` row arity {} != {}",
                            pr.name,
                            row.len(),
                            pr.source_columns.len()
                        ),
                    ));
                }
                let proj: Vec<Datum> = pr
                    .source_columns
                    .iter()
                    .map(|&c| row[c].clone())
                    .collect();
                let null_on_key = reject_join
                    && proj.iter().enumerate().any(|(tl, d)| {
                        let gl = level_map[ri][tl];
                        gl < plan.join_var_count && d.is_null()
                    });
                if null_on_key {
                    null_rejected[ri] += 1;
                    continue;
                }
                for (tl, d) in proj.iter().enumerate() {
                    domains[level_map[ri][tl]].add(d.clone());
                }
                rows.push(proj);
            }
            projected.push(rows);
        }
        for d in &mut domains {
            d.freeze();
        }

        // Pass 2: encode retained rows and build tries.
        let mut tries = Vec::with_capacity(n_rel);
        for (ri, pr) in plan.relations.iter().enumerate() {
            let encoded: Vec<Vec<u32>> = projected[ri]
                .iter()
                .map(|row| {
                    row.iter()
                        .enumerate()
                        .map(|(tl, d)| {
                            domains[level_map[ri][tl]]
                                .code_of(d)
                                .expect("datum frozen into domain")
                        })
                        .collect()
                })
                .collect();
            let retained = encoded.len();
            let input = catalog_rows[ri].len();
            let trie = RelationTrie::from_encoded(
                pr.name.clone(),
                pr.trie_vars.clone(),
                encoded,
                input,
                retained,
            )?;
            tries.push(trie);
        }

        Ok(TrieSet {
            plan,
            tries,
            domains,
            level_map,
            null_rejected,
        })
    }
}

/// One emitted output row in global variable order (codes), with multiplicity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodeRow {
    pub codes: Vec<u32>,
    /// Always 1 per emitted multiset row; kept for clarity/future aggregation.
    pub multiplicity: u64,
}

/// Opaque, versioned resumption checkpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkpoint {
    pub plan_fingerprint: u64,
    /// Bound code at every global level of the tuple where the cut happened.
    pub bounds: Vec<u32>,
    /// Multiset rows already emitted inside that tuple's multiplicity run.
    pub within_leaf: u64,
}

impl Checkpoint {
    pub fn encode(&self) -> String {
        let mut s = String::new();
        s.push_str(&self.plan_fingerprint.to_string());
        s.push('.');
        for b in &self.bounds {
            s.push_str(&b.to_string());
            s.push(',');
        }
        s.push('.');
        s.push_str(&self.within_leaf.to_string());
        use base64::Engine as _;
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(s.as_bytes())
    }

    pub fn decode(token: &str) -> Result<Self> {
        use base64::Engine as _;
        let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(token.as_bytes())
            .map_err(|_| JoinError::new(ErrorCode::InvalidCursor, "cursor is not valid base64"))?;
        let s = String::from_utf8(raw)
            .map_err(|_| JoinError::new(ErrorCode::InvalidCursor, "cursor is not utf-8"))?;
        let mut parts = s.splitn(3, '.');
        let fp: u64 = parts
            .next()
            .and_then(|p| p.parse().ok())
            .ok_or_else(|| JoinError::new(ErrorCode::InvalidCursor, "bad fingerprint"))?;
        let bounds: Vec<u32> = parts
            .next()
            .map(|p| {
                p.trim_end_matches(',')
                    .split(',')
                    .filter(|x| !x.is_empty())
                    .map(|x| x.parse::<u32>())
                    .collect::<std::result::Result<Vec<_>, _>>()
            })
            .transpose()
            .map_err(|_| JoinError::new(ErrorCode::InvalidCursor, "bad bounds"))?
            .unwrap_or_default();
        let within: u64 = parts
            .next()
            .and_then(|p| p.parse().ok())
            .ok_or_else(|| JoinError::new(ErrorCode::InvalidCursor, "bad within-leaf offset"))?;
        Ok(Checkpoint {
            plan_fingerprint: fp,
            bounds,
            within_leaf: within,
        })
    }
}

/// Stable fingerprint binding a resume cursor to the plan *and* the data.
///
/// Covers the variable/relation order, null policy and a content summary of
/// every trie (row counts, node count, root keys). A cursor minted against a
/// different plan — or against tries rebuilt after the catalog changed — fails
/// validation with [`ErrorCode::InvalidCursor`] instead of silently resuming
/// at a meaningless position. Implemented as FNV-1a over a canonical encoding
/// so the value is deterministic across processes and toolchain releases.
pub fn fingerprint(ts: &TrieSet) -> u64 {
    let mut h = Fnv1a::new();
    let plan = &ts.plan;
    for v in &plan.ordered_vars {
        h.bytes(v.as_bytes());
    }
    h.u64(plan.join_var_count as u64);
    h.bytes(plan.null_policy.as_str().as_bytes());
    for r in &plan.relations {
        h.bytes(r.name.as_bytes());
        for v in &r.trie_vars {
            h.bytes(v.as_bytes());
        }
    }
    for t in &ts.tries {
        h.u64(t.input_rows as u64);
        h.u64(t.retained_rows as u64);
        h.u64(t.node_count() as u64);
        for k in t.root_keys() {
            h.u64(*k as u64);
        }
    }
    h.finish()
}

/// Minimal FNV-1a/64 hasher (deterministic, no dependency on std internals).
struct Fnv1a(u64);

impl Fnv1a {
    fn new() -> Self {
        Fnv1a(0xcbf2_9ce4_8422_2325)
    }

    fn bytes(&mut self, bs: &[u8]) {
        // Length-prefix so concatenations like ("ab","c") vs ("a","bc")
        // cannot collide through framing.
        self.u64(bs.len() as u64);
        for b in bs {
            self.0 ^= u64::from(*b);
            self.0 = self.0.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }

    fn u64(&mut self, v: u64) {
        self.bytes(&v.to_le_bytes());
    }

    fn finish(self) -> u64 {
        self.0
    }
}

/// Result of driving the join.
pub struct JoinRun {
    /// Emitted rows as decoded datums, **projected** per the plan.
    pub rows: Vec<Vec<Datum>>,
    pub truncated: bool,
    pub checkpoint: Option<Checkpoint>,
    pub stats: JoinStats,
    /// Physical accesses per relation (harvested from the live cursors).
    pub per_cursor: BTreeMap<String, CursorStats>,
}

/// Internal mutable engine.
struct Engine<'a> {
    ts: &'a TrieSet,
    cursors: Vec<TrieCursor<'a>>,
    bounds: Vec<u32>,
    stats: JoinStats,
    rows: Vec<Vec<u32>>,
    remaining: u64,
    truncated: bool,
    stop: bool,
    /// Checkpoint captured at the cut (when truncated).
    pending_checkpoint: Option<Checkpoint>,
    /// Resume prefix: `Some(code)` while descending the checkpoint path.
    seek_target: Vec<Option<u32>>,
    /// Multiset rows already emitted inside the resumed leaf.
    skip_in_leaf: u64,
    /// Round-robin driver per global level.
    driver: Vec<usize>,
}

impl<'a> Engine<'a> {
    fn new(ts: &'a TrieSet, limit: Option<u64>, resume: Option<&Checkpoint>) -> Self {
        let nlevels = ts.plan.ordered_vars.len();
        let cursors = ts.tries.iter().map(|t| t.cursor()).collect();
        let mut seek_target = vec![None; nlevels];
        let mut skip_in_leaf = 0;
        if let Some(cp) = resume {
            for (g, code) in cp.bounds.iter().enumerate() {
                if g < nlevels {
                    seek_target[g] = Some(*code);
                }
            }
            skip_in_leaf = cp.within_leaf;
        }
        Engine {
            ts,
            cursors,
            bounds: vec![u32::MAX; nlevels],
            stats: JoinStats::default(),
            rows: Vec::new(),
            remaining: limit.unwrap_or(u64::MAX),
            truncated: false,
            stop: false,
            pending_checkpoint: None,
            seek_target,
            skip_in_leaf,
            driver: vec![0; nlevels],
        }
    }

    #[inline]
    fn active(&self, level: usize) -> &[usize] {
        &self.ts.plan.level_relations[level]
    }

    /// Align all active cursors at `level` on their next common key.
    /// `advance` is the iterator that was just `next()`-ed (or `None` for the
    /// initial alignment). Returns the common key, or `None` at exhaustion.
    fn align(&mut self, level: usize) -> Option<u32> {
        let rels = self.active(level).to_vec();
        if rels.len() == 1 {
            let c = &mut self.cursors[rels[0]];
            if c.at_end() {
                return None;
            }
            return Some(c.key());
        }
        // Leap the minimum iterator toward the running maximum until all meet.
        loop {
            let mut max_key = 0u32;
            for &ri in &rels {
                let c = &self.cursors[ri];
                if c.at_end() {
                    return None;
                }
                max_key = max_key.max(c.key());
            }
            // Pick any lagging iterator with the smallest key and leap it.
            let lagger = rels
                .iter()
                .copied()
                .filter(|&ri| self.cursors[ri].key() < max_key)
                .min_by_key(|&ri| self.cursors[ri].key());
            match lagger {
                None => return Some(max_key),
                Some(ri) => {
                    self.cursors[ri].seek(max_key);
                    self.stats.seeks += 1;
                    if self.cursors[ri].at_end() {
                        return None;
                    }
                }
            }
        }
    }

    /// Open active cursors to `level`; returns the relations that were opened
    /// (so the caller can `up` the same number on exit).
    fn enter_level(&mut self, level: usize) -> Vec<usize> {
        // `ts` is a shared `&'a TrieSet`; iterating through it keeps the
        // borrow off `self` so the cursors/stats can be mutated in the loop.
        let ts: &'a TrieSet = self.ts;
        let mut opened = Vec::new();
        for &ri in &ts.plan.level_relations[level] {
            // `depth` is the count of trie levels already open. Trie levels
            // follow the global order, so the next unopened trie variable is
            // `level_map[ri][depth]`. Open precisely when that variable is the
            // current global level: comparing the cursor's current depth
            // against `level` is wrong, because a root cursor's first variable
            // can sit at a deep global level (e.g. S(b,c) first enters at the
            // global level of `b`, skipping `a`).
            let depth = self.cursors[ri].at_level();
            let next_global = ts.level_map[ri].get(depth).copied();
            if next_global == Some(level) {
                self.cursors[ri].open();
                self.stats.opens += 1;
                opened.push(ri);
            }
        }
        opened
    }

    fn leave_level(&mut self, opened: Vec<usize>) {
        for ri in opened {
            self.cursors[ri].up();
            self.stats.ups += 1;
        }
    }

    /// Main depth-first search at a global level.
    fn solve(&mut self, level: usize) {
        if self.stop {
            return;
        }
        let opened = self.enter_level(level);
        let rels = self.active(level).to_vec();

        // Resume path: leap every active cursor to the checkpoint bound first.
        if let Some(target) = self.seek_target[level] {
            for &ri in &rels {
                let c = &mut self.cursors[ri];
                if c.key() < target {
                    c.seek(target);
                    self.stats.seeks += 1;
                }
            }
        }

        let nlevels = self.ts.plan.ordered_vars.len();
        loop {
            if self.stop {
                break;
            }
            let key = match self.align(level) {
                Some(k) => k,
                None => break,
            };

            // On the resume prefix the aligned key must equal the checkpoint.
            if let Some(target) = self.seek_target[level] {
                if key != target {
                    // Stale/invalid checkpoint: treat as end (validated earlier
                    // too, this is a defensive stop).
                    break;
                }
            }

            self.bounds[level] = key;

            if level + 1 == nlevels {
                self.visit_leaf();
                if self.stop {
                    break;
                }
            } else {
                self.solve(level + 1);
                if self.stop {
                    break;
                }
            }

            // Advance one round-robin driver, then re-align.
            let k = rels.len();
            let d = self.driver[level] % k;
            self.driver[level] = self.driver[level].wrapping_add(1);
            let driver_ri = rels[d];
            self.cursors[driver_ri].next();
            self.stats.nexts += 1;
            // The checkpoint prefix only applies to the first key at each level.
            self.seek_target[level] = None;
        }

        self.leave_level(opened);
    }

    /// Emit the current full assignment, expanding multiplicity, until the
    /// budget is exhausted.
    fn visit_leaf(&mut self) {
        self.stats.assignments += 1;
        let mut mult: u64 = 1;
        for c in &self.cursors {
            // Every cursor is parked at its leaf; free-var-only relations too.
            mult = mult.saturating_mul(c.multiplicity());
        }

        let mut emitted_here: u64 = 0;
        let skip = self.skip_in_leaf;
        for _ in 0..mult {
            if emitted_here < skip {
                emitted_here += 1;
                continue;
            }
            if self.remaining == 0 {
                self.truncated = true;
                self.stop = true;
                break;
            }
            self.rows.push(self.bounds.clone());
            self.stats.rows_emitted += 1;
            self.remaining -= 1;
            emitted_here += 1;
        }
        // Skip applies only to the very first leaf on the resume path.
        self.skip_in_leaf = 0;

        if self.stop {
            // Record the cut inside this leaf.
            self.pending_checkpoint = Some(Checkpoint {
                plan_fingerprint: fingerprint(self.ts),
                bounds: self.bounds.clone(),
                within_leaf: emitted_here,
            });
        }
    }

    fn finish(self) -> JoinRun {
        let Engine {
            ts,
            cursors,
            rows,
            stats,
            truncated,
            pending_checkpoint,
            ..
        } = self;
        let per_cursor: BTreeMap<String, CursorStats> = ts
            .tries
            .iter()
            .zip(cursors.into_iter())
            .map(|(t, c)| (t.relation.clone(), c.into_stats()))
            .collect();
        let projected: Vec<Vec<Datum>> = rows
            .into_iter()
            .map(|codes| {
                ts.plan
                    .projection
                    .iter()
                    .map(|&gl| {
                        ts.domains[gl]
                            .decode(codes[gl])
                            .cloned()
                            .unwrap_or(Datum::Null)
                    })
                    .collect()
            })
            .collect();
        JoinRun {
            rows: projected,
            truncated,
            checkpoint: pending_checkpoint,
            stats,
            per_cursor,
        }
    }
}

/// Run a join with an optional row limit and optional resume checkpoint.
pub fn run(ts: &TrieSet, limit: Option<u64>, resume: Option<&Checkpoint>) -> Result<JoinRun> {
    if let Some(cp) = resume {
        if cp.plan_fingerprint != fingerprint(ts) {
            return Err(JoinError::new(
                ErrorCode::InvalidCursor,
                "resume cursor does not match this query/order/data",
            ));
        }
        if cp.bounds.len() != ts.plan.ordered_vars.len() {
            return Err(JoinError::new(
                ErrorCode::InvalidCursor,
                "resume cursor has wrong arity",
            ));
        }
    }
    if let Some(l) = limit {
        if l == 0 {
            return Err(JoinError::new(ErrorCode::InvalidLimit, "limit must be >= 1"));
        }
    }

    let mut engine = Engine::new(ts, limit, resume);
    engine.solve(0);
    Ok(engine.finish())
}
