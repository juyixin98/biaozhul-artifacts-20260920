//! Leapfrog Triejoin execution.
//!
//! Turns a resolved [`Plan`](crate::query::model::Plan) into a resumable,
//! access-counted enumerator following the classic Leapfrog Triejoin:
//!
//! * at every variable level the participating trie iterators are
//!   *intersected* with the standard min/max `seek` procedure - domain
//!   iteration *jumps*, it never scans a whole level;
//! * when all participants share a key, the participants descend and the
//!   enumeration recurses to the next global variable;
//! * an atom whose trie skips the current variable is left parked at its
//!   own trie depth; when the search enters a level under an unchanged
//!   ancestor prefix, that atom is *reopened* at its range start - the
//!   cyclic/general case Leapfrog handles beyond acyclic joins;
//! * output multiplicity is the product of the leaf multiplicities parked
//!   by the atom iterators, so duplicate input rows are counted exactly;
//! * the search is an explicit depth stack, so an enumeration stopped by an
//!   output or access budget *resumes* from an opaque [`Cursor`] without
//!   re-emitting any tuple.
//!
//! No partial-assignment table is ever materialized: live state is
//! `O(variables * atoms)` cursor frames. This is the property the naive
//! oracle in [`crate::query::oracle`] deliberately lacks.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::{Deserialize, Serialize};

use crate::error::{Error, ReasonCode, Result};
use crate::query::model::Plan;
use crate::query::trie::TrieIterator;
use crate::query::value::Scalar;

/// One answer tuple and its multiset multiplicity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnswerRow {
    pub values: Vec<Scalar>,
    pub multiplicity: u64,
}

/// Primitive trie access counters aggregated across every atom iterator.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct JoinStats {
    pub opens: u64,
    pub seeks: u64,
    pub nexts: u64,
    pub downs: u64,
    /// Distinct tuples emitted so far (cumulative across resume slices).
    pub rows_emitted: u64,
    /// Summed multiplicity of emitted tuples.
    pub multiplicity_emitted: u64,
}

impl JoinStats {
    pub fn total_accesses(&self) -> u64 {
        self.opens + self.seeks + self.nexts + self.downs
    }
}

/// Why a [`run`] returned.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    /// Every tuple of the join was enumerated.
    Exhausted,
    /// The distinct-row output limit was reached; a cursor is attached.
    RowLimit,
    /// The primitive-access budget was reached; a cursor is attached.
    AccessBudget,
}

/// Parameters of one (possibly resumed) enumeration slice.
#[derive(Debug, Clone, Copy)]
pub struct RunParams {
    /// Maximum distinct tuples returned in this slice. Must be >= 1.
    pub row_limit: usize,
    /// Optional ceiling on primitive trie accesses consumed *in this slice*
    /// (cumulative totals are still reported in [`JoinStats`]).
    pub access_budget: Option<u64>,
}

impl Default for RunParams {
    fn default() -> Self {
        Self {
            row_limit: 1000,
            access_budget: None,
        }
    }
}

/// Result of driving the enumerator for one slice.
#[derive(Debug)]
pub struct RunOutcome {
    pub rows: Vec<AnswerRow>,
    pub stats: JoinStats,
    pub stop: StopReason,
    /// Present iff `stop != Exhausted`.
    pub cursor: Option<Cursor>,
}

// ---------------------------------------------------------------------------
// Opaque resumable cursor
// ---------------------------------------------------------------------------

