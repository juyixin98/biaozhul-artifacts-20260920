//! Execution layer: turns a catalog + request into typed result batches,
//! handling planning, trie construction, paging, resumption and statistics.

use std::collections::BTreeMap;

use crate::batch::{Column, ColumnType, NamedColumn, RecordBatch};
use crate::leapfrog::{self, Checkpoint, JoinStats, TrieSet};
use crate::plan::{self, JoinPlan, JoinRequest};
use crate::types::Datum;

/// One page of a join result.
#[derive(Debug, Clone)]
pub struct Page {
    pub batch: RecordBatch,
    pub truncated: bool,
    /// Opaque token to fetch the next page (`None` when finished).
    pub next_cursor: Option<String>,
    pub stats: ExecStats,
}

/// Serializable execution statistics (work counters + sizes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecStats {
    pub seeks: u64,
    pub nexts: u64,
    pub opens: u64,
    pub ups: u64,
    pub assignments: u64,
    pub rows_emitted: u64,
    pub per_relation_seeks: BTreeMap<String, u64>,
    pub trie_nodes: BTreeMap<String, usize>,
    pub null_rejected: BTreeMap<String, usize>,
}

impl ExecStats {
    fn from_join(plan: &JoinPlan, ts: &TrieSet, stats: &JoinStats, per: &BTreeMap<String, crate::trie::CursorStats>) -> Self {
        ExecStats {
            seeks: stats.seeks,
            nexts: stats.nexts,
            opens: stats.opens,
            ups: stats.ups,
            assignments: stats.assignments,
            rows_emitted: stats.rows_emitted,
            per_relation_seeks: per.iter().map(|(k, v)| (k.clone(), v.seeks)).collect(),
            trie_nodes: ts
                .tries
                .iter()
                .map(|t| (t.relation.clone(), t.node_count()))
                .collect(),
            null_rejected: plan
                .relations
                .iter()
                .enumerate()
                .map(|(ri, pr)| (pr.name.clone(), ts.null_rejected[ri]))
                .collect(),
        }
    }
}

/// A prepared, reusable join (immutable tries), used by the service state.
pub struct PreparedJoin {
    pub ts: TrieSet,
}

impl PreparedJoin {
    pub fn prepare(request: &JoinRequest, catalog: &BTreeMap<String, RecordBatch>) -> crate::Result<Self> {
        let plan = plan::plan(request, catalog)?;
        let rows = extract_rows(catalog, &plan);
        let ts = TrieSet::build(plan, &rows)?;
        Ok(PreparedJoin { ts })
    }

    /// Run one page. `cursor` resumes a previous page; `limit` bounds this page.
    pub fn page(&self, limit: Option<u64>, cursor: Option<&str>) -> crate::Result<Page> {
        let resume = match cursor {
            None => None,
            Some(tok) => Some(Checkpoint::decode(tok)?),
        };
        let run = leapfrog::run(&self.ts, limit, resume.as_ref())?;
        let batch = build_batch(&self.ts.plan, &run.rows)?;
        let next_cursor = if run.truncated {
            run.checkpoint.as_ref().map(|cp| cp.encode())
        } else {
            None
        };
        let stats = ExecStats::from_join(&self.ts.plan, &self.ts, &run.stats, &run.per_cursor);
        Ok(Page {
            batch,
            truncated: run.truncated,
            next_cursor,
            stats,
        })
    }

    /// Drain all pages by repeatedly resuming (used by verification/CLI).
    pub fn drain(&self, page_size: u64) -> crate::Result<(RecordBatch, Vec<ExecStats>)> {
        let mut all_rows: Vec<Vec<Datum>> = Vec::new();
        let mut stats_log = Vec::new();
        let mut cursor: Option<String> = None;
        let mut types: Vec<ColumnType> = Vec::new();
        let mut names: Vec<String> = Vec::new();
        loop {
            let page = self.page(Some(page_size), cursor.as_deref())?;
            if names.is_empty() {
                names = page.batch.columns.iter().map(|c| c.name.clone()).collect();
                types = page.batch.columns.iter().map(|c| c.column.col_type()).collect();
            }
            for r in 0..page.batch.n_rows() {
                all_rows.push(page.batch.row_datums(r));
            }
            stats_log.push(page.stats);
            match page.next_cursor {
                Some(tok) => cursor = Some(tok),
                None => break,
            }
        }
        let batch = RecordBatch::from_rows(&names, &types, all_rows)?;
        Ok((batch, stats_log))
    }
}

/// Pull every relation's rows out of the catalog in catalog column order.
fn extract_rows(
    catalog: &BTreeMap<String, RecordBatch>,
    plan: &JoinPlan,
) -> Vec<Vec<Vec<Datum>>> {
    plan.relations
        .iter()
        .map(|pr| {
            let batch = &catalog[&pr.name];
            (0..batch.n_rows()).map(|r| batch.row_datums(r)).collect()
        })
        .collect()
}

/// Build a typed output batch from decoded datum rows.
fn build_batch(plan: &JoinPlan, rows: &[Vec<Datum>]) -> crate::Result<RecordBatch> {
    let names: Vec<String> = plan
        .projection
        .iter()
        .map(|&gl| plan.ordered_vars[gl].clone())
        .collect();
    let types: Vec<ColumnType> = plan.projection.iter().map(|&gl| plan.types[gl]).collect();
    let mut datum_cols: Vec<Vec<Datum>> = vec![Vec::with_capacity(rows.len()); names.len()];
    for r in rows {
        for (i, d) in r.iter().enumerate() {
            datum_cols[i].push(d.clone());
        }
    }
    let columns = names
        .iter()
        .zip(types.iter())
        .zip(datum_cols.into_iter())
        .map(|((name, ty), col)| NamedColumn {
            name: name.clone(),
            column: Column::from_datums(*ty, &col)
                .expect("output type matches plan"),
        })
        .collect();
    RecordBatch::new(columns)
}
