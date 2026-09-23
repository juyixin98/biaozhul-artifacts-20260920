//! Test-only helpers exposed publicly so integration tests under `tests/` can
//! drive the engine with in-memory bytes and the injectable VFS without
//! depending on the CLI binary.

use std::path::Path;
use std::sync::Arc;

use crate::error::Result;
use crate::io::{FaultPlan, FaultVfs, RealVfs, Vfs};
use crate::key::KeySpec;
use crate::reference::reference_sort_bytes;
use crate::repository::{JobConfig, Repository};
use crate::sort::run_job;

/// Description of one end-to-end sort to run through the repository.
pub struct Harness {
    pub root: std::path::PathBuf,
    pub repo: Repository,
    pub id: String,
}

impl Harness {
    /// Create a fresh job rooted in a unique temp directory.
    pub fn new(id: &str, spec: &str, budget: u64, lanes: usize, keep_temp: bool) -> Self {
        let root = unique_root(id);
        let repo = Repository::new(&root);
        let cfg = JobConfig {
            key_spec: spec.to_string(),
            delim: b',',
            budget,
            max_lanes: lanes,
            keep_temp,
        };
        repo.create_job(id, cfg).expect("create job");
        Harness {
            root,
            repo,
            id: id.to_string(),
        }
    }

    /// Like [`Harness::new`] but wraps the VFS in a [`FaultVfs`] using `plan`.
    pub fn with_faults(id: &str, spec: &str, budget: u64, lanes: usize, plan: FaultPlan) -> Self {
        let root = unique_root(id);
        let real = Arc::new(RealVfs::new(Path::new(&root)));
        let vfs: Arc<dyn Vfs> = Arc::new(FaultVfs::new(real, plan));
        let repo = Repository::with_vfs(&root, vfs);
        let cfg = JobConfig {
            key_spec: spec.to_string(),
            delim: b',',
            budget,
            max_lanes: lanes,
            keep_temp: true,
        };
        repo.create_job(id, cfg).expect("create job");
        Harness {
            root,
            repo,
            id: id.to_string(),
        }
    }

    /// Reopen a previously created (possibly faulted) job through normal
    /// recovery on a plain, non-faulting VFS.
    pub fn reopen(id: &str, root: &std::path::Path) -> (Repository, crate::repository::Job) {
        let repo = Repository::new(root);
        let job = repo.open_job(id).expect("open/recover job");
        (repo, job)
    }

    pub fn vfs(&self) -> Arc<dyn Vfs> {
        self.repo.vfs()
    }

    pub fn input_path(&self) -> String {
        format!("jobs/{}/input.dat", self.id)
    }
    pub fn output_path(&self) -> String {
        format!("jobs/{}/output.txt", self.id)
    }

    pub fn write_input(&self, data: &[u8]) {
        let vfs = self.vfs();
        crate::io::atomic_write_bytes(
            vfs.as_ref(),
            &format!("jobs/{}/input.dat.tmp", self.id),
            &self.input_path(),
            data,
        )
        .expect("write input");
    }

    /// Run to completion.
    pub fn run(&mut self) -> Result<crate::sort::SortStats> {
        let mut job = self
            .repo
            .open_job(&self.id)
            .unwrap_or_else(|_| panic!("job {} missing", self.id));
        run_job(&self.repo, &mut job)
    }

    pub fn output(&self) -> Vec<u8> {
        crate::io::read_all(self.vfs().as_ref(), &self.output_path()).expect("read output")
    }

    pub fn cleanup(self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

/// Compute the in-memory reference result for `data` under `spec`.
pub fn reference(data: &[u8], spec: &str) -> Vec<u8> {
    let ks = KeySpec::parse(spec, b',').unwrap();
    reference_sort_bytes(data, &ks).expect("reference sort")
}

fn unique_root(id: &str) -> std::path::PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir =
        std::env::temp_dir().join(format!("extsort-it-{id}-{}-{}", std::process::id(), nanos));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}