/// Wire form of one iterator frame `(range.start, range.end, pos)`.
type WireFrame = (usize, usize, usize);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum WirePhase {
    Seeking,
    Descended,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CursorWire {
    /// Cursor format version.
    v: u32,
    version: u64,
    fingerprint: String,
    /// Frames per atom iterator (outer index = atom).
    iterators: Vec<Vec<WireFrame>>,
    /// Number of fixed global-variable levels at pause.
    depth: usize,
    /// Phase of the top (last fixed) level at pause.
    top_phase: WirePhase,
    /// Cumulative primitive accesses already consumed by earlier slices.
    accesses: u64,
    /// Per-category cumulative counters inherited from earlier slices.
    floors: (u64, u64, u64, u64),
    /// Cumulative distinct rows emitted in earlier slices.
    rows_emitted: u64,
    /// Cumulative multiplicity emitted in earlier slices.
    multiplicity_emitted: u64,
}

/// Opaque resume token.
///
/// It pins the catalog version and query fingerprint: a resumed request that
/// no longer matches is rejected with `cursor_version` / `cursor_plan`
/// instead of silently continuing over changed data.
#[derive(Debug, Clone)]
pub struct Cursor(CursorWire);

impl Cursor {
    pub fn encode(&self) -> String {
        let json = serde_json::to_vec(&self.0).expect("cursor serializes");
        URL_SAFE_NO_PAD.encode(json)
    }

    pub fn decode(token: &str) -> Result<Self> {
        let raw = URL_SAFE_NO_PAD
            .decode(token.as_bytes())
            .map_err(|_| cursor_err("token is not valid base64url"))?;
        let wire: CursorWire = serde_json::from_slice(&raw)
            .map_err(|_| cursor_err("token payload is not a valid cursor"))?;
        if wire.v != 1 {
            return Err(cursor_err("unsupported cursor format version"));
        }
        if wire.depth == 0 {
            return Err(cursor_err("cursor has no fixed levels"));
        }
        Ok(Cursor(wire))
    }

    pub(crate) fn depth(&self) -> usize {
        self.0.depth
    }
}

fn cursor_err(msg: &str) -> Error {
    Error::validation(ReasonCode::CursorDecode, msg)
}

// ---------------------------------------------------------------------------
// Enumerator
// ---------------------------------------------------------------------------

/// State of one level on the depth stack.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// Intersection still in progress (or not yet started) at this level.
    Seeking,
    /// A shared key was found and the participants descended; child levels
    /// are being (or were) enumerated.
    Descended,
}

impl Phase {
    fn wire(self) -> WirePhase {
        match self {
            Phase::Seeking => WirePhase::Seeking,
            Phase::Descended => WirePhase::Descended,
        }
    }
}

#[derive(Debug)]
struct LevelState {
    level: usize,
    phase: Phase,
    /// Atom indices participating at this level.
    participants: Vec<usize>,
    /// Shared key fixed at this level (`None` while seeking).
    key: Option<Scalar>,
}

/// Result of one intersection round.
enum Intersect {
    Found(Scalar),
    End,
    Budget,
}

/// What one pump produced.
enum Pump {
    Row(AnswerRow),
    Budget,
    Done,
}

/// The resumable Leapfrog Triejoin enumerator.
pub struct Enumerator<'a> {
    plan: &'a Plan,
    iters: Vec<TrieIterator<'a>>,
    stack: Vec<LevelState>,
    /// Aggregate iterator counters at the start of *this* slice.
    counters_floor: (u64, u64, u64, u64),
    /// Per-category cumulative counters inherited from previous slices.
    inherited: (u64, u64, u64, u64),
    rows_floor: u64,
    mult_floor: u64,
}

/// Run a fresh enumeration (or resume `cursor`) for one slice.
pub fn run(plan: &Plan, params: RunParams, cursor: Option<&Cursor>) -> Result<RunOutcome> {
    if params.row_limit == 0 {
        return Err(Error::validation(
            ReasonCode::InvalidLimit,
            "row_limit must be at least 1",
        ));
    }
    let mut en = match cursor {
        None => Enumerator::new(plan),
        Some(c) => Enumerator::resume(plan, c)?,
    };
    Ok(en.drive(params))
}

fn it_key<'a>(it: &'a TrieIterator, trie_depth: usize) -> Option<&'a Scalar> {
    if it.depth() == trie_depth {
        it.key()
    } else {
        it.frame_key(trie_depth)
    }
}

impl<'a> Enumerator<'a> {
    /// Open a fresh enumerator over `plan`.
    fn new(plan: &'a Plan) -> Self {
        let iters = plan.views.iter().map(TrieIterator::open).collect();
        Self {
            plan,
            iters,
            stack: Vec::new(),
            counters_floor: (0, 0, 0, 0),
            inherited: (0, 0, 0, 0),
            rows_floor: 0,
            mult_floor: 0,
        }
    }

