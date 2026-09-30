//! Shared application state: the catalog of loaded [`Dataset`]s (behind an
//! `RwLock` because deletions mutate the version clock), configuration, and a
//! monotonic run-id counter used to correlate logs with requests.

use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};

use crate::config::Config;
use crate::dataset::Dataset;
use crate::error::{Result, TritmapError};
use crate::verify::gen::{generate_case, BOUNDARY_LENGTHS};
use crate::verify::{cross_check, VerifyReport};

#[derive(Default)]
struct Catalog {
    tables: BTreeMap<String, Dataset>,
}

pub struct AppState {
    catalog: RwLock<Catalog>,
    config: Config,
    run_counter: AtomicU64,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        Self {
            catalog: RwLock::new(Catalog::default()),
            config,
            run_counter: AtomicU64::new(1),
        }
    }

    pub fn config(&self) -> Config {
        self.config.clone()
    }

    /// Correlatable run identity: monotonic number plus coarse timestamp.
    pub fn next_run_id(&self) -> String {
        let seq = self.run_counter.fetch_add(1, Ordering::Relaxed);
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        format!("run-{ts}-{seq:06}")
    }

    pub fn insert_table(&self, name: impl Into<String>, dataset: Dataset) {
        let mut cat = self.catalog.write().expect("catalog lock poisoned");
        cat.tables.insert(name.into(), dataset);
    }

    pub fn table_names(&self) -> Vec<String> {
        let cat = self.catalog.read().expect("catalog lock poisoned");
        cat.tables.keys().cloned().collect()
    }

    /// Read-only access to one table for a scoped operation.
    pub fn with_table<T>(&self, name: &str, f: impl FnOnce(&Dataset) -> Result<T>) -> Result<T> {
        let cat = self.catalog.read().expect("catalog lock poisoned");
        let d = cat
            .tables
            .get(name)
            .ok_or_else(|| TritmapError::UnknownTable(name.to_string()))?;
        f(d)
    }

    pub fn delete_row(&self, name: &str, row: usize, version: u64) -> Result<()> {
        let mut cat = self.catalog.write().expect("catalog lock poisoned");
        let d = cat
            .tables
            .get_mut(name)
            .ok_or_else(|| TritmapError::UnknownTable(name.to_string()))?;
        d.delete_row(row, version)
    }

    /// Execute differential verification on throwaway synthetic datasets, so
    /// randomized deletions never mutate tables loaded for serving.
    pub fn run_differential_verify(
        self: &Arc<Self>,
        base_seed: u64,
        iterations: usize,
        rows: usize,
        null_density: u32,
    ) -> Result<Vec<VerifyReport>> {
        if iterations == 0 {
            return Err(TritmapError::Config("iterations must be >= 1".into()));
        }
        if null_density > 100 {
            return Err(TritmapError::Config(
                "null_density must be in 0..=100".into(),
            ));
        }

        let mut reports = Vec::with_capacity(iterations);
        for i in 0..iterations {
            // First iterations pin the awkward universe lengths; the next one
            // is a dedicated all-NULL / all-UNKNOWN case; the rest use the
            // configured size and density.
            let (case_rows, density) = if let Some(&boundary) = BOUNDARY_LENGTHS.get(i) {
                (boundary, null_density)
            } else if i == BOUNDARY_LENGTHS.len() {
                (67, 100)
            } else {
                (rows, null_density)
            };
            let seed = base_seed.wrapping_add((i as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15));
            let run_id = self.next_run_id();
            let case_name = format!("diff-{i:03}-n{case_rows}");

            let case = generate_case(case_name.clone(), seed, case_rows, density)?;
            let expr = crate::query::parse_predicate(&case.expr_src)?;
            let data_version = 1;
            let mut dataset = Dataset::load("verify", case.batch, data_version, &[])?;

            // Apply intended deletions at strictly advancing versions.
            let mut dead = HashSet::new();
            let mut v = data_version;
            for row in &case.dead_rows {
                v += 1;
                dataset.delete_row(*row, v)?;
                dead.insert(*row);
            }

            let report = cross_check(
                &dataset,
                &expr,
                None, // current version: every intended deletion is visible
                &dead,
                &run_id,
                &case_name,
                seed,
            )?;
            reports.push(report);
        }
        Ok(reports)
    }
}