    /// Restore an enumerator from a previously issued cursor.
    fn resume(plan: &'a Plan, cursor: &Cursor) -> Result<Self> {
        let wire = &cursor.0;
        if wire.version != plan.catalog_version {
            return Err(Error::validation(
                ReasonCode::CursorVersion,
                "cursor was issued against a different catalog version; re-run the query",
            ));
        }
        if wire.fingerprint != plan.shape.fingerprint() {
            return Err(Error::validation(
                ReasonCode::CursorPlan,
                "cursor belongs to a different query shape",
            ));
        }
        if wire.iterators.len() != plan.views.len() {
            return Err(cursor_err("cursor iterator count does not match the plan"));
        }
        if wire.depth == 0 || wire.depth > plan.global_order.len() {
            return Err(cursor_err("cursor fixed-level count is out of range"));
        }

        let mut en = Self::new(plan);
        for (it, frames) in en.iters.iter_mut().zip(wire.iterators.iter()) {
            it.restore_frames_checked(frames)?;
        }

        // Rebuild the bookkeeping stack from the stored frames. No trie
        // accesses are repeated: the frames already encode the open ranges
        // and parked positions.
        for level in 0..wire.depth {
            let participants = plan.participants(level);
            if participants.is_empty() {
                return Err(cursor_err("cursor names a level with no participants"));
            }
            let leader = participants[0];
            let trie_depth = en.trie_depth(leader, level);
            let key = it_key(&en.iters[leader], trie_depth)
                .cloned()
                .ok_or_else(|| cursor_err("cursor is positioned at an exhausted level"))?;
            let phase = if level + 1 == wire.depth {
                match wire.top_phase {
                    WirePhase::Seeking => Phase::Seeking,
                    WirePhase::Descended => Phase::Descended,
                }
            } else {
                Phase::Descended
            };
            en.stack.push(LevelState {
                level,
                phase,
                participants,
                key: Some(key),
            });
        }

        en.inherited = wire.floors;
        en.rows_floor = wire.rows_emitted;
        en.mult_floor = wire.multiplicity_emitted;
        // Restored frames do not replay the original `open`s; make the
        // inherited iterator counters the floor for this slice.
        en.counters_floor = en.aggregate_counters();
        Ok(en)
    }

    /// Trie depth at which atom `atom_idx` stores the variable at global
    /// `level` (the atom's variables appear in global order inside its view).
    fn trie_depth(&self, atom_idx: usize, level: usize) -> usize {
        let var = &self.plan.global_order[level];
        self.plan.resolved[atom_idx]
            .ordered_vars
            .iter()
            .position(|v| v == var)
            .expect("participant binds this variable")
    }

    fn aggregate_counters(&self) -> (u64, u64, u64, u64) {
        self.iters
            .iter()
            .map(TrieIterator::access_breakdown)
            .fold((0u64, 0u64, 0u64, 0u64), |(o, s, n, d), b| {
                (o + b.0, s + b.1, n + b.2, d + b.3)
            })
    }

    fn slice_accesses(&self) -> u64 {
        let (o, s, n, d) = self.aggregate_counters();
        let (fo, fs, fn_, fd) = self.counters_floor;
        (o + s + n + d).saturating_sub(fo + fs + fn_ + fd)
    }

    fn over_budget(&self, budget: Option<u64>) -> bool {
        matches!(budget, Some(cap) if self.slice_accesses() >= cap)
    }

    fn cumulative_stats(&self, slice_rows: u64, slice_mult: u64) -> JoinStats {
        let (o, s, n, d) = self.aggregate_counters();
        let (fo, fs, fn_, fd) = self.counters_floor;
        let (io, is_, in_, id_) = self.inherited;
        JoinStats {
            opens: io + (o - fo),
            seeks: is_ + (s - fs),
            nexts: in_ + (n - fn_),
            downs: id_ + (d - fd),
            rows_emitted: self.rows_floor + slice_rows,
            multiplicity_emitted: self.mult_floor + slice_mult,
        }
    }

    fn drive(mut self, params: RunParams) -> RunOutcome {
        let mut rows = Vec::new();
        let mut slice_rows = 0u64;
        let mut slice_mult = 0u64;
        let mut stop = StopReason::Exhausted;

        loop {
            match self.pump(params.access_budget) {
                Pump::Row(row) => {
                    slice_rows += 1;
                    slice_mult = slice_mult.saturating_add(row.multiplicity);
                    rows.push(row);
                    if rows.len() >= params.row_limit {
                        stop = StopReason::RowLimit;
                        break;
                    }
                }
                Pump::Budget => {
                    stop = StopReason::AccessBudget;
                    break;
                }
                Pump::Done => break,
            }
        }

        let stats = self.cumulative_stats(slice_rows, slice_mult);
        let cursor = if stop == StopReason::Exhausted {
            None
        } else {
            Some(self.snapshot(&stats))
        };
        RunOutcome {
            rows,
            stats,
            stop,
            cursor,
        }
    }

    fn snapshot(&self, stats: &JoinStats) -> Cursor {
        let top_phase = self
            .stack
            .last()
            .map(|s| s.phase.wire())
            .unwrap_or(WirePhase::Seeking);
        Cursor(CursorWire {
            v: 1,
            version: self.plan.catalog_version,
            fingerprint: self.plan.shape.fingerprint(),
            iterators: self
                .iters
                .iter()
                .map(TrieIterator::snapshot_frames)
                .collect(),
            depth: self.stack.len(),
            top_phase,
            accesses: stats.total_accesses(),
            floors: (stats.opens, stats.seeks, stats.nexts, stats.downs),
            rows_emitted: stats.rows_emitted,
            multiplicity_emitted: stats.multiplicity_emitted,
        })
    }

    /// Open global `level`: position every participant at the trie depth of
    /// this level's variable, then push a fresh seeking frame.
    ///
    /// `depth == want` means the atom skips this variable *and* a frame at
    /// that trie depth was left behind by a previous sibling branch. The
    /// parked frame still reflects the *previous* key, so it must be reopened
    /// at its range start. `depth > want` happens when an atom bound at this
    /// level is currently parked inside a completed child subtree: lift it
    /// back and reopen the ancestor range.
    fn push_level(&mut self, level: usize) {
        let participants = self.plan.participants(level);
        debug_assert!(!participants.is_empty(), "every variable has an atom");
        for &idx in &participants {
            let want = self.trie_depth(idx, level);
            if self.iters[idx].depth() < want {
                while self.iters[idx].depth() < want {
                    self.iters[idx].down();
                }
            } else {
                while self.iters[idx].depth() > want {
                    self.iters[idx].up();
                }
                self.iters[idx].reopen();
            }
            debug_assert_eq!(self.iters[idx].depth(), want);
        }
        self.stack.push(LevelState {
            level,
            phase: Phase::Seeking,
            participants,
            key: None,
        });
    }

    /// Leapfrog intersection over the participants at the top level.
    ///
    /// Textbook min/max variant: find the smallest current key; if every
    /// participant is on it the intersection found it, otherwise leap every
    /// participant still below the *maximum* key forward. All motion is
    /// forward `seek`/`next`, so no shared key can be skipped, and every move
    /// is logarithmic in the level size rather than a linear scan.
    fn intersect(&mut self, budget: Option<u64>) -> Intersect {
        let participants = self.stack.last().unwrap().participants.clone();
        debug_assert!(!participants.is_empty());

        // Unary "intersection": enumerate the sole participant's keys.
        if participants.len() == 1 {
            if self.over_budget(budget) {
                return Intersect::Budget;
            }
            return match self.iters[participants[0]].key() {
                Some(k) => Intersect::Found(k.clone()),
                None => Intersect::End,
            };
        }

        loop {
            if self.over_budget(budget) {
                return Intersect::Budget;
            }

            let mut min_key: Option<Scalar> = None;
            let mut max_key: Option<Scalar> = None;
            for &idx in &participants {
                let key = match self.iters[idx].key() {
                    Some(k) => k.clone(),
                    None => return Intersect::End,
                };
                match &min_key {
                    Some(cur) if *cur <= key => {}
                    _ => min_key = Some(key.clone()),
                }
                match &max_key {
                    Some(cur) if *cur >= key => {}
                    _ => max_key = Some(key),
                }
            }
            let min_key = min_key.unwrap();
            let max_key = max_key.unwrap();
            if min_key == max_key {
                return Intersect::Found(max_key);
            }

            // Every participant below the current maximum leaps to it.
            for &idx in &participants {
                match self.iters[idx].key() {
                    Some(k) if *k < max_key => self.iters[idx].seek(&max_key),
                    Some(_) => {}
                    None => return Intersect::End,
                }
            }
        }
    }

    /// Advance the search by one step: emit one row, hit the budget, or
    /// finish. Implements the standard Leapfrog Triejoin depth-first loop
    /// (`seek -> descend; ...; at end, up/next`).
    fn pump(&mut self, budget: Option<u64>) -> Pump {
        let nvars = self.plan.global_order.len();

        if self.stack.is_empty() {
            self.push_level(0);
        }

        loop {
            let (level, phase) = {
                let top = self.stack.last().expect("stack non-empty during pump");
                (top.level, top.phase)
            };

            match phase {
                Phase::Seeking => match self.intersect(budget) {
                    Intersect::Budget => return Pump::Budget,
                    Intersect::End => {
                        self.retract();
                        if self.stack.is_empty() {
                            return Pump::Done;
                        }
                    }
                    Intersect::Found(key) => {
                        let top = self.stack.last_mut().unwrap();
                        top.phase = Phase::Descended;
                        top.key = Some(key);
                        if level + 1 == nvars {
                            return Pump::Row(self.emit_leaf());
                        }
                        self.push_level(level + 1);
                    }
                },
                Phase::Descended => {
                    // Child subtree for the fixed key is complete: advance
                    // this level's participants at their parked trie depth,
                    // then intersect again.
                    let participants =
                        self.stack.last().unwrap().participants.clone();
                    for &idx in &participants {
                        self.iters[idx].next();
                    }
                    let top = self.stack.last_mut().unwrap();
                    top.phase = Phase::Seeking;
                    top.key = None;
                }
            }
        }
    }

    /// Close the top level after its intersection reached the end:
    ///
    /// Every participant advances past the exhausted key *at the depth it is
    /// parked at*, then is lifted back to the trie depth of the deepest
    ancestor level it also binds (its root depth when it binds no ancestor
    /// variable). The lift is essential for atoms whose variables skip a
    /// global level: in the triangle, `T(a,c)` is parked at trie depth 1
    /// (variable `c`) while global level `c` closes; leaving it there would
    /// make the outer `a` level read a stale `c` key instead of the next `a`.
    /// Advancing/lifting happens only for direct participants of the closed
    /// level; iterators parked deeper by a still-open child level are left
    /// exactly where they are.
    fn retract(&mut self) {
        let Some(top) = self.stack.pop() else { return };

        for &idx in &top.participants {
            self.iters[idx].next();
            // Frame depth to return to: the trie depth of the deepest
            // already-fixed level this atom binds, or its root.
            let mut lift_to = 0usize;
            for ancestor in &self.stack {
                if self.plan.resolved[idx]
                    .ordered_vars
                    .contains(&self.plan.global_order[ancestor.level])
                {
                    lift_to = self.trie_depth(idx, ancestor.level);
                }
            }
            while self.iters[idx].depth() > lift_to {
                self.iters[idx].up();
            }
        }
    }

    /// Build the output tuple for a fully fixed binding, with multiplicity the
    /// product of the leaf multiplicities parked by the atom iterators.
    fn emit_leaf(&mut self) -> AnswerRow {
        let nvars = self.plan.global_order.len();
        let mut values = Vec::with_capacity(nvars);
        for ls in &self.stack {
            values.push(ls.key.clone().expect("fixed level has a key"));
        }
        debug_assert_eq!(values.len(), nvars);

        let mut multiplicity: u64 = 1;
        for (idx, it) in self.iters.iter().enumerate() {
            debug_assert_eq!(
                it.depth() + 1,
                self.plan.resolved[idx].ordered_vars.len(),
                "every atom iterator is parked on a leaf"
            );
            multiplicity = multiplicity.saturating_mul(it.leaf_multiplicity());
        }
        AnswerRow {
            values,
            multiplicity,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::query::model::{
        resolve, Atom, PlannerConfig, QueryShape, SchemaSource,
    };
    use crate::query::trie::Trie;

    fn s(i: i64) -> Scalar {
        Scalar::Int(i)
    }

    struct Catalog {
        relations: BTreeMap<String, Trie>,
        version: u64,
    }

    impl Catalog {
        fn new(version: u64) -> Self {
            Self {
                relations: BTreeMap::new(),
                version,
            }
        }

        fn put(&mut self, name: &str, rows: Vec<Vec<Scalar>>) {
            self.relations.insert(name.to_string(), Trie::build(rows));
        }

        fn plan(&self, shape: &QueryShape) -> Plan {
            resolve(shape, self, &PlannerConfig::default()).unwrap()
        }
    }

    impl SchemaSource for Catalog {
        fn arity(&self, relation: &str) -> Option<usize> {
            self.relations.get(relation).map(Trie::arity)
        }
        fn multiset_rows(&self, relation: &str) -> Option<Vec<(Vec<Scalar>, u64)>> {
            self.relations.get(relation).map(Trie::distinct_leaves)
        }
        fn version(&self) -> u64 {
            self.version
        }
    }

    /// Drive the enumerator to completion, resuming through every limit.
    fn collect_all(plan: &Plan, row_limit: usize) -> (Vec<AnswerRow>, JoinStats) {
        let mut out = Vec::new();
        let mut cursor = None;
        let mut last_stats = JoinStats::default();
        loop {
            let outcome = run(
                plan,
                RunParams {
                    row_limit,
                    access_budget: None,
                },
                cursor.as_ref(),
            )
            .unwrap();
            last_stats = outcome.stats;
            out.extend(outcome.rows);
            match outcome.stop {
                StopReason::Exhausted => break,
                _ => cursor = outcome.cursor,
            }
            assert!(cursor.is_some(), "non-exhausted slice must return a cursor");
        }
        (out, last_stats)
    }

    fn triangle_catalog() -> Catalog {
        let mut cat = Catalog::new(1);
        cat.put(
            "R",
            vec![
                vec![s(1), s(2)],
                vec![s(1), s(2)],
                vec![s(2), s(3)],
                vec![s(3), s(4)],
            ],
        );
        cat.put(
            "S",
            vec![vec![s(2), s(3)], vec![s(3), s(4)], vec![s(4), s(5)]],
        );
        cat.put(
            "T",
            vec![vec![s(1), s(3)], vec![s(2), s(4)], vec![s(3), s(5)]],
        );
        cat
    }

    fn triangle_shape() -> QueryShape {
        QueryShape::new(vec![
            Atom::new("R", &["a", "b"]),
            Atom::new("S", &["b", "c"]),
            Atom::new("T", &["a", "c"]),
        ])
    }

    #[test]
    fn triangle_join_matches_expected_multiset() {
        let cat = triangle_catalog();
        let plan = cat.plan(&triangle_shape());
        let (rows, stats) = collect_all(&plan, 100);
        assert_eq!(
            rows,
            vec![
                AnswerRow {
                    values: vec![s(1), s(2), s(3)],
                    multiplicity: 2,
                },
                AnswerRow {
                    values: vec![s(2), s(3), s(4)],
                    multiplicity: 1,
                },
            ]
        );
        assert_eq!(stats.rows_emitted, 2);
        assert_eq!(stats.multiplicity_emitted, 3);
    }

    #[test]
    fn row_limit_resumes_without_loss_or_duplication() {
        let cat = triangle_catalog();
        let plan = cat.plan(&triangle_shape());
        // One row per slice exercises leaf retraction + resume every time.
        let (rows_limit1, _) = collect_all(&plan, 1);
        let (rows_limit3, _) = collect_all(&plan, 3);
        assert_eq!(rows_limit1.len(), 2);
        assert_eq!(rows_limit1, rows_limit3);
        assert_eq!(rows_limit1[0].multiplicity, 2);
        assert_eq!(rows_limit1[1].multiplicity, 1);
    }

    #[test]
    fn resume_cursor_is_pinned_to_shape_and_version() {
        let cat = triangle_catalog();
        let plan = cat.plan(&triangle_shape());
        let first = run(
            &plan,
            RunParams {
                row_limit: 1,
                access_budget: None,
            },
            None,
        )
        .unwrap();
        assert_eq!(first.stop, StopReason::RowLimit);
        let token = first.cursor.unwrap().encode();

        assert_eq!(
            Cursor::decode("not-a-cursor")
                .unwrap_err()
                .reason()
                .unwrap()
                .code,
            ReasonCode::CursorDecode
        );
        let cursor = Cursor::decode(&token).unwrap();

        let again = run(
            &plan,
            RunParams {
                row_limit: 10,
                access_budget: None,
            },
            Some(&cursor),
        )
        .unwrap();
        assert_eq!(again.stop, StopReason::Exhausted);

        // Different query shape -> CursorPlan.
        let mut other_cat = Catalog::new(1);
        other_cat.put("R", vec![vec![s(1), s(2)]]);
        other_cat.put("S", vec![vec![s(2), s(3)]]);
        other_cat.put("T", vec![vec![s(1), s(3)]]);
        let other_shape = QueryShape::new(vec![
            Atom::new("R", &["x", "y"]),
            Atom::new("S", &["y", "z"]),
            Atom::new("T", &["x", "z"]),
        ]);
        let other_plan = other_cat.plan(&other_shape);
        let err = run(
            &other_plan,
            RunParams {
                row_limit: 10,
                access_budget: None,
            },
            Some(&cursor),
        )
        .unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::CursorPlan);

        // Same shape, newer catalog version -> CursorVersion.
        let moved_cat = Catalog {
            relations: cat.relations.clone(),
            version: 2,
        };
        let moved_plan = moved_cat.plan(&triangle_shape());
        let err = run(
            &moved_plan,
            RunParams {
                row_limit: 10,
                access_budget: None,
            },
            Some(&cursor),
        )
        .unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::CursorVersion);
    }

    #[test]
    fn access_budget_pauses_and_resumes_to_completion() {
        let cat = triangle_catalog();
        let plan = cat.plan(&triangle_shape());

        let mut rows = Vec::new();
        let mut cursor = None;
        let mut slices = 0;
        loop {
            let outcome = run(
                &plan,
                RunParams {
                    row_limit: 1000,
                    access_budget: Some(3),
                },
                cursor.as_ref(),
            )
            .unwrap();
            slices += 1;
            rows.extend(outcome.rows);
            match outcome.stop {
                StopReason::Exhausted => break,
                StopReason::AccessBudget => {
                    assert!(outcome.cursor.is_some());
                    cursor = outcome.cursor;
                }
                StopReason::RowLimit => panic!("row limit should not trigger"),
            }
            assert!(slices < 100, "budget resume must make progress");
        }
        assert_eq!(rows.len(), 2);
        assert!(slices >= 2, "a tiny budget must require several slices");
    }

    #[test]
    fn zero_limit_is_rejected() {
        let cat = triangle_catalog();
        let plan = cat.plan(&triangle_shape());
        let err = run(
            &plan,
            RunParams {
                row_limit: 0,
                access_budget: None,
            },
            None,
        )
        .unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::InvalidLimit);
    }

    /// Cyclic case where one atom skips a global level: in the (a,b,c)
    /// order T(a,c) skips level b. Its domain under each `a` must reopen at
    /// the range start or only the first branch would be enumerated.
    #[test]
    fn atom_skipping_a_level_is_reopened_per_branch() {
        let mut cat = Catalog::new(1);
        cat.put(
            "R",
            vec![
                vec![s(1), s(1)],
                vec![s(1), s(2)],
                vec![s(2), s(1)],
                vec![s(2), s(2)],
            ],
        );
        cat.put("S", vec![vec![s(1), s(9)], vec![s(2), s(9)]]);
        cat.put("T", vec![vec![s(1), s(9)], vec![s(2), s(9)]]);
        let shape = QueryShape::new(vec![
            Atom::new("R", &["a", "b"]),
            Atom::new("S", &["b", "c"]),
            Atom::new("T", &["a", "c"]),
        ]);
        let plan = cat.plan(&shape);
        let (rows, _) = collect_all(&plan, 1);
        assert_eq!(
            rows.iter().map(|r| r.values.clone()).collect::<Vec<_>>(),
            vec![
                vec![s(1), s(1), s(9)],
                vec![s(1), s(2), s(9)],
                vec![s(2), s(1), s(9)],
                vec![s(2), s(2), s(9)],
            ]
        );
    }

    /// Unary atom binding only the middle variable must reopen whenever the
    /// level before it changes, even though it never descends with that
    /// level.
    #[test]
    fn unary_atom_on_inner_variable() {
        let mut cat = Catalog::new(1);
        cat.put(
            "R",
            vec![vec![s(1), s(10)], vec![s(2), s(20)], vec![s(3), s(30)]],
        );
        cat.put("B", vec![vec![s(10)], vec![s(30)]]);
        let shape = QueryShape::new(vec![
            Atom::new("R", &["a", "b"]),
            Atom::new("B", &["b"]),
        ]);
        let plan = cat.plan(&shape);
        let (rows, _) = collect_all(&plan, 1);
        assert_eq!(
            rows.iter().map(|r| r.values.clone()).collect::<Vec<_>>(),
            vec![vec![s(1), s(10)], vec![s(3), s(30)]]
        );
    }

    #[test]
    fn permuted_views_join_like_native_ones() {
        let mut cat = Catalog::new(1);
        cat.put("R", vec![vec![s(1), s(2)], vec![s(2), s(3)]]);
        // S is stored (c, b); the atom forces a reindex view.
        cat.put("S", vec![vec![s(5), s(2)], vec![s(6), s(3)]]);
        let shape = QueryShape::new(vec![
            Atom::new("R", &["a", "b"]),
            Atom::new("S", &["c", "b"]),
        ]);
        let plan = cat.plan(&shape);
        let (rows, _) = collect_all(&plan, 10);
        assert_eq!(
            rows.iter().map(|r| r.values.clone()).collect::<Vec<_>>(),
            vec![vec![s(1), s(2), s(5)], vec![s(2), s(3), s(6)]]
        );
    }

    /// Sparse intersection: the level-0 domains share only one far-away key.
    /// Finding it must take logarithmically few seeks rather than scanning
    /// the thousands of non-matching keys.
    #[test]
    fn sparse_intersection_leaps_to_the_only_hit() {
        let mut cat = Catalog::new(1);
        let dense: Vec<Vec<Scalar>> = (0..10_000).map(|i| vec![s(i)]).collect();
        cat.put("Dense", dense);
        cat.put("Sparse", vec![vec![s(9_001)]]);
        let shape = QueryShape::new(vec![
            Atom::new("Dense", &["x"]),
            Atom::new("Sparse", &["x"]),
        ]);
        let plan = cat.plan(&shape);
        let outcome = run(
            &plan,
            RunParams {
                row_limit: 10,
                access_budget: None,
            },
            None,
        )
        .unwrap();
        assert_eq!(outcome.stop, StopReason::Exhausted);
        assert_eq!(
            outcome.rows,
            vec![AnswerRow {
                values: vec![s(9_001)],
                multiplicity: 1,
            }]
        );
        assert!(
            outcome.stats.seeks <= 2,
            "expected logarithmic seeks, got {}",
            outcome.stats.seeks
        );
    }
}
